# HANDOFF 0630: 截图文件 fallback 实现完成

日期：2026-06-30（记录 0629 晚间开发）

状态：已实现并验证。viewport 截图在 SSE 超时后，通过轮询 UE 项目截图目录获取落盘文件，Vision 闭环正常触发。

基线文档：[HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md](HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md)（问题分析） + [PLAN_0629_CAPTURE_FALLBACK_V2.md](PLAN_0629_CAPTURE_FALLBACK_V2.md)（设计审查）

---

## 1. 解决了什么问题

Harness 调用 UE 的 viewport 截图工具 `CaptureAssetImage(AssetPath="")` 时，UE 完成了截图并把 PNG 写到了项目目录，但 Harness 没有读到 final SSE result，导致 `take_screenshot` 工具超时报错。

**受影响的场景：** 仅 `mode="viewport"`（或 `mode="editor"` 失败后回退 viewport）。`mode="asset"` 走资产缩略图分支，不受影响。

---

## 2. 根因：UE 截图回调延迟远超 Harness 超时

UE 的 viewport 截图是异步两段式：Harness 发 HTTP 请求 → UE 打开 SSE 流 → UE 注册截图回调 → 等编辑器 viewport 渲染帧 → 回调触发 → 写 final SSE frame。

**关键 log 证据（2026-06-29 21:32 CST，全链路）：**

```text
# UE 侧
13:32:28:153  Running tool: 'CaptureAssetImage'          ← Harness 请求到达
13:35:21:425  Async callback ENTERED                     ← 2 分 53 秒后回调才进入
13:35:21:438  About to OnComplete SSE result             ← 准备发送 final SSE
13:35:21:438  Tracing Screenshot "ScreenShot00051" taken  ← 文件落盘

# Harness 侧（对应时间线）
21:32:27 调用工具: CaptureAssetImage (id=3)              ← 发起请求
21:35:23 SSE 未返回结果，使用文件 fallback:              ← 60s 超时 + 近 3 分钟轮询后命中
          ScreenShot00051.png (mtime=2s ago)
21:35:44 Vision 分析完成: ✅ PASS                         ← 正常触发 Vision 闭环
```

**结论：** 截图文件在 Harness SSE 超时（60s）之后约 2 分钟才落盘。靠"超时即刻检查一次"无法工作，必须轮询等待。

---

## 3. 解决方案：三件套

### 3.1 asset 空路径语义保护

**为什么需要：** 旧代码中 `mode="asset"` 且 `asset_path=""` 会静默退化成 viewport 截图——调用方以为自己在测 asset 缩略图，实际走的是 viewport 分支。既混淆测试结果，也会触发不该触发文件 fallback。

**做法：** 在 `capture()` 入口加一行校验，空 asset path + asset mode 直接抛 `ValueError`，并提示使用 `mode="viewport"` 代替。

> **代码位置：** `harness/verification/capturer.py` → `capture()`，第 112-116 行

### 3.2 自动发现 UE 项目截图目录

**为什么需要：** 用户启动 Harness 时不应每次手动传 `--ue-project-root`。Harness 已经知道 UE 的 host/port，应能自动找到连接的是哪个 UE 项目。

**为什么用 netstat + wmic 而非 PowerShell：** PowerShell 有 .NET 冷启动延迟（首次调用可超过 3 秒），在 subprocess 中会触发超时。`netstat -ano` 和 `wmic` 是原生 Win32 命令，零冷启动。

**解析优先级：**

| 优先级 | 来源 | 用途 |
|:---:|------|------|
| 0 | `HARNESS_UE_SCREENSHOT_DIR` 或 CLI `--ue-screenshot-dir` | **硬覆盖**。用户直接指定截图目录时无条件使用 |
| 1 | 自动发现（netstat → PID → wmic → .uproject） | **默认主路径**。拼接 `Saved/Screenshots/WindowsEditor` |
| 2 | `HARNESS_UE_PROJECT_ROOT` 或 CLI `--ue-project-root` | **救援路径**。自动发现失败时使用 |
| 3 | 无 | 禁用文件 fallback，保留原始 SSE 异常 |

**自动发现执行流程：**

```text
netstat -ano | findstr :8000 LISTENING  → 拿到 PID
  ↓
wmic process where ProcessId=<pid> get CommandLine  → 拿到命令行
  ↓
正则提取 .uproject 路径  → 得到项目根目录
  ↓
拼接 Saved/Screenshots/WindowsEditor  → 截图目录
```

