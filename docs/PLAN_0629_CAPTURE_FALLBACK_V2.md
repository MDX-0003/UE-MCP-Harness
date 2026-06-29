# PLAN 0629: Screenshot Timeout File Fallback V2

日期：2026-06-29

状态：**已 grill，已整合 G1-G3 修正**

基于 [HANDOFF_0629](HANDOFF_0629_CAPTURE_ASSET_IMAGE_BRANCH_AND_FALLBACK.md) 的三点设计修正，经过 grill 审查后整合三项防御性改进。

**路径约定：** Harness 内部文件相对于 `UE-MCP-Harness` 根目录；UE Engine 文件相对于 `Engine/` 目录；UE 项目路径记为 `{UE_PROJECT_ROOT}`。

---

## 修正 1: 自动发现 UE 项目路径（替代手动 --ue-project-root）

### 问题

原方案要求用户在每次 `harness start` 时通过 `--ue-project-root` 或环境变量 `HARNESS_UE_PROJECT_ROOT` 手动指定 UE 项目路径。但 8000 端口同时只有一个 UE 进程——Harness 已经连接了它，应该能自动发现。

### 方案

Harness 在 `init_shot_session()` 时，通过 OS 进程检测自动发现 UE 项目根目录：

**优先级链：**

| 优先级 | 方式 | 触发条件 |
|--------|------|---------|
| 1 | 进程自动发现 | 默认，`init_shot_session()` 时自动执行一次 |
| 2 | `.env` / 环境变量 `HARNESS_UE_PROJECT_ROOT` | 进程发现失败时的 fallback |
| 3 | CLI `--ue-project-root` | 调试/覆盖用 |

**实现（纯标准库，零新依赖）：**

```python
# 新增函数，放在 harness/verification/capturer.py 中
import subprocess
import sys
from pathlib import Path

def _discover_ue_project_root(ue_port: int = 8000) -> Path | None:
    """通过查询监听 ue_port 的 UE Editor 进程命令行，自动发现项目根目录。

    原理：UE Editor 启动时命令行包含 .uproject 文件路径。
    纯标准库实现，不引入新依赖。

    步骤：
      1. Get-NetTCPConnection 查监听 ue_port 的 PID（结构化输出，跳过文本解析）
      2. Get-CimInstance 查进程命令行
      3. 正则抽 .uproject 路径 → 取其父目录作为项目根
    """
    if sys.platform != "win32":
        # macOS/Linux: lsof + /proc/{pid}/cmdline，P1 再补
        return None

    # 步骤 1: Get-NetTCPConnection 查 PID（结构化 cmdlet，免去 netstat 文本解析的脆弱性）
    ps_pid_cmd = (
        f"Get-NetTCPConnection -LocalPort {ue_port} -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.State -eq 'Listen' }} | "
        f"Select-Object -ExpandProperty OwningProcess"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_pid_cmd],
        capture_output=True, text=True,
    )
    pid = result.stdout.strip()
    if not pid or not pid.isdigit():
        return None

    # 步骤 2: 获取进程命令行
    ps_cmdline_cmd = (
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmdline_cmd],
        capture_output=True, text=True,
    )

    # 步骤 3: 正则抽 .uproject 路径 → 父目录即项目根
    import re
    match = re.search(r'"([^"]+\.uproject)"', result.stdout)
    if not match:
        match = re.search(r'([^\s"]+\.uproject)', result.stdout)
    if match:
        return Path(match.group(1)).parent

    return None
```

**在 `init_shot_session()` 中调用：**

```python
async def init_shot_session(config: object) -> None:
    global _shot_client, _ue_screenshot_dir
    
    # ... 现有 session 创建逻辑 ...
    
    # 自动发现截图目录
    _ue_screenshot_dir = _resolve_screenshot_dir(config)
    if _ue_screenshot_dir:
        logger.info("截图 fallback 目录: %s", _ue_screenshot_dir)
    else:
        logger.info("未配置截图 fallback 目录，超时后将直接报错")
```

