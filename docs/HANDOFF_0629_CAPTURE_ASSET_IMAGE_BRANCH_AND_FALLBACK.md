# HANDOFF 0629: CaptureAssetImage 分支确认与截图文件 fallback

日期：2026-06-29

状态：建议执行 Harness 侧 fallback 改动。新的证据已经把问题范围从“所有 `CaptureAssetImage` 都不稳定”收窄到“空路径或当前关卡路径触发的 viewport 截图分支更容易在连续调用后丢 final SSE result”。显式指定非关卡资产 object path 时，工具走资产缩略图分支，目前多次调用稳定。

写作方式：按 `code-to-article` 的混合模式组织。前半部分先解释链路和机制，后半部分给源码位置、改动点和测试计划。

## 结论先行

当前最值得做的改动不是继续把 timeout 调大，也不是直接把所有截图逻辑搬到 UE 侧重写。应该先在 Harness 的截图封装层加一个文件 fallback：

1. 正常路径仍然调用 UE MCP tool，优先拿最终 SSE result。
2. 如果截图类 tool 超时，并且这次调用是 viewport 截图语义，则去 UE 项目的截图目录找“本次请求开始之后产生的最新截图文件”。
3. 找到后复用 Harness 已有的 `capture_from_file()`，把本地图片读成 base64，再交给当前 Vision 管线。
4. 如果找不到足够新的文件，就保留原始 timeout，不返回旧图。

这个改动的价值在于：用户已经能在 UE log 中看到工具执行、异步回调进入、准备 `OnComplete`、以及 `Tracing Screenshot "ScreenShot00037" taken...`。这说明“截图动作本身”已经发生。fallback 利用的是已经落盘的截图结果，绕过的是“final SSE result 没有被 Harness 读到”的不稳定链路。

## 当前链路

本仓库默认端口语义如下，以当前 `harness/config.py` 为准：

| 端口 | 角色 | 源码位置 |
| --- | --- | --- |
| `9000` | Harness 对外暴露的 MCP Server，LLM 或测试脚本连接这里 | `harness/config.py:24` |
| `8000` | UE 内部 ModelContextProtocol MCP Server，Harness 连接这里 | `harness/config.py:19` |

所以正常链路是：

```text
Client / test script
  -> http://127.0.0.1:9000/mcp
  -> Harness take_screenshot
  -> Harness screenshot client
  -> http://127.0.0.1:8000/mcp
  -> UE ModelContextProtocolServer
  -> ToolsetRegistry.EditorAppToolset.CaptureAssetImage
  -> UE async result
  -> UE final SSE result
  -> Harness parse_screenshot / capture_from_file
  -> VisionInterceptor / SnapshotRecorder
```

如果你说“直连 UE”，按当前配置通常指绕过 Harness，直接连 `8000`。如果测试脚本连的是 `9000`，那它实际上在走 Harness 的封装工具 `take_screenshot`，只是这个封装内部再去调 UE。

## TCP/SSE 规则：UE log 里的 OnComplete 不等于 Harness 已收到

这里最容易误解的一点是：UE 打出 `About to OnComplete SSE result`，只说明 C++ 代码调用了 HTTPServer 的完成回调；它不等于最终 SSE body 已经穿过 HTTPServer 状态机、TCP socket、httpx 流式读取，并被 Harness 解析到。

UE MCP Server 的 `tools/call` 不是普通的一次性 JSON response，而是 event-stream。它分两步写：

1. 第一次 `OnComplete`：先发 HTTP 头和空 body，标记这是一个还会继续写的 SSE 流。
2. 第二次 `OnComplete`：工具真正完成后，再发 `data: {"result": ...}\n\n` 这样的 final SSE frame。

源码对应在项目插件的 `ModelContextProtocolServer.cpp`：

```cpp
EnumAddFlags(Response->Flags,
    EHttpServerResponseFlags::MultipleWriteStream |
    EHttpServerResponseFlags::HasAdditionalWrites);
OnComplete(MoveTemp(Response));
```