> **代码位置：**
> - 入口：`capturer.py` → `init_shot_session()`，第 65 行调用 `_resolve_screenshot_dir()`
> - 协调：`capturer.py` → `_resolve_screenshot_dir()`，第 441-462 行（async，优先级编排）
> - PID 发现：`capturer.py` → `_list_listening_pids()`，第 350-372 行（netstat 解析）
> - 命令行查询：`capturer.py` → `_get_process_command_line()`，第 375-392 行（wmic 主路径 + PowerShell 回退）
> - 路径提取：`capturer.py` → `_extract_uproject_from_command_line()`，第 395-403 行
> - 配置读取：`harness/config.py` → `Config.ue_project_root` / `Config.ue_screenshot_dir`（env + CLI merge）

### 3.3 超时后轮询文件 fallback

**为什么需要轮询：** 如第 2 节所述，UE 截图文件可能比 SSE 超时晚 2-3 分钟才落盘。单次检查必然失败。

**做法：** SSE 超时或 SSE 流无 result 后，不立即抛异常，而是进入轮询循环——每 3 秒扫描一次截图目录，检查是否有本次请求开始之后产生的有效 PNG 文件。找到则读取返回（复用已有的 `capture_from_file()`），轮询超时（180 秒）后才重新抛出原始异常。

**文件选择 guard（4 层）：**

| Guard | 作用 |
|-------|------|
| mtime ≥ `request_start - 2s` | 排除旧文件 |
| mtime ≤ `now + 2s` | 排除异常未来时间戳 |
| `st_size > 0` | 排除空文件 |
| PNG magic bytes (`\x89PNG\r\n\x1a\n`) | 排除非 PNG / 损坏文件 |

**异常捕获范围（精确的两种）：**

| 异常类型 | 触发条件 | 是否 fallback |
|----------|---------|:---:|
| `httpx.ReadTimeout` | SSE 读取超时 | ✅ |
| `JsonRpcError(-32000, "SSE 流结束但未找到工具结果...")` | SSE 流正常结束但无 result | ✅ |
| 其他 `JsonRpcError` | 工具执行错误 | ❌ 原样抛出 |
| `ValueError` / `RuntimeError` | Harness 内部错误 | ❌ 原样抛出 |

> **代码位置：**
> - 轮询入口：`capturer.py` → `_try_file_fallback()`，第 243-270 行（async，委托给 `_poll_and_capture`）
> - 轮询循环：`capturer.py` → `_poll_and_capture()`，第 278-299 行（async，每 3s 检查，最多 180s）
> - 文件查找：`capturer.py` → `_find_latest_screenshot()`，第 215-240 行
> - PNG 校验：`capturer.py` → `_looks_like_png()`，第 206-212 行
> - 触发条件判断：`capturer.py` → `_should_use_file_fallback()`，第 186-195 行（仅 `CaptureAssetImage` + 空路径）
> - SSE 错误识别：`capturer.py` → `_is_sse_no_result_error()`，第 198-203 行
> - 主流程编排：`capturer.py` → `_capture_asset_image_with_file_fallback()`，第 302-337 行（try/except 双路径）
> - 调用入口：`capturer.py` → `capture()`，第 123-162 行（viewport 和 editor 回退均走此 helper）

---

## 4. 改动文件清单

| 文件 | 改动说明 |
|------|---------|
| `harness/config.py` | 新增 `ue_project_root` / `ue_screenshot_dir` 字段；`from_env()` 读取 `HARNESS_UE_PROJECT_ROOT` / `HARNESS_UE_SCREENSHOT_DIR`；`merge_cli_overrides()` 支持覆盖，同步 `current` dict；新增 `_env_optional_path()` helper |
| `harness/cli.py` | 新增 `--ue-project-root` / `--ue-screenshot-dir` CLI 参数；修复 `--ue-host` 此前未传入 `merge_cli_overrides()` 的 bug；新增顶层 `Path` import |
| `harness/verification/capturer.py` | **主要改动**。新增 15 个函数/helper：asset 空路径校验、双异常捕获与 fallback、轮询文件发现、netstat/wmic 自动发现、PNG 校验、mtime 过滤。截图超时改为 60s |
| `tests/test_config.py` | +8 tests：路径 env 读取、CLI merge、字段不丢失 |
| `tests/test_verification.py` | +25 tests：fallback 判断、PNG 校验、mtime 选择、双异常处理、mode 语义、轮询命中/未命中 |

