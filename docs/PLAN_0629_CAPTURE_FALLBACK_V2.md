# PLAN 0629: Screenshot Timeout File Fallback V2 审查修订版

日期：2026-06-29

状态：已根据计划审查重写。本文保留 V2 中可行的核心设计，同时把不可执行或容易误导实现的部分移到文末“审查经验：要避开的执行方向”。

基线文档：[HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md](HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md)

## 目标

本计划解决一个窄问题：当 Harness 通过 UE MCP 调用 viewport 截图工具时，UE 已经完成截图并把文件写到项目截图目录，但 final SSE result 没有被 Harness 读到，导致 `take_screenshot` 超时。

目标行为：

1. 正常路径仍然优先使用 UE SSE result。
2. 只有 `CaptureAssetImage(AssetPath="")` 这个 viewport 截图分支超时或 SSE 无 result 时，才尝试文件 fallback。
3. 文件 fallback 从当前 UE 项目的截图目录读取“本次请求开始后生成的最新 PNG”。
4. `mode="asset"` 必须显式传 `asset_path`，不允许空 asset path 静默退化成 viewport。
5. 自动发现 UE 项目根目录是最终的默认主路径；显式配置只作为硬覆盖或救援通道。

## 核心判断

`CaptureAssetImage` 有两条完全不同的 UE 逻辑分支：

| UE 工具调用 | Harness 触发场景 | UE 图像来源 | 写项目截图目录 | 文件 fallback |
| --- | --- | --- | --- | --- |
| `CaptureEditorImage()` | `mode="editor"` | Slate 窗口合成 | 否 | 否 |
| `CaptureAssetImage(AssetPath="")` | `mode="viewport"` 或 editor 失败后回退 viewport | Level Editor viewport | 是 | 是，唯一默认触发点 |
| `CaptureAssetImage(AssetPath="/Engine/...")` | `mode="asset"` | `ThumbnailTools::RenderThumbnail` | 否 | 否 |

所以 fallback 判断函数应保持非常窄：

```python
def _should_use_file_fallback(tool_name: str, asset_path: str) -> bool:
    return (
        tool_name == "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
        and asset_path == ""
    )
```

## 路径发现策略

### 设计原则

自动发现应成为常规使用路径：用户启动 Harness 时不需要每次传 `--ue-project-root`。Harness 已经知道 UE MCP 的 `ue_host` 和 `ue_port`，在本机连接时应主动找到监听该端口的 UnrealEditor 进程，从命令行中的 `.uproject` 推导项目根目录。

但显式配置仍然必须存在。它不是日常主路径，而是硬覆盖或故障救援：

1. 用户修改了 Editor Screenshot Save Directory。
2. UE 通过跳板、脚本或非标准命令行启动，自动发现拿不到 `.uproject`。
3. 多个 UE 进程或端口映射导致自动发现结果不可信。
4. 未来支持远程 UE，Harness 机器无法扫描 UE 进程。

### 解析优先级

优先级分成“硬覆盖”和“默认主路径”两类：

| 优先级 | 来源 | 定位 |
| --- | --- | --- |
| 0 | `HARNESS_UE_SCREENSHOT_DIR` 或 CLI `--ue-screenshot-dir` | 硬覆盖。用户直接给截图目录时必须尊重 |
| 1 | 自动发现当前 UE 进程 | 默认主路径。无硬覆盖时首先执行 |
| 2 | `HARNESS_UE_PROJECT_ROOT` 或 CLI `--ue-project-root` | 救援路径。自动发现失败或被禁用时使用 |
| 3 | 无配置、发现失败 | 禁用文件 fallback，保留原始 SSE 异常 |

注意：如果用户显式传入 `HARNESS_UE_SCREENSHOT_DIR`，它一定优先，因为 UE 编辑器截图保存目录可能被改到项目外。`HARNESS_UE_PROJECT_ROOT` 默认作为救援路径，而不是日常主路径；如果后续需要强制手动项目根，可以增加 `HARNESS_UE_PROJECT_ROOT_MODE=force`，不放进 P0。