代码位置：`D:\Programs\2024-2\Epic Games\UE58_Proj\MCP\Plugins\ModelContextProtocol\Source\ModelContextProtocol\Private\ModelContextProtocolServer.cpp:867-877`

工具完成后再写 final frame：

```cpp
EnumAddFlags(ServerResponse->Flags,
    EHttpServerResponseFlags::MultipleWriteStream |
    EHttpServerResponseFlags::SkipHeaderWrite);
UE_LOG(LogModelContextProtocol, Log,
    TEXT("[DIAG] About to OnComplete SSE result for '%ls'"), *ToolName);
OnComplete(MoveTemp(ServerResponse));
```

代码位置：`ModelContextProtocolServer.cpp:943-947`

HTTPServer 的三个 flag 语义在引擎源码里写得很直接：

```cpp
MultipleWriteStream = 1 << 0,
HasAdditionalWrites = 1 << 1,
SkipHeaderWrite = 1 << 2,
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Source\Runtime\Online\HTTPServer\Public\HttpServerResponse.h:20-30`

关键 TCP 规则是：HTTPServer 写入是异步状态机，不是函数调用栈返回值。第一次写带 `HasAdditionalWrites`，连接写完后会回到等待下一次写的状态；最后一次写不带 `HasAdditionalWrites`，连接才回到普通读请求状态或关闭。

```cpp
if (WriteContext.HasAdditionalWrites())
{
    ChangeState(EHttpConnectionState::AwaitingProcessing);
}
else
{
    ChangeState(EHttpConnectionState::AwaitingRead);
}
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Source\Runtime\Online\HTTPServer\Private\HttpConnection.cpp:276-288`

Harness 这边正在用 `httpx.stream()` 和 `aiter_lines()` 等 final SSE frame：

```python
if "text/event-stream" in content_type:
    result = await self._read_sse_stream(rid, response)
    await response.aclose()
    return result
```

代码位置：`D:\Programs\2024-2\ue-agent-harness\harness\client.py:261-264`

因此当前现象可以精确表述为：UE tool 已经执行到截图完成，甚至可能已经调用了第二次 `OnComplete`，但 Harness 没有读到 final SSE body，于是 `aiter_lines()` 一直等到 read timeout。

## UE 侧分支：有没有显式 path 是两个不同工具路径

`CaptureAssetImage` 这个名字会误导人。它不是“永远捕获一个资产图”。它内部先看 `AssetPath`，然后决定是截当前 viewport，还是渲染资产缩略图。

### 分支 1：空路径或当前关卡路径，走 viewport 截图

触发条件在 `CaptureAssetImage()` 一开始：

```cpp
if (AssetPath.IsEmpty() ||
    CurrentLevelPackage == FPackageName::ObjectPathToPackageName(AssetPath))
{
    return FViewportScreenshotCapture::Start(bShowUI).Get();
}
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Plugins\Experimental\ToolsetRegistry\Source\ToolsetRegistry\Private\ToolsetRegistry\EditorAppToolset.cpp:389-392`

这个分支做的事不是查一个默认资产，而是启动 editor viewport 截图。它先拿到当前 Level Editor viewport，注册截图完成 delegate，然后发起截图请求：

```cpp
Handle = UE::ToolsetRegistry::FDelegateHandleRaii::Create(
    OnScreenshotCaptured,
    OnScreenshotCaptured.AddLambda(...));
FScreenshotRequest::RequestScreenshot(bShowUI);
```

代码位置：`EditorAppToolset.cpp:91-103`

截图完成后，`OnCaptured()` 把像素编码进 `FToolsetImage`，再 `Result->SetValue(Image)`：

```cpp
if (Image.SetFromBitmap(Final, Dimensions))
{
    Result->SetValue(Image);
}
```

代码位置：`EditorAppToolset.cpp:137-168`

这条链路有两个特征：

1. 它跨帧。`RequestScreenshot()` 只是请求截图，真正的像素数据在后续 viewport 处理截图时回调。
2. 它会触发 UE 编辑器自身的截图保存逻辑。当前用户已经在项目目录看到 `ScreenShot00046.png` 等文件。