`_resolve_screenshot_dir()` 逻辑：

```python
def _resolve_screenshot_dir(config: Config) -> Path | None:
    """按优先级解析截图目录：
    1. 环境变量 HARNESS_UE_SCREENSHOT_DIR（显式配置最高优先）
    2. 环境变量 HARNESS_UE_PROJECT_ROOT → 拼接 Saved/Screenshots/WindowsEditor
    3. 进程自动发现 → 拼接 Saved/Screenshots/WindowsEditor
    """
    # 显式覆盖
    explicit = os.getenv("HARNESS_UE_SCREENSHOT_DIR")
    if explicit:
        return Path(explicit).expanduser()
    
    # 环境变量或进程发现
    project_root = (
        Path(os.getenv("HARNESS_UE_PROJECT_ROOT")).expanduser()
        if os.getenv("HARNESS_UE_PROJECT_ROOT")
        else _discover_ue_project_root(config.ue_port)
    )
    if project_root:
        return project_root / "Saved" / "Screenshots" / "WindowsEditor"
    
    return None
```

### Config 层改动（最小）

`harness/config.py` **不需要**新增 `ue_project_root` 和 `ue_screenshot_dir` 字段。进程发现 + 环境变量读取在 `capturer.py` 内完成，不污染 Config 模型。

---

## 修正 2: fallback 触发条件简化为三种 UE 工具调用

### 问题

原方案表格有 5 行（viewport / editor-fallback / asset-nonempty / asset-empty / raw passthrough），过度设计。

### 方案

**文件 fallback 只在一种情况下触发：`CaptureAssetImage(AssetPath="")`——即 viewport 截图分支。**

| UE 工具调用 | Harness 触发场景 | 写截图文件? | 文件 fallback? |
|------------|-----------------|:---:|:---:|
| `CaptureEditorImage()` | `mode="editor"` | ❌ Slate 合成，不落盘 | ❌ |
| `CaptureAssetImage(AssetPath="")` | `mode="viewport"` 或 editor→viewport fallback | ✅ 触发 `RequestSaveScreenshot` | ✅ **唯一触发** |
| `CaptureAssetImage(AssetPath="/Engine/...")` | `mode="asset"` | ❌ `RenderThumbnail` 直接返像素 | ❌ |

判断函数：

```python
def _should_use_file_fallback(tool_name: str, asset_path: str) -> bool:
    return (
        tool_name == "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
        and not asset_path
    )
```

说明：
- `editor` 模式内部 fallback 到 viewport 时调用的是 `CaptureAssetImage(AssetPath="")`，自然被同一条件覆盖。
- asset 空路径校验（改动 4）独立保留——`mode="asset"` 且 `asset_path` 为空时直接 `ValueError`，不会走到 UE 调用。

---

## 修正 3: 异常类型——`httpx.ReadTimeout` + `JsonRpcError`（SSE 流结束无结果）

### 问题

原方案列了三种异常：`httpx.ReadTimeout`、`asyncio.TimeoutError`、`TimeoutError`。后两种在当前代码路径下不会触发。但反过来——原方案**遗漏**了一条关键的异常路径。

### 两条异常路径对应 UE HTTPServer 的两种行为

`_read_sse_stream`（`harness/client.py:452-526`）有两个不同的"拿不到 result"出口：

**路径 A：socket 超时 → `httpx.ReadTimeout`**

```
Harness                                    UE HTTPServer
  │
  ├─ stream("POST", ...) ─────────────────► ① tools/call
  │  ◄── HTTP 200 + SSE headers            ② 第一次 OnComplete（HasAdditionalWrites）
  │  aiter_lines() 等待中...
  │  ...  sse_read_timeout 到期，socket 上无新字节
  │  httpx.ReadTimeout ◄──                 (final frame 未发出或 TCP 丢包)
```

**路径 B：连接正常关闭但无 result → `JsonRpcError(-32000, "SSE 流结束但未找到工具结果")`**