### Config 与 CLI

这两个字段应该进入 `Config`，不要把环境变量读取藏在 `capturer.py` 内部：

```python
ue_project_root: Path | None = None
ue_screenshot_dir: Path | None = None
```

`Config.from_env()` 负责读取：

```text
HARNESS_UE_PROJECT_ROOT
HARNESS_UE_SCREENSHOT_DIR
```

`Config.merge_cli_overrides()` 负责接收：

```text
--ue-project-root
--ue-screenshot-dir
```

同时修正现有 CLI 一致性问题：`cli.py` 已经定义了 `--ue-host`，但 `cmd_start()` 当前没有把 `args.ue_host` 传给 `merge_cli_overrides()`。这次一并修掉。

### 路径拼接

自动发现或手动项目根得到的是：

```text
{UE_PROJECT_ROOT}
```

截图目录拼接为：

```text
{UE_PROJECT_ROOT}/Saved/Screenshots/WindowsEditor
```

这是 Windows Editor 下 `FPaths::ScreenShotDir()` 的默认结果。非 Windows 平台 P1 再补平台目录推导；P0 只保证当前 Windows 开发环境。

## 自动发现实现

### 触发时机

在 `init_shot_session(config)` 中执行一次并缓存结果：

```python
_ue_screenshot_dir: Path | None = None
```

不要每次截图都扫描进程。发现失败也要缓存“无 fallback 目录”的结果，并在日志中说明。

### 异步安全

`init_shot_session()` 是 async 函数。PowerShell 进程发现不能直接长时间阻塞 event loop。实现上可以：

1. 同步 helper 负责真实发现。
2. async 层通过 `asyncio.to_thread()` 调用。
3. 每个 subprocess 调用必须有 timeout，例如 3 秒。

伪代码：

```python
async def _resolve_screenshot_dir(config: Config) -> Path | None:
    if config.ue_screenshot_dir:
        return _normalize_path(config.ue_screenshot_dir)

    discovered = await asyncio.to_thread(_discover_ue_project_root_sync, config)
    if discovered:
        return discovered / "Saved" / "Screenshots" / "WindowsEditor"

    if config.ue_project_root:
        return _normalize_path(config.ue_project_root) / "Saved" / "Screenshots" / "WindowsEditor"

    return None
```

### Windows 进程发现

P0 只实现 Windows：

```python
def _discover_ue_project_root_sync(config: Config) -> Path | None:
    if sys.platform != "win32":
        return None
    if not _is_local_host(config.ue_host):
        return None

    pids = _list_listening_pids(config.ue_port)
    for pid in pids:
        cmdline = _get_process_command_line(pid)
        project = _extract_uproject_from_command_line(cmdline)
        if project and project.is_file():
            return project.parent
    return None
```

关键要求：

1. `Get-NetTCPConnection` 可能返回多行 PID，必须去重并逐个尝试。
2. 优先选择命令行里包含 `.uproject` 的进程。
3. 每个 PowerShell subprocess 必须设置 timeout。
4. 如果 `ue_host` 不是 `127.0.0.1`、`localhost`、`::1` 或本机地址，直接返回 `None`。
5. 自动发现失败不能影响 Harness 启动，只是禁用文件 fallback 或转入手动项目根救援路径。

PowerShell 查询可以这样组织：

```powershell
Get-NetTCPConnection -LocalPort <port> -ErrorAction SilentlyContinue |
  Where-Object { $_.State -eq 'Listen' } |
  Select-Object -ExpandProperty OwningProcess -Unique
```

命令行查询：

```powershell
(Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>').CommandLine
```

解析 `.uproject` 时先匹配引号内路径，再匹配无引号路径：

```python
match = re.search(r'"([^"]+\.uproject)"', cmdline)
if not match:
    match = re.search(r'([^\s"]+\.uproject)', cmdline)
```

## fallback 异常范围

需要捕获两条异常路径。

### 路径 A：socket read timeout

`httpx.ReadTimeout` 表示 Harness 仍在等 SSE body，但 `sse_read_timeout` 内没有读到新字节。