UE 默认截图目录来自 `FPaths::ScreenShotDir()`：

```cpp
FString FPaths::ScreenShotDir()
{
    return FPaths::ProjectSavedDir() + TEXT("Screenshots/") +
        FPlatformProperties::PlatformName() + TEXT("/");
}
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Source\Runtime\Core\Private\Misc\Paths.cpp:553-556`

编辑器的默认截图保存目录会被初始化为这个路径：

```cpp
GameScreenshotSaveDirectory.Path = FPaths::ScreenShotDir();
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Source\Runtime\Engine\Private\GameEngine.cpp:945`

在 viewport 截图处理阶段，UE 既广播 `OnScreenshotCaptured()`，也调用保存逻辑：

```cpp
FScreenshotRequest::OnScreenshotCaptured().Broadcast(...);
bIsScreenshotSaved = RequestSaveScreenshot(bWriteAlpha, Bitmap, BitmapSize, 255);
```

代码位置：`D:\Programs\2024-2\UE\UE_5.8\Engine\Source\Editor\UnrealEd\Private\EditorViewportClient.cpp:7048-7066`

这就是 fallback 成立的根基：即使 Harness 没读到 final SSE result，viewport 分支仍可能已经把截图文件写到了项目 `Saved/Screenshots/WindowsEditor`。

### 分支 2：显式非关卡资产路径，走资产缩略图

如果 `AssetPath` 非空，并且不是当前加载关卡，代码会把裸 package path 规范化成 object path，检查资产存在和类型，然后走缩略图渲染：

```cpp
FString ObjectPath = AssetPath;
if (!ObjectPath.Contains(TEXT(".")))
{
    ObjectPath += TEXT(".") + ObjectPath.RightChop(LastSlash + 1);
}
```

代码位置：`EditorAppToolset.cpp:399-405`

通过资产注册表找到资产后，只有动画、骨架、静态网格、骨骼网格、纹理、材质接口这些类型可以继续：

```cpp
else if (!AssetData.IsInstanceOf<UAnimationAsset>() &&
    !AssetData.IsInstanceOf<USkeleton>() &&
    ...
    !AssetData.IsInstanceOf<UMaterialInterface>())
```

代码位置：`EditorAppToolset.cpp:420-429`

最终进入缩略图 capture：

```cpp
Result = FAssetThumbnailCapture::Start(ObjectPath).Get();
```

代码位置：`EditorAppToolset.cpp:432`

缩略图分支先异步加载资产，再用 ticker 轮询加载、编译、streaming 状态，最多等 10 秒：

```cpp
StreamableHandle = UAssetManager::Get().GetStreamableManager().RequestAsyncLoad(...);
TickerHandle = FTSTicker::GetCoreTicker().AddTicker(...);
```

代码位置：`EditorAppToolset.cpp:203-216`

当资产就绪后，它调用 `ThumbnailTools::RenderThumbnail()` 直接得到 256x256 之类的缩略图像素，再编码返回：

```cpp
ThumbnailTools::RenderThumbnail(
    Asset,
    ThumbnailTools::DefaultThumbnailSize,
    ThumbnailTools::DefaultThumbnailSize,
    ...);
```

代码位置：`EditorAppToolset.cpp:271-278`

这条链路和 viewport 分支的关键差异是：它不依赖 `FScreenshotRequest::RequestScreenshot()`，也不依赖 editor viewport 的截图文件落盘。用户这次显式传入：

```text
/Engine/BasicShapes/BasicShapeMaterial_Inst.BasicShapeMaterial_Inst
```

这是一个材质实例 object path，符合 `UMaterialInterface` 分支，所以走的是缩略图。三次返回：

```text
Screenshot 已获取: 256x256 image/png (mode=asset)
VisionInterceptor 已触发
```

这个结果和源码完全吻合。

## 两条分支的同步差异