```
Harness                                    UE HTTPServer
  │
  ├─ stream("POST", ...) ─────────────────► ① tools/call
  │  ◄── HTTP 200 + SSE headers            ② 第一次 OnComplete（HasAdditionalWrites）
  │  aiter_lines() 等待中...
  │  ◄── TCP FIN                           ③ 连接被关闭（HasAdditionalWrites 状态机 bug / UE crash）
  │  aiter_lines() 迭代结束                 但第二次 OnComplete 从未调用
  │  遍历所有行 → 无 "result" key
  │  JsonRpcError(-32000, "SSE 流结束但未找到工具结果") ◄──
```

两条路径的**根因相同**：final SSE result 没到达 Harness。只是 UE HTTPServer 对"没有第二次 OnComplete"的连接有两种处理方式——等超时 vs 主动关连接。

`except httpx.ReadTimeout` **捕获不到** `JsonRpcError`——两者在 Python 异常继承树上没有关系：

```
BaseException
  └── Exception
        ├── JsonRpcError           ← Harness 自定义 (client.py:68)
        └── httpx.HTTPError
              └── httpx.TimeoutException
                    └── httpx.ReadTimeout   ← httpx 库
```

### 方案

捕获两种异常，`JsonRpcError` 匹配 code + message 判断是否为 "SSE 流结束无结果"：

```python
import httpx
from harness.client import JsonRpcError

start_wall = time.time()
try:
    result = await _shot_client.call_tool(
        "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
        {"AssetPath": path, "bShowUI": b_show_ui},
    )
    return parse_screenshot(result, max_width, max_height)
except httpx.ReadTimeout:
    pass  # 路径 A：socket 超时 → 尝试 fallback
except JsonRpcError as e:
    if e.code == -32000 and "未找到工具结果" in e.message:
        pass  # 路径 B：SSE 流结束无 result → 同样的 fallback
    else:
        raise  # 其他 JsonRpcError（如工具执行失败）→ 不透传
else:
    return  # 正常拿到 result，上面的 except 不会执行

# 两条异常路径在此汇聚，统一尝试文件 fallback
if _should_use_file_fallback(tool_name, path):
    if _ue_screenshot_dir and _ue_screenshot_dir.is_dir():
        latest = _find_latest_screenshot(_ue_screenshot_dir, since=start_wall - 2.0)
        if latest:
            try:
                return capture_from_file(latest, max_width, max_height)
            except Exception as file_err:
                logger.warning("File fallback read failed: %s (file=%s)",
                               file_err, latest)
raise  # fallback 不适用 / 未配置目录 / 找不到新文件 / 文件损坏 → 重抛原始异常
```

### 为什么不用 `httpx.ReadTimeout` 和 `JsonRpcError` 的公共父类？

`JsonRpcError` 的父类是 `Exception`，`httpx.ReadTimeout` 也是 `Exception`。`except Exception` 太宽——会把 `ValueError`（参数校验）、`RuntimeError`（session 断连）等不该 fallback 的场景也吞掉。显式列出两种异常类型是精确的。

---

## 文件变更清单

| 文件 | 改动 | 程度 |
|------|------|------|
| `harness/verification/capturer.py` | ① `_discover_ue_project_root()` — 进程自动发现；② `_resolve_screenshot_dir()` — 三级优先级；③ `capture()` viewport 分支 — 双异常 catch + 文件 fallback；④ asset 空路径校验；⑤ `_find_latest_screenshot()` — 按时间过滤，只取 `*.png`、mtime >= request_start - 2s | 核心改动 |
| `harness/config.py` | **不改**（进程发现 + env var 读取在 capturer 内完成，不污染 Config 模型） | 不变 |
| `harness/cli.py` | **不改**（自动发现，无需新增 CLI 参数。`--ue-project-root` 可 P1 再加） | 不变 |
| `harness/client.py` | **不改**（`JsonRpcError` 已存在于 `client.py:68`，capturer 只需 import） | 不变 |
| `tests/test_verification.py` | 新增：进程发现 mock 测试、`JsonRpcError` fallback 测试、文件不存在/损坏回退测试、asset 空路径校验测试 | 新增测试 |