```python
except httpx.ReadTimeout:
    # 尝试文件 fallback
```

### 路径 B：SSE 流结束但无 result

当前 `harness/client.py` 的 `_read_sse_stream()` 在流正常结束但没有 result 时会抛：

```text
JsonRpcError(-32000, "SSE 流结束但未找到工具结果 ...")
```

它和 `httpx.ReadTimeout` 没有继承关系，必须单独捕获：

```python
def _is_sse_no_result_error(exc: JsonRpcError) -> bool:
    return exc.code == -32000 and "SSE 流结束但未找到工具结果" in exc.message
```

不要捕获宽泛的 `Exception`。`ValueError`、`RuntimeError`、普通 tool error 都不应该进入文件 fallback。

## fallback 文件选择规则

只扫描 `_ue_screenshot_dir` 下的 PNG：

```python
def _find_latest_screenshot(directory: Path, since: float) -> Path | None:
    candidates = []
    now = time.time()
    for path in directory.glob("*.png"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        if stat.st_mtime < since:
            continue
        if stat.st_mtime > now + 2.0:
            continue
        candidates.append((stat.st_mtime, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]
```

读取前建议做最小 PNG header 校验：

```python
def _looks_like_png(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False
```

如果 fallback 文件读取失败，必须保留原始 SSE 异常，不要让 `OSError`、坏 PNG、权限错误覆盖根因。

## capturer.py 主流程伪代码

重点：不要用“except 后 pass，再在外面裸 `raise`”的写法。fallback 找不到文件时，要在对应 except block 内直接 `raise`，保留原异常。

```python
import httpx
import time
from harness.client import JsonRpcError


async def capture(
    ue_client,
    max_width=1024,
    max_height=768,
    *,
    mode="viewport",
    asset_path="",
    hide_ui=False,
) -> Screenshot:
    if mode not in ("viewport", "editor", "asset"):
        raise ValueError("无效的截图模式")
    if mode == "asset" and not asset_path:
        raise ValueError("mode='asset' requires non-empty asset_path")
    if _shot_client is None or not _shot_client.is_connected:
        raise RuntimeError("截图 session 未初始化或已断开")

    b_show_ui = not hide_ui

    async with _shot_lock:
        if mode == "editor":
            try:
                result = await _shot_client.call_tool(
                    "ToolsetRegistry.EditorAppToolset.CaptureEditorImage", {}
                )
                if result:
                    return parse_screenshot(result, max_width, max_height)
            except Exception as e:
                from harness.verification.debug import log_exception
                log_exception(e, "capturer editor->viewport fallback")
                logger.debug("CaptureEditorImage 失败，回退到 viewport 模式")

            # editor 失败后必须强制 viewport。
            # 不要让调用方传进来的 asset_path 影响这个分支。
            return await _capture_asset_image_with_file_fallback(
                asset_path="",
                b_show_ui=b_show_ui,
                max_width=max_width,
                max_height=max_height,
            )

        if mode == "viewport":
            return await _capture_asset_image_with_file_fallback(
                asset_path="",
                b_show_ui=b_show_ui,
                max_width=max_width,
                max_height=max_height,
            )

        # mode == "asset"，上方已经校验 asset_path 非空。
        result = await _shot_client.call_tool(
            "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
            {"AssetPath": asset_path, "bShowUI": b_show_ui},
        )
        return parse_screenshot(result, max_width, max_height)
```

helper：

```python
async def _capture_asset_image_with_file_fallback(
    *,
    asset_path: str,
    b_show_ui: bool,
    max_width: int,
    max_height: int,
) -> Screenshot:
    tool_name = "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
    args = {"AssetPath": asset_path, "bShowUI": b_show_ui}
    start_wall = time.time()

    try:
        result = await _shot_client.call_tool(tool_name, args)
        return parse_screenshot(result, max_width, max_height)
    except httpx.ReadTimeout:
        screenshot = _try_file_fallback(
            tool_name, asset_path, start_wall, max_width, max_height
        )
        if screenshot is not None:
            return screenshot
        raise
    except JsonRpcError as exc:
        if not _is_sse_no_result_error(exc):
            raise
        screenshot = _try_file_fallback(
            tool_name, asset_path, start_wall, max_width, max_height
        )
        if screenshot is not None:
            return screenshot
        raise
```