| 维度 | 空路径 / 当前关卡路径 | 显式非关卡资产路径 |
| --- | --- | --- |
| 进入条件 | `AssetPath.IsEmpty()` 或 path 指向当前关卡 | `AssetPath` 非空且不是当前关卡 |
| UE 内部对象 | `FViewportScreenshotCapture` | `FAssetThumbnailCapture` |
| 图像来源 | 当前 Level Editor viewport | 资产缩略图渲染 |
| 完成方式 | `FScreenshotRequest` 发请求，后续 viewport 处理截图并广播 delegate | async load + ticker 轮询，资产就绪后直接 `RenderThumbnail` |
| 是否写项目截图目录 | 会触发 UE 截图保存逻辑，当前目录已有 `ScreenShot00046.png` 等证据 | 通常不写 `Saved/Screenshots/WindowsEditor` |
| 典型尺寸 | viewport 尺寸，例如用户 log 中 `1318 x 630` | 默认缩略图尺寸，用户测试中 `256 x 256` |
| 当前稳定性 | 连续调用第二次开始容易 timeout | 用户显式 object path 多次成功 |
| fallback 可行性 | 高，可以读最新截图文件 | 默认不建议用截图目录 fallback，语义可能错误 |

这里“一步同步差异”可以这样理解：

同步工具或近同步工具在 HTTP request 的同一段生命周期里很快产生 result，UE 的第二次 SSE 写入和第一次写入间隔很短。viewport 截图是两段式：先把 HTTP SSE 流打开，然后等 editor viewport 下一轮真正截图。这个等待期间，HTTPServer 连接、MCP session、active request、client stream 都要保持一致。任何一层状态机错过 final body，Harness 就会 timeout。

缩略图分支也不是纯同步，它也有 async load 和 ticker，但它不触发全局 viewport screenshot 流程，不依赖 editor viewport 保存截图文件，也不会产生 `Tracing Screenshot "ScreenShot000xx"` 这一类项目截图输出。当前证据说明它更稳定。

## 新证据如何修正 0624 的判断

0624 的判断是：`CaptureAssetImage` 已经执行，截图完成，但 final SSE result 在 UE `OnComplete` 到 Harness socket 之间丢失。这个判断仍然能解释“UE log 显示截图完成但 Harness timeout”。

0629 新证据把它进一步收窄：

1. 显式传入非关卡资产 object path 后，多次 Harness 调用稳定成功。
2. 因此不能继续说“`CaptureAssetImage` 整体不稳定”。
3. 更准确的说法是：`CaptureAssetImage(AssetPath="")` 或 path 指向当前关卡时，会进入 viewport 截图分支；这个分支在连续 Harness 调用后更容易出现 final SSE result 读不到。
4. `mode="asset"` 如果没有传 `asset_path`，当前 Harness 代码会把空字符串传给 UE，实际仍然是 viewport 分支，不是资产分支。

当前 Harness 的关键代码是：

```python
path = "" if mode == "viewport" else asset_path
result = await _shot_client.call_tool(
    "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
    {"AssetPath": path, "bShowUI": b_show_ui},
)
```

代码位置：`D:\Programs\2024-2\ue-agent-harness\harness\verification\capturer.py:118-123`

所以需要补一个输入语义保护：`mode="asset"` 时如果 `asset_path` 为空，应该直接报错，或者显式改成 `mode="viewport"`。不要让“asset 模式”静默退化成 viewport 分支。

## 为什么读最新截图文件是合适的 fallback

用户给出的 UE log 里有：

```text
LogModelContextProtocol: Running tool: 'ToolsetRegistry.EditorAppToolset.CaptureAssetImage'
LogModelContextProtocol: [DIAG] Async callback ENTERED: 'ToolsetRegistry.EditorAppToolset.CaptureAssetImage'
LogModelContextProtocol: [DIAG] About to OnComplete SSE result for 'ToolsetRegistry.EditorAppToolset.CaptureAssetImage'
LogCore: Display: Tracing Screenshot "ScreenShot00037" taken with size: 1318 x 630
```

这组日志说明：