---

## 伪代码：capturer.py `capture()` 完整流程

```python
import httpx
import time
from harness.client import JsonRpcError

async def capture(ue_client, max_width=1024, max_height=768, *,
                  mode="viewport", asset_path="", hide_ui=False) -> Screenshot:
    if mode not in ("viewport", "editor", "asset"):
        raise ValueError(...)
    if mode == "asset" and not asset_path:
        raise ValueError("mode='asset' requires non-empty asset_path")
    if _shot_client is None or not _shot_client.is_connected:
        raise RuntimeError("截图 session 未初始化")

    b_show_ui = not hide_ui

    async with _shot_lock:
        if mode == "editor":
            # CaptureEditorImage 不写文件，不需要 fallback
            try:
                result = await _shot_client.call_tool(
                    "ToolsetRegistry.EditorAppToolset.CaptureEditorImage", {})
                if result:
                    return parse_screenshot(result, max_width, max_height)
            except Exception as e:
                log_exception(e, "capturer editor→viewport fallback")
            # fall through to viewport

        # viewport / asset — 统一路径
        path = "" if mode == "viewport" else asset_path
        tool_name = "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
        args = {"AssetPath": path, "bShowUI": b_show_ui}

        start_wall = time.time()
        try:
            result = await _shot_client.call_tool(tool_name, args)
            return parse_screenshot(result, max_width, max_height)
        except httpx.ReadTimeout:
            pass  # 路径 A: socket 超时 → 尝试 fallback
        except JsonRpcError as e:
            if e.code == -32000 and "未找到工具结果" in e.message:
                pass  # 路径 B: SSE 流结束无 result → 同样 fallback
            else:
                raise

        # 两条异常路径在此汇聚，只有 viewport 分支才尝试文件 fallback
        if not path:  # path 为空 = viewport 分支
            if _ue_screenshot_dir and _ue_screenshot_dir.is_dir():
                latest = _find_latest_screenshot(
                    _ue_screenshot_dir, since=start_wall - 2.0)
                if latest:
                    try:
                        logger.warning(
                            "SSE 未返回截图, 使用文件 fallback: %s (mtime=%s)",
                            latest.name, latest.stat().st_mtime)
                        return capture_from_file(latest, max_width, max_height)
                    except Exception as file_err:
                        logger.warning(
                            "文件 fallback 读取失败: %s (file=%s)",
                            file_err, latest)
        raise  # 重抛原始异常
```

---

## 风险与边界

1. **进程发现不是 100% 可靠**：PowerShell 权限受限、UE 通过跳板启动等情况可能导致 PID 发现失败。此时退化到 env var / CLI 或直接无 fallback。`Get-NetTCPConnection` 需要 PowerShell 5.0+（Win10+/Server 2016+，当前开发环境满足）。
2. **跨平台**：当前只实现 Windows。macOS/Linux 上 `_discover_ue_project_root()` 返回 `None`，需 P1 补充 `lsof -i :{port} -sTCP:LISTEN -t` + `/proc/{pid}/cmdline` 方案。
3. **文件 fallback 读损坏**（G1）：`capture_from_file` 外包 `try/except`，文件读取失败时 log warning 并重抛原始 SSE 异常——不让 `OSError` 吞噬 `httpx.ReadTimeout`/`JsonRpcError`。
4. **SSE 流关闭情况**（G2）：`except` 覆盖 `httpx.ReadTimeout` + `JsonRpcError(-32000, "SSE 流结束但未找到工具结果")` 两条路径。UE HTTPServer 在第二次 OnComplete 前主动关闭连接的情况也能触发 fallback。
5. **不引入新依赖**：进程发现使用 `subprocess` + PowerShell cmdlet，全部是 OS 内置工具。