fallback helper：

```python
def _try_file_fallback(
    tool_name: str,
    asset_path: str,
    start_wall: float,
    max_width: int,
    max_height: int,
) -> Screenshot | None:
    if not _should_use_file_fallback(tool_name, asset_path):
        return None
    if _ue_screenshot_dir is None or not _ue_screenshot_dir.is_dir():
        return None

    latest = _find_latest_screenshot(_ue_screenshot_dir, since=start_wall - 2.0)
    if latest is None:
        return None
    if not _looks_like_png(latest):
        logger.warning("截图 fallback 候选不是有效 PNG: %s", latest)
        return None

    try:
        logger.warning(
            "UE screenshot SSE 未返回结果，使用文件 fallback: %s",
            latest,
        )
        return capture_from_file(latest, max_width, max_height)
    except Exception as file_err:
        logger.warning("文件 fallback 读取失败: %s (file=%s)", file_err, latest)
        return None
```

## 文件变更清单

| 文件 | 改动 |
| --- | --- |
| `harness/config.py` | 增加 `ue_project_root`、`ue_screenshot_dir`；`from_env()` 读取对应 env；`merge_cli_overrides()` 支持覆盖 |
| `harness/cli.py` | 增加 `--ue-project-root`、`--ue-screenshot-dir`；修复已有 `--ue-host` 未传入 `merge_cli_overrides()` 的问题 |
| `harness/verification/capturer.py` | 增加自动发现、截图目录解析、asset 空路径校验、viewport 文件 fallback、PNG/mtime 过滤、双异常捕获 |
| `tests/test_config.py` | 覆盖 env 和 CLI merge 的路径字段 |
| `tests/test_verification.py` 或新增 `tests/test_capturer.py` | 覆盖自动发现 mock、latest screenshot 选择、ReadTimeout fallback、JsonRpcError fallback、asset 空路径校验、editor 回退强制 viewport |

## 测试计划

### 单元测试

1. 配置解析：
   - `HARNESS_UE_SCREENSHOT_DIR` 被读取并规范化。
   - `HARNESS_UE_PROJECT_ROOT` 被读取并可拼接截图目录。
   - CLI override 覆盖 env。
   - `--ue-host` 覆盖真实生效。

2. 自动发现：
   - mock PowerShell 返回单个 PID 和包含 `.uproject` 的命令行。
   - mock PowerShell 返回多行重复 PID。
   - mock 命令行没有 `.uproject`，返回 `None`。
   - `ue_host` 为非本机地址时不扫描本机进程。
   - subprocess timeout 时返回 `None`，不影响启动。

3. 最新截图选择：
   - 只选择 `*.png`。
   - 空文件跳过。
   - mtime 早于 `start_wall - 2s` 的旧图跳过。
   - mtime 异常晚于 `now + 2s` 的文件跳过。
   - 多个候选取最新。

4. fallback：
   - `httpx.ReadTimeout` + 新 PNG -> 返回 `capture_from_file()` 结果。
   - `JsonRpcError(-32000, "SSE 流结束但未找到工具结果")` + 新 PNG -> 返回 `capture_from_file()` 结果。
   - 其他 `JsonRpcError` 不 fallback，原样抛出。
   - fallback 目录不存在 -> 原 `ReadTimeout` 仍然抛出，不变成 `RuntimeError`。
   - 文件损坏 -> 记录 warning，原 SSE 异常仍然抛出。

5. mode 语义：
   - `mode="asset", asset_path=""` 直接 `ValueError`。
   - `mode="asset", asset_path="/Engine/..."` 不进入文件 fallback。
   - `mode="editor"` 的 `CaptureEditorImage()` 失败后，回退调用必须是 `CaptureAssetImage(AssetPath="")`，即使调用方传了 `asset_path`。