1. ToolsetRegistry 的 tool 确实被调用。
2. 异步 callback 确实进入。
3. UE 已经准备发送 final SSE result。
4. viewport 截图文件已经被 UE 生成或 trace 到。

当前项目截图目录也已经有连续文件：

```text
D:\Programs\2024-2\Epic Games\UE58_Proj\MCP\Saved\Screenshots\WindowsEditor
ScreenShot00046.png
ScreenShot00045.png
ScreenShot00044.png
ScreenShot00043.png
...
```

这说明当 Harness 超时时，我们不是在“凭空猜一张图”。我们是在拿 UE 已经为同一次 viewport 截图动作写出的文件。

Harness 里已经有本地图片读取函数：

```python
def capture_from_file(path: Path, max_width: int = 1024, max_height: int = 768) -> Screenshot:
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
```

代码位置：`D:\Programs\2024-2\ue-agent-harness\harness\verification\capturer.py:126-145`

因此 fallback 的实现不是新建一条 Vision 图片通道，而是复用已有的本地文件转 base64、resize、`Screenshot` dataclass、`VisionInterceptor` 机制。

## 建议的 fallback 设计

### 触发条件

建议第一版只在这两类路径触发：

| Harness mode | UE tool | 是否启用文件 fallback | 原因 |
| --- | --- | --- | --- |
| `viewport` | `CaptureAssetImage(AssetPath="")` | 是 | UE 会写项目截图目录，语义一致 |
| `editor` 且 `CaptureEditorImage()` 失败后回退 viewport | `CaptureAssetImage(AssetPath="")` | 是 | 当前已有 editor 到 viewport fallback，最终仍是 viewport 截图 |
| `asset` 且 `asset_path` 非空 | `CaptureAssetImage(AssetPath="<asset>")` | 默认否 | 资产缩略图不写项目截图目录，读最新 viewport 文件会返回错误语义 |
| `asset` 且 `asset_path` 为空 | 不应调用 UE | 直接参数错误 | 避免“asset 模式”静默变成 viewport |
| raw passthrough `ToolsetRegistry.EditorAppToolset.CaptureAssetImage` | 直接透传 UE | P1 再做 | P0 放在 `take_screenshot` 封装层最小、最安全 |

捕获异常建议包括：

```python
httpx.ReadTimeout
asyncio.TimeoutError
TimeoutError
```

如果当前包装层只拿到普通 `Exception`，可以先通过 `isinstance` 和错误文本包含 `ReadTimeout` / `timed out` 做保守识别，但长期最好不要靠字符串分类。

### 文件选择规则

fallback 绝对不能简单读目录里最新的一张图。它必须证明这张图和本次请求足够相关。

建议规则：

1. 在调用 UE 前记录 `start_wall = time.time()`。
2. UE 调用 timeout 后，扫描截图目录的图片文件。
3. 只考虑 `mtime >= start_wall - 2.0` 的文件，给 Windows 文件时间戳和 UE 写入延迟留 2 秒余量。
4. 再加一个最大新鲜窗口，例如 `mtime <= time.time() + 2.0` 且 `mtime >= time.time() - 10 * 60`。
5. 选择 mtime 最新、文件大小大于 0、扩展名优先 `.png` 的文件。
6. 找到后记录日志：fallback 使用的文件路径、mtime、原始异常类型。
7. 找不到就重新抛出原始 timeout。

伪代码：

```python
start_wall = time.time()
try:
    result = await _shot_client.call_tool(...)
    return parse_screenshot(result, max_width, max_height)
except (httpx.ReadTimeout, asyncio.TimeoutError, TimeoutError) as exc:
    if should_try_file_fallback(mode, path):
        latest = find_latest_ue_screenshot(config, since=start_wall - 2.0)
        if latest is not None:
            logger.warning("UE screenshot SSE timed out; using file fallback: %s", latest)
            return capture_from_file(latest, max_width, max_height)
    raise
```

### 路径配置和相对拼接

用户当前项目截图目录是：

```text
D:\Programs\2024-2\Epic Games\UE58_Proj\MCP\Saved\Screenshots\WindowsEditor
```