测试总数：88 → **113**（+25）

---

## 5. 验证通过的完整 log

```text
# === Harness 启动 ===
21:22:31 截图专用 session 已创建: bf8a496644bb4ae849acff9ee0baf040
21:22:31 自动发现: 端口 8000 上监听的 PID: [25940]
21:22:32 自动发现: 找到 .uproject=D:\...\MCP\MCP.uproject (PID=25940)
21:22:32 截图文件 fallback 目录: D:\...\MCP\Saved\Screenshots\WindowsEditor

# === viewport 截图请求 ===
21:32:27 调用工具: ToolsetRegistry.EditorAppToolset.CaptureAssetImage (id=3)
#   UE 侧: 13:32:28 开始执行 → 13:35:21 截图完成（间隔 2 分 53 秒）

# === fallback 命中 ===
21:35:23 UE screenshot SSE 未返回结果，使用文件 fallback:
          D:\...\Saved\Screenshots\WindowsEditor\ScreenShot00051.png (mtime=2s ago)

# === Vision 闭环 ===
21:35:44 Vision 分析完成: ✅ PASS — 这是Unreal Engine编辑器视口的截图...
21:35:44 Screenshot snapshot 已保存

# === 测试脚本侧 ===
测试 mode='viewport' {'hide_ui': True}
  isError=False, 返回: 'Screenshot 已获取: 1024x630 image/png (mode=viewport)'
  ✅ mode='viewport' VisionInterceptor 已触发
   >>> 上次视觉验证：✅ 通过
```

---

## 6. 文件地图（速查）

| 想找什么 | 位置 |
|---------|------|
| fallback 触发条件（什么情况下走文件 fallback） | `capturer.py:186` `_should_use_file_fallback()` |
| 轮询循环（怎么等文件出现） | `capturer.py:278` `_poll_and_capture()` |
| 文件选择规则（怎么挑最新截图） | `capturer.py:215` `_find_latest_screenshot()` |
| 自动发现入口（怎么找 UE 项目目录） | `capturer.py:441` `_resolve_screenshot_dir()` |
| PID 发现（怎么找到 UE 进程） | `capturer.py:350` `_list_listening_pids()` |
| 命令行提取（怎么拿到 .uproject 路径） | `capturer.py:375` `_get_process_command_line()` |
| 配置字段定义 | `config.py:23-26` `ue_project_root` / `ue_screenshot_dir` |
| CLI 参数定义 | `cli.py:252-258` `--ue-project-root` / `--ue-screenshot-dir` |
| 截图超时值 | `capturer.py:57` `sse_read_timeout=60.0` |
| 轮询超时值 | `capturer.py:274` `_FALLBACK_POLL_SECONDS = 180` |

---

## 7. 未完成 / 后续

| 项目 | 状态 | 说明 |
|------|:---:|------|
| 非 Windows 自动发现 | ❌ | `_discover_ue_project_root_sync()` 仅实现 win32。macOS/Linux 需用 `lsof` + `ps` 实现进程扫描 |
| 远程 UE 支持 | ❌ | 自动发现依赖本机进程扫描。远程 UE 必须通过 `HARNESS_UE_SCREENSHOT_DIR` 或 `HARNESS_UE_PROJECT_ROOT` 手动配置 |
| 用户自定义截图目录 | ⚠️ | 如果用户在 UE Editor Settings 中改了 Screenshot Save Directory，`Saved/Screenshots/WindowsEditor` 拼接会失败。此时必须用 `HARNESS_UE_SCREENSHOT_DIR` 硬覆盖 |
| 轮询 180s 上限 | ⚠️ | 当前保守值。可根据更多 viewport 截图延迟数据调优 |
| Editor Screenshot 超时后无 fallback | ❌ | `CaptureEditorImage()` 是 Slate 窗口合成，不写项目截图目录。当前超时后直接报错 |

---

## 8. 与 HANDOFF_0629 的关系

[HANDOFF_0629](HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md) 是**问题分析文档**——分析了 `CaptureAssetImage` 的两条 UE 分支、为什么 viewport 分支会丢 SSE result、以及为什么文件 fallback 是合理的解决方向。

本文档是**实现完成文档**——记录了实际实现中遇到的额外问题（PowerShell 冷启动、UE 回调延迟 >3 分钟、轮询必要性）和最终的 log 验证结果。两份文档互补，不重复。