### 手动验证

1. 不设置 `HARNESS_UE_PROJECT_ROOT`，启动 UE 与 Harness。
2. Harness 启动日志应显示自动发现到的截图 fallback 目录。
3. 多次调用：

```text
take_screenshot {"mode": "viewport", "hide_ui": true}
```

如果 SSE 正常，返回正常截图；如果 SSE timeout 或无 result，fallback 应读取项目截图目录中新生成的 `ScreenShotxxxxx.png`。

4. 多次调用：

```text
take_screenshot {
  "mode": "asset",
  "asset_path": "/Engine/BasicShapes/BasicShapeMaterial_Inst.BasicShapeMaterial_Inst",
  "hide_ui": true
}
```

应继续走 256x256 thumbnail 分支，不使用文件 fallback。

## 风险与边界

1. 自动发现是目标主路径，但不等于无条件可信。显式截图目录必须能硬覆盖。
2. 远程 UE 不支持进程扫描，必须配置截图目录或项目根。
3. 用户如果修改了 Editor Screenshot Save Directory，项目默认路径可能不对，应使用 `HARNESS_UE_SCREENSHOT_DIR`。
4. 文件 fallback 只保证 viewport 语义，不能用于 asset thumbnail。
5. 不删除截图目录中的任何文件。
6. 自动发现失败不能阻断 Harness 启动。

## 审查经验：要避开的执行方向

下面保留原 V2 中被审查判定为不可直接执行的方向，作为后续实现时的反例记录。

### 经验 1：不要在 except 外部裸 `raise`

原方向：

```python
except httpx.ReadTimeout:
    pass
except JsonRpcError as e:
    if ...:
        pass
...
raise
```

问题：离开 `except` block 后，裸 `raise` 没有 active exception，会产生 `RuntimeError: No active exception to reraise`。正确方向是在 `except` block 内尝试 fallback，失败后直接 `raise`。

### 经验 2：不要把路径配置完全藏进 capturer.py

原方向：`harness/config.py` 不新增字段，`capturer.py` 自己读取 `HARNESS_UE_PROJECT_ROOT` 和 `HARNESS_UE_SCREENSHOT_DIR`。

问题：配置来源分裂，CLI override 无法统一，测试也更难。截图目录是 Harness 行为配置，应该进入 `Config`，自动发现只是 resolver 的一部分。

### 经验 3：不要把自动发现降级成边缘 fallback

审查时曾建议“显式配置为主，自动发现最后 fallback”。这个方向不符合目标体验。最终目标是 Harness 默认自动理解它连接的是哪个 UE 项目，用户不需要日常手填路径。

修订后方向：自动发现是无硬覆盖时的默认主路径；显式截图目录是硬覆盖，显式项目根是救援路径。

### 经验 4：不要让 editor 回退路径继承调用方 asset_path

原伪代码把 `mode="editor"` 的失败路径 fall through 到统一 `path = "" if mode == "viewport" else asset_path`。如果调用方传了 `mode="editor", asset_path="/Engine/..."`，回退可能误走 asset thumbnail。

正确方向：`editor` 的 fallback 必须强制调用 `CaptureAssetImage(AssetPath="")`。

### 经验 5：不要假设 PowerShell 输出只有一个 PID

原方向：

```python
pid = result.stdout.strip()
if not pid or not pid.isdigit():
    return None
```

问题：`Get-NetTCPConnection` 可能输出多行或重复 PID。应解析多行、去重、逐个检查命令行。

### 经验 6：不要让 PowerShell 发现阻塞 event loop

原方向在 async 初始化函数里直接 `subprocess.run()`，且没有 timeout。

问题：PowerShell 卡住会阻塞 Harness 启动。应使用 `asyncio.to_thread()`，并给 subprocess 设置短 timeout。

### 经验 7：不要只用 mtime 下限判断截图新鲜度

原方向只写 `mtime >= request_start - 2s`。

问题：还需要排除空文件、非 PNG、异常未来时间，并在读取失败时保留原 SSE 异常。