这个路径可以拆成：

```text
UE project root:
D:\Programs\2024-2\Epic Games\UE58_Proj\MCP

UE screenshot relative path:
Saved\Screenshots\WindowsEditor
```

这和 UE 源码 `FPaths::ScreenShotDir()` 的规则一致：`ProjectSavedDir() + Screenshots + PlatformName()`。在 Windows Editor 下平台名就是 `WindowsEditor`。

建议在 `Config` 增加两个字段：

```python
ue_project_root: Path | None = None
ue_screenshot_dir: Path | None = None
```

解析优先级：

```python
def resolved_ue_screenshot_dir(self) -> Path | None:
    if self.ue_screenshot_dir:
        return self.ue_screenshot_dir
    if self.ue_project_root:
        return self.ue_project_root / "Saved" / "Screenshots" / "WindowsEditor"
    return None
```

推荐环境变量：

```text
HARNESS_UE_PROJECT_ROOT=D:\Programs\2024-2\Epic Games\UE58_Proj\MCP
HARNESS_UE_SCREENSHOT_DIR=D:\Programs\2024-2\Epic Games\UE58_Proj\MCP\Saved\Screenshots\WindowsEditor
```

`HARNESS_UE_SCREENSHOT_DIR` 用于覆盖默认拼接；`HARNESS_UE_PROJECT_ROOT` 用于最常见场景。两者都支持相对路径，但要明确相对谁解析：

1. 如果 Harness 总是从 repo 根目录启动，可以允许：

```text
HARNESS_UE_PROJECT_ROOT=..\Epic Games\UE58_Proj\MCP
```

2. 在代码里用 `Path(value).expanduser()`，如果不是绝对路径，则相对 `Path.cwd()` resolve。
3. 日志里打印 resolve 后的绝对路径，避免以后从别的 cwd 启动时悄悄指错目录。

不要把 `D:\Programs\2024-2\Epic Games\UE58_Proj\MCP` 写死进 `capturer.py`。这是当前机器上的项目路径，不是 harness 的通用配置。

## 具体改动计划

### 改动 1：配置层支持 UE 项目根和截图目录

要改的文件：`D:\Programs\2024-2\ue-agent-harness\harness\config.py`

当前 `Config` 只有 UE host/port、Harness listen port、timeout、vision、log dir。没有 UE project root 或 screenshot dir。

建议增加：

```python
ue_project_root: Path | None = None
ue_screenshot_dir: Path | None = None
```

`from_env()` 增加：

```python
ue_project_root=_env_path("HARNESS_UE_PROJECT_ROOT"),
ue_screenshot_dir=_env_path("HARNESS_UE_SCREENSHOT_DIR"),
```

`merge_cli_overrides()` 增加同名参数，并把这两个字段放进 `current` dict。否则 CLI 覆盖或 `init_shot_session(config)` 时会丢配置。

### 改动 2：CLI 支持显式传路径

要改的文件：`D:\Programs\2024-2\ue-agent-harness\harness\cli.py`

当前 `cmd_start()` 只把 `ue_port`、`listen_port` 传进 `merge_cli_overrides()`：

```python
config = Config.from_env().merge_cli_overrides(
    ue_port=args.ue_port,
    listen_port=args.listen_port,
)
```

代码位置：`harness/cli.py:62-65`

建议补：

```text
--ue-project-root
--ue-screenshot-dir
```

然后传入：

```python
config = Config.from_env().merge_cli_overrides(
    ue_port=args.ue_port,
    listen_port=args.listen_port,
    ue_host=args.ue_host,
    ue_project_root=args.ue_project_root,
    ue_screenshot_dir=args.ue_screenshot_dir,
)
```

顺手修一个现有问题：CLI 已定义 `--ue-host`，但 `cmd_start()` 目前没有把 `args.ue_host` 传进 `merge_cli_overrides()`。

### 改动 3：capturer 增加 latest screenshot fallback

要改的文件：`D:\Programs\2024-2\ue-agent-harness\harness\verification\capturer.py`

当前已有：

| 代码 | 作用 |
| --- | --- |
| `_shot_client` + `_shot_lock`，`capturer.py:26-27` | 截图专用 session 和串行锁 |
| `init_shot_session(config)`，`capturer.py:30-48` | Harness 启动时创建持久截图 session |
| `capture()`，`capturer.py:69-123` | 根据 mode 调 UE 截图 tool |
| `capture_from_file()`，`capturer.py:126-145` | 读本地图片并转成 `Screenshot` |

建议新增：

```python
_shot_config: Config | None = None
```

在 `init_shot_session(config)` 里保存配置。然后增加：

```python
def resolve_ue_screenshot_dir(config: Config) -> Path | None:
    ...

def find_latest_ue_screenshot(config: Config, since_wall: float) -> Path | None:
    ...

def _is_timeout_error(exc: BaseException) -> bool:
    ...

def _should_use_file_fallback(mode: str, asset_path: str) -> bool:
    return mode == "viewport"
```

`editor` 模式当前是先尝试 `CaptureEditorImage()`，失败后 fallback 到 viewport：

```python
result = await _shot_client.call_tool(
    "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
    {"AssetPath": "", "bShowUI": b_show_ui},
)
```

代码位置：`capturer.py:111-115`

这个内部 viewport fallback 也应该套同一套 timeout 到文件 fallback。

### 改动 4：asset 模式参数校验

要改的文件：`capturer.py`

当前：

```python
path = "" if mode == "viewport" else asset_path
```

代码位置：`capturer.py:118`

建议改成：

```python
if mode == "asset" and not asset_path:
    raise ValueError("mode='asset' requires non-empty asset_path; empty path captures the viewport")
path = "" if mode == "viewport" else asset_path
```

这是一个小改动，但能防止测试和生产日志继续混淆。否则调用方以为自己在测 asset，UE 实际在走 viewport。

### 改动 5：把 fallback 来源暴露到日志

可选但推荐。

现在 `Screenshot` dataclass 只有：

```python
data_b64: str
mime_type: str = "image/png"
width: int = 0
height: int = 0
```

代码位置：`capturer.py:61-66`

可以增加：

```python
source: str = "ue_sse"
source_path: str = ""
```

如果担心影响调用方，第一版不改 dataclass，只用 logger 记录即可。为了说服后续调试，我更建议加字段，并在 `server.py` 的返回文本里追加：

```text
source=file_fallback
```

但这属于可观测性增强，不是 fallback 成立的必要条件。

## 测试计划

### 单元测试

1. `Config` path 解析：
   - `HARNESS_UE_PROJECT_ROOT` 绝对路径时，得到 `<root>/Saved/Screenshots/WindowsEditor`。
   - `HARNESS_UE_SCREENSHOT_DIR` 存在时优先使用它。
   - 相对路径 resolve 后日志可读。

2. 最新截图选择：
   - 临时目录创建 `ScreenShot00001.png`、`ScreenShot00002.png`。
   - 只有 mtime 晚于 `since_wall - tolerance` 的文件可选。
   - 空文件、旧文件、非图片文件不会被选。

3. timeout fallback：
   - monkeypatch `_shot_client.call_tool` 抛 `httpx.ReadTimeout`。
   - 在临时截图目录放一张新 PNG。
   - `capture(mode="viewport")` 返回 `capture_from_file()` 生成的 `Screenshot`。

4. asset 模式校验：
   - `capture(mode="asset", asset_path="")` 直接 `ValueError`。
   - `capture(mode="asset", asset_path="/Engine/...")` 不使用文件 fallback。

### 手动验证

1. 启动 Harness：

```powershell
harness start --ue-port 8000 --listen-port 9000 --ue-project-root "D:\Programs\2024-2\Epic Games\UE58_Proj\MCP"
```

2. 连续跑显式资产路径：

```powershell
python tests/tool_verify_harness_vision.py
```

期望：继续稳定返回 `256x256 image/png`。

3. 单独压测 viewport：

```text
take_screenshot {"mode": "viewport", "hide_ui": true}
take_screenshot {"mode": "viewport", "hide_ui": true}
take_screenshot {"mode": "viewport", "hide_ui": true}
```

期望：

1. 如果 UE SSE 正常返回，行为不变。
2. 如果第二次或后续 `ReadTimeout`，Harness 读取 `Saved/Screenshots/WindowsEditor` 中本次请求产生的新图并返回。
3. VisionInterceptor 仍能触发，因为返回对象仍是 `Screenshot`。
4. 日志能看到 fallback 使用了哪一张 `ScreenShotxxxxx.png`。

### 对照矩阵

为了避免再混淆“session 改动”和“显式 path 改动”，建议至少保留这组矩阵：

| 条件 | 预期 |
| --- | --- |
| 持久截图 session + 显式资产 path | 稳定，缩略图分支 |
| 持久截图 session + 空 path | 可能触发 fallback，viewport 分支 |
| raw UE 直连 + 显式资产 path | 稳定，缩略图分支 |
| raw UE 直连 + 空 path | 用来观察 UE viewport 分支本身是否仍会产出文件 |

## 风险和边界

1. 文件 fallback 只能保证 viewport 语义，不保证 asset thumbnail 语义。显式资产 path 超时时，不应该默认返回最新 viewport 截图。
2. `CaptureEditorImage()` 本身是 Slate window 合成，不一定写项目截图目录。只有它回退到 viewport 后，文件 fallback 才可靠。
3. 如果多个 Harness 或人工操作同时在同一 UE 项目里截图，最新文件可能来自别的请求。时间戳 guard 是必须的，不是优化项。
4. 不要删除 UE 截图目录中的文件。fallback 只读。
5. 不要在没有配置项目根或截图目录时猜路径。没有配置就保留原 timeout。
6. 如果用户改过 Editor Screenshot Save Directory，`ProjectRoot/Saved/Screenshots/WindowsEditor` 可能不是实际目录。因此保留 `HARNESS_UE_SCREENSHOT_DIR` 作为最高优先级覆盖项。

## 推荐执行顺序

1. 先做 `capturer.py` 的 asset 空路径校验。这能立即消除“asset 模式其实走 viewport”的误判。
2. 再加 `Config` 和 CLI 的 `ue_project_root` / `ue_screenshot_dir`。
3. 然后在 `capturer.py` 给 viewport UE call 包上 timeout fallback。
4. 最后补测试和日志。

这组改动是低侵入的：不需要改 UE C++，不需要改变 SSE parser，也不改变正常成功路径。它只是给“UE 已经截图但 final SSE 没到 Harness”的情况补一条可证明、可审计、可回退的读取路径。

## 交接给下一个 agent 的最小任务描述

请在 `ue-agent-harness` 里实现截图 timeout 文件 fallback：

1. 在 `harness/config.py` 增加 `ue_project_root`、`ue_screenshot_dir`，支持 env 和 CLI override。
2. 在 `harness/cli.py` 增加 `--ue-project-root`、`--ue-screenshot-dir`，并把现有 `--ue-host` 传进 `merge_cli_overrides()`。
3. 在 `harness/verification/capturer.py`：
   - `mode="asset"` 且 `asset_path` 为空时直接报错。
   - 对 viewport 分支的 `CaptureAssetImage(AssetPath="")` 捕获 timeout。
   - timeout 后扫描 `resolved_ue_screenshot_dir()`。
   - 只接受本次请求开始之后产生的新图片。
   - 使用已有 `capture_from_file()` 返回 `Screenshot`。
4. 增加单元测试覆盖路径解析、最新文件选择、timeout fallback、asset 空路径校验。

当前用户项目根：

```text
D:\Programs\2024-2\Epic Games\UE58_Proj\MCP
```

当前用户截图目录：

```text
D:\Programs\2024-2\Epic Games\UE58_Proj\MCP\Saved\Screenshots\WindowsEditor
```

不要硬编码这两个绝对路径；它们只用于本机验证和默认配置示例。
