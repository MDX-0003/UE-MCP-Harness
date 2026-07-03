"""截图获取 — 通过 MCP 调用 EditorAppToolset.CaptureEditorImage() 或 CaptureAssetImage()。

FToolsetImage 返回 MimeType: "image/png" + Data: base64，Harness 零转码直传 Vision API。
截图在发送前 resize 到最大 config.vision_max_size（默认 1024x768），保持宽高比。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from harness.client import McpClientSession
    from harness.config import Config as _ConfigType

from harness.client import JsonRpcError

logger = logging.getLogger("harness.verification.capturer")

# 截图专用持久 session + 串行锁
# init_shot_session() 在 Harness 启动时调用一次；
# 所有 capture() 调用复用同一 session；
# close_shot_session() 在 Harness 关闭时调用。
_shot_client: "McpClientSession | None" = None
_shot_lock = asyncio.Lock()

# 文件 fallback 截图目录（init_shot_session 时自动发现并缓存一次）
_ue_screenshot_dir: "Path | None" = None


async def init_shot_session(config: object) -> None:
    """创建截图专用持久 MCP session。可重复调用——内部先关闭旧 session 再重建。

    使用独立 session 避免与主 session 的 TCP 连接池状态串扰；
    持久复用避免频繁 connect/close/DELETE 对 UE HTTPServer 状态机的刺激。

    同时执行一次截图目录自动发现并缓存结果（失败不阻断启动）。
    """
    global _shot_client, _ue_screenshot_dir

    # 幂等：已存在则先关闭
    if _shot_client is not None:
        await close_shot_session()

    from harness.client import McpClientSession as _McpClientSession
    from harness.config import Config as _Config

    shot_config = _Config(
        ue_port=config.ue_port,  # type: ignore[attr-defined]
        ue_host=config.ue_host,  # type: ignore[attr-defined]
        sse_read_timeout=60.0,  # type: ignore[attr-defined] # 截图超时 60s
        preload_all_toolsets=False,
    )
    _shot_client = _McpClientSession(shot_config)
    await _shot_client.connect()
    logger.info("截图专用 session 已创建: %s", _shot_client.session_id)

    # 自动发现截图目录（失败仅记录 warning，不阻断启动）
    _ue_screenshot_dir = await _resolve_screenshot_dir(config)
    if _ue_screenshot_dir is not None:
        logger.info("截图文件 fallback 目录: %s", _ue_screenshot_dir)
    else:
        logger.info("截图文件 fallback 目录未配置或自动发现失败，文件 fallback 已禁用")


async def close_shot_session() -> None:
    """关闭截图专用 session。Harness 关闭时调用。"""
    global _shot_client
    if _shot_client is not None:
        await _shot_client.close()
        _shot_client = None
        logger.info("截图专用 session 已关闭")


@dataclass
class Screenshot:
    """截图结果。"""
    data_b64: str          # base64 编码的 PNG 数据
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0


async def capture(
    ue_client: McpClientSession,
    max_width: int = 1024,
    max_height: int = 768,
    *,
    mode: str = "viewport",
    asset_path: str = "",
    hide_ui: bool = False,
) -> Screenshot:
    """通过 MCP 获取 UE 编辑器截图。

    使用截图专用持久 session + asyncio.Lock 串行化。
    绕过了 UE HTTP Server MultipleWriteStream 在频繁 session
    churn 下的跨线程异步数据丢失问题。

    mode 参数选择截图方案：
      - "viewport": CaptureAssetImage(AssetPath="") — 仅视口画面
      - "editor":   CaptureEditorImage() — 合成编辑器窗口
      - "asset":    CaptureAssetImage(AssetPath=<path>) — 资产缩略图
    """
    if mode not in ("viewport", "editor", "asset"):
        raise ValueError(f"无效的截图模式: {mode}，可选 viewport / editor / asset")
    if mode == "asset" and not asset_path:
        raise ValueError(
            "mode='asset' requires non-empty asset_path; "
            "empty path captures the viewport, use mode='viewport' instead"
        )

    if _shot_client is None:
        raise RuntimeError("截图 session 未初始化，请重启 Harness")
    # _connected 检查已移除 — _ensure_connected() (Issue 012) 在 call_tool()
    # 内部处理重连，外部守卫会阻断急救路径。

    b_show_ui = not hide_ui

    async with _shot_lock:
        # 截图前激活 UE 窗口，确保 DWM 正常合成、viewport 渲染帧
        if _shot_client is not None:
            _activate_ue_window(_shot_client.ue_port)

        if mode == "editor":
            try:
                result = await _shot_client.call_tool(
                    "ToolsetRegistry.EditorAppToolset.CaptureEditorImage", {}
                )
                if result:
                    return parse_screenshot(result, max_width, max_height)
            except Exception as e:
                from harness.verification.debug import log_exception
                log_exception(e, "capturer editor→viewport fallback")
                logger.debug("CaptureEditorImage 失败，回退到 viewport 模式")

            # editor 失败后必须强制 viewport（AssetPath=""）。
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

        # mode == "asset"，上方已校验 asset_path 非空，不进入文件 fallback。
        result = await _shot_client.call_tool(
            "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
            {"AssetPath": asset_path, "bShowUI": b_show_ui},
        )
        return parse_screenshot(result, max_width, max_height)


def capture_from_file(path: Path, max_width: int = 1024, max_height: int = 768) -> Screenshot:
    """从本地文件读取截图（用于测试和离线 replay）。"""
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")

    # 获取尺寸（优先 PIL，fallback PNG header）
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w > max_width or h > max_height:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            w, h = img.size
        return Screenshot(data_b64=b64, width=w, height=h)
    except ImportError:
        logger.debug("PIL 未安装，尝试 PNG header 解析尺寸")
        dims = _parse_png_dimensions(data)
        if dims:
            return Screenshot(data_b64=b64, width=dims[0], height=dims[1])
        return Screenshot(data_b64=b64)
    except Exception as e:
        logger.warning("PIL resize 失败: %s，尝试 PNG header fallback", e)
        dims = _parse_png_dimensions(data)
        if dims:
            return Screenshot(data_b64=b64, width=dims[0], height=dims[1])
        return Screenshot(data_b64=b64)


# ---- 窗口激活辅助（解决后台窗口不渲染帧导致截图超时） -------------------


def _activate_ue_window(port: int) -> bool:
    """在截图前激活 UE 编辑器窗口，确保 viewport 被 DWM 正常合成。

    根因：UE viewport 截图通过 FScreenshotRequest 设置 flag，依赖下一帧渲染。
    但当 UE 窗口在后台时，Windows DWM 可能节流/跳过 GPU 合成，导致
    FViewport::Draw() 不被调用、ProcessScreenShots() 永远检查不到 flag。

    此函数查找 UE 进程的主窗口并尝试恢复+置顶。
    仅在 Windows 平台有效，失败静默返回 False 不阻断截图流程。
    """
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 找到监听 UE 端口的进程 PID
    pids = _list_listening_pids(port)
    if not pids:
        logger.debug("窗口激活跳过: 未找到监听端口 %d 的进程", port)
        return False
    target_pid = pids[0]

    # 枚举顶层窗口，找到匹配 PID 的可见窗口
    found_windows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == lparam:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                found_windows.append((hwnd, buf.value))
        return True

    enum_proc = WNDENUMPROC(_enum_callback)
    user32.EnumWindows(enum_proc, wintypes.LPARAM(target_pid))

    if not found_windows:
        logger.debug("窗口激活跳过: PID=%d 无可见窗口", target_pid)
        return False

    # 优先选择标题最大的窗口（UE 编辑器主窗口）
    found_windows.sort(key=lambda x: len(x[1]), reverse=True)
    hwnd, title = found_windows[0]
    logger.debug("窗口激活: HWND=%s 标题=%s PID=%d", hwnd, title, target_pid)

    # 如果窗口已最小化，先恢复
    SW_RESTORE = 9
    SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        logger.debug("窗口激活: 从最小化恢复")

    # 解除前台窗口锁定（ASFW_ANY = 0xFFFFFFFF），允许本进程调 SetForegroundWindow
    user32.AllowSetForegroundWindow(-1)
    # 置顶 + 设为前台窗口
    HWND_TOP = 0
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    # 给 DWM 一点时间恢复合成（SetForegroundWindow 有异步限制）
    time.sleep(0.15)
    return True


# ---- 文件 fallback 辅助函数 ------------------------------------------------


def _should_use_file_fallback(tool_name: str, asset_path: str) -> bool:
    """仅 viewport 截图分支（CaptureAssetImage 空路径）启用文件 fallback。

    asset thumbnail 不写项目截图目录，不能使用文件 fallback。
    CaptureEditorImage 是 Slate 窗口合成，也不写项目截图目录。
    """
    return (
        tool_name == "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
        and asset_path == ""
    )


def _is_sse_no_result_error(exc: JsonRpcError) -> bool:
    """SSE 流正常结束但未包含工具结果的特定错误。"""
    return (
        exc.code == -32000
        and "SSE 流结束但未找到工具结果" in exc.message
    )


def _looks_like_png(path: Path) -> bool:
    """最小 PNG header 校验——检查 magic bytes。"""
    try:
        with path.open("rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _find_latest_screenshot(directory: Path, since: float) -> Path | None:
    """在截图目录中查找本次请求开始后产生的最新 PNG。

    多层 guard：
      - 仅 *.png
      - 跳过空文件
      - mtime 必须在 [since, now+2s] 窗口内
      - 多候选取 mtime 最新
    """
    candidates: list[tuple[float, Path]] = []
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


async def _try_file_fallback(
    tool_name: str,
    asset_path: str,
    start_wall: float,
    max_width: int,
    max_height: int,
) -> "Screenshot | None":
    """尝试从 UE 项目截图目录读取最新文件作为 fallback。

    超时后轮询等待文件出现（最多额外等 _FALLBACK_POLL_SECONDS 秒），
    因为 UE viewport 截图可能比 Harness SSE timeout 更晚落盘。
    找不到合适文件或读取失败时返回 None，
    由调用方在 except block 内重新 raise 原始异常。
    """
    if not _should_use_file_fallback(tool_name, asset_path):
        logger.info("文件 fallback 跳过: tool=%s asset_path=%r 不符合触发条件",
                     tool_name, asset_path)
        return None
    if _ue_screenshot_dir is None:
        logger.info("文件 fallback 跳过: _ue_screenshot_dir 为 None（自动发现失败或未配置）")
        return None
    if not _ue_screenshot_dir.is_dir():
        logger.info("文件 fallback 跳过: 目录不存在 %s", _ue_screenshot_dir)
        return None

    return await _poll_and_capture(_ue_screenshot_dir, start_wall, max_width, max_height)


# 超时后额外轮询等待文件的时间（秒）
_FALLBACK_POLL_SECONDS = 180  # viewport 截图可能 3 分钟以上
_FALLBACK_POLL_INTERVAL = 3   # 每 3 秒检查一次


async def _poll_and_capture(
    directory: Path,
    start_wall: float,
    max_width: int,
    max_height: int,
) -> "Screenshot | None":
    """轮询等待 UE 截图文件出现，找到后读取返回。"""
    deadline = time.time() + _FALLBACK_POLL_SECONDS

    while time.time() < deadline:
        latest = _find_latest_screenshot(directory, since=start_wall - 2.0)
        if latest is not None and _looks_like_png(latest):
            try:
                logger.warning(
                    "UE screenshot SSE 未返回结果，使用文件 fallback: %s (mtime=%.0fs ago)",
                    latest, time.time() - latest.stat().st_mtime,
                )
                return capture_from_file(latest, max_width, max_height)
            except Exception as file_err:
                logger.warning("文件 fallback 读取失败: %s (file=%s)", file_err, latest)
                return None
        await asyncio.sleep(_FALLBACK_POLL_INTERVAL)

    logger.info("文件 fallback 轮询超时 (%.0fs): 在 %s 中未找到新 PNG",
                _FALLBACK_POLL_SECONDS, directory)
    return None


async def _capture_asset_image_with_file_fallback(
    *,
    asset_path: str,
    b_show_ui: bool,
    max_width: int,
    max_height: int,
) -> Screenshot:
    """调用 CaptureAssetImage 并带 timeout / SSE 无结果时的文件 fallback。

    仅在 asset_path 为空（viewport 语义）时启用 fallback。
    """
    tool_name = "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
    args = {"AssetPath": asset_path, "bShowUI": b_show_ui}
    start_wall = time.time()

    # 截图前激活 UE 窗口，确保 DWM 正常合成、viewport 渲染帧
    if _shot_client is not None:
        _activate_ue_window(_shot_client.ue_port)

    try:
        result = await _shot_client.call_tool(tool_name, args)
        return parse_screenshot(result, max_width, max_height)
    except httpx.ReadTimeout:
        screenshot = await _try_file_fallback(
            tool_name, asset_path, start_wall, max_width, max_height
        )
        if screenshot is not None:
            return screenshot
        raise
    except JsonRpcError as exc:
        if not _is_sse_no_result_error(exc):
            raise
        screenshot = await _try_file_fallback(
            tool_name, asset_path, start_wall, max_width, max_height
        )
        if screenshot is not None:
            return screenshot
        raise


# ---- 自动发现 UE 项目根目录（Windows） ------------------------------------


def _is_local_host(host: str) -> bool:
    """检查 host 是否指向本机。"""
    return host in ("127.0.0.1", "localhost", "::1")


def _list_listening_pids(port: int) -> list[int]:
    """通过 netstat 查询监听指定端口的进程 PID 列表（去重）。

    使用 netstat 而非 PowerShell 避免 .NET 冷启动延迟（首次调用可 >3s）。
    """
    try:
        proc = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.info("netstat 超时")
        return []
    except FileNotFoundError:
        logger.info("netstat 不可用")
        return []

    pattern = f":{port}"
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        # 只匹配 LISTENING 状态的连接
        if "LISTENING" not in line.upper():
            continue
        if pattern not in line:
            continue
        parts = line.split()
        # netstat -ano 输出格式: Proto  Local Address  Foreign Address  State  PID
        # 最后一列是 PID
        if parts:
            pid_str = parts[-1].strip()
            if pid_str.isdigit():
                pids.append(int(pid_str))
    return list(dict.fromkeys(pids))  # 去重保序


def _get_process_command_line(pid: int) -> str | None:
    """通过 wmic 查询进程的命令行（无需 PowerShell，无冷启动延迟）。"""
    try:
        proc = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # wmic 不可用时回退到 PowerShell（更长超时）
        return _get_process_command_line_ps(pid)

    if proc.returncode != 0:
        return _get_process_command_line_ps(pid)

    # wmic 输出第一行是 "CommandLine"，第二行是实际值
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    for line in lines:
        if line.lower() == "commandline":
            continue
        # wmic 使用 UTF-16 编码，可能会有 null 字节残留
        return line.replace("\x00", "")
    return _get_process_command_line_ps(pid)


def _get_process_command_line_ps(pid: int) -> str | None:
    """通过 PowerShell 查询进程命令行（回退方案，有冷启动延迟）。"""
    script = (
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,  # PowerShell 冷启动更长
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    output = proc.stdout.strip()
    return output or None


def _extract_uproject_from_command_line(cmdline: str) -> Path | None:
    """从进程命令行中提取 .uproject 文件的路径。

    先匹配引号内路径，再匹配无引号路径。
    """
    match = re.search(r'"([^"]+\.uproject)"', cmdline)
    if not match:
        match = re.search(r'([^\s"]+\.uproject)', cmdline)
    if not match:
        return None
    return Path(match.group(1))


def _discover_ue_project_root_sync(config: object) -> Path | None:
    """同步执行 Windows 进程扫描，发现当前 UE 进程的项目根目录。

    仅当 ue_host 为本机且平台为 win32 时执行。
    返回 .uproject 所在目录，失败返回 None。
    """
    if sys.platform != "win32":
        logger.info("自动发现跳过: 非 Windows 平台 (%s)", sys.platform)
        return None
    ue_host: str = getattr(config, "ue_host", "127.0.0.1")
    ue_port: int = getattr(config, "ue_port", 8000)
    if not _is_local_host(ue_host):
        logger.info("自动发现跳过: ue_host=%s 不是本机地址", ue_host)
        return None

    pids = _list_listening_pids(ue_port)
    logger.info("自动发现: 端口 %d 上监听的 PID: %s", ue_port, pids)
    for pid in pids:
        cmdline = _get_process_command_line(pid)
        if not cmdline:
            logger.info("自动发现: PID %d 无法获取命令行", pid)
            continue
        project = _extract_uproject_from_command_line(cmdline)
        if project and project.is_file():
            logger.info("自动发现: 找到 .uproject=%s (PID=%d)", project, pid)
            return project.parent
        logger.info("自动发现: PID %d 命令行中未找到 .uproject: %.200s", pid, cmdline)
    logger.info("自动发现: 未找到 UE 项目根目录（端口 %d 上 %d 个 PID 均不含 .uproject）",
                ue_port, len(pids))
    return None


async def _resolve_screenshot_dir(config: object) -> Path | None:
    """解析截图文件 fallback 目录。

    优先级（见 PLAN_0629）：
      0. HARNESS_UE_SCREENSHOT_DIR（硬覆盖）
      1. 自动发现（默认主路径）
      2. HARNESS_UE_PROJECT_ROOT + Saved/Screenshots/WindowsEditor（救援路径）
      3. 返回 None，禁用文件 fallback
    """
    ue_screenshot_dir: Path | None = getattr(config, "ue_screenshot_dir", None)
    if ue_screenshot_dir is not None:
        return ue_screenshot_dir.resolve()

    discovered = await asyncio.to_thread(_discover_ue_project_root_sync, config)
    if discovered is not None:
        return discovered / "Saved" / "Screenshots" / "WindowsEditor"

    ue_project_root: Path | None = getattr(config, "ue_project_root", None)
    if ue_project_root is not None:
        return ue_project_root.resolve() / "Saved" / "Screenshots" / "WindowsEditor"

    return None


def parse_screenshot(raw: str, max_width: int = 1024, max_height: int = 768) -> Screenshot:
    """解析 MCP 返回的截图数据并 resize。

    这是截图数据提取的**唯一入口**——CLI、VisionInterceptor、SnapshotRecorder 三条路径均通过此函数汇聚。
    接受原始 JSON-RPC result 字符串或已序列化的 dict，支持 6 种格式。纯函数，零副作用。

    MCP 可能返回两种格式：
      1. image content block: {"content": [{"type": "image", "data": "...", "mimeType": "image/png"}]}
      2. text content block 中嵌 base64: {"content": [{"type": "text", "text": "<base64>"}]}
      3. 直接是 base64 字符串（无 JSON 包装）
    """
    import json
    import re

    b64_data = raw

    # 尝试解析 JSON
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            # 检查是否为错误响应
            if parsed.get("isError"):
                content = parsed.get("content", [])
                err_text = ""
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        err_text = item.get("text", "")
                        break
                raise ValueError(f"UE 截图失败: {err_text}")

            content = parsed.get("content", [])
            for item in content:
                if not isinstance(item, dict):
                    continue
                # 格式 1: image block
                if item.get("type") == "image":
                    b64_data = item.get("data", "")
                    if b64_data:
                        break
                # 格式 2: text block 中可能嵌了 base64
                if item.get("type") == "text":
                    text = item.get("text", "")

                    # 格式 2a: UE 工具返回嵌套 JSON — {"returnValue":{"mimeType":"...","data":"<base64>"}}
                    if text.lstrip().startswith("{") and "returnValue" in text:
                        try:
                            inner = json.loads(text)
                            rv = inner.get("returnValue", {})
                            if isinstance(rv, dict) and rv.get("data"):
                                b64_data = rv["data"]
                                logger.debug("从嵌套 returnValue JSON 提取 base64，长度=%d", len(b64_data))
                                break
                        except json.JSONDecodeError:
                            pass  # 继续尝试其他格式

                    # 格式 2b: data URI — data:image/png;base64,...
                    data_uri_match = re.search(
                        r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', text
                    )
                    if data_uri_match:
                        b64_data = data_uri_match.group(1)
                        logger.debug("从 data URI 提取 base64，长度=%d", len(b64_data))
                        break

                    # 格式 2c: 纯 base64 字符串
                    cleaned = re.sub(r'\s+', '', text)
                    if _looks_like_base64(cleaned):
                        b64_data = cleaned
                        logger.debug("从 text 提取纯 base64，长度=%d", len(b64_data))
                        break
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # 如果 b64_data 还是原始 raw，尝试直接从 raw 中提取
    if b64_data == raw:
        # 尝试 data URI
        data_uri_match = re.search(
            r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', raw
        )
        if data_uri_match:
            b64_data = data_uri_match.group(1)
            logger.debug("从原始文本提取 data URI base64，长度=%d", len(b64_data))

    # 验证 base64 有效性
    cleaned = re.sub(r'\s+', '', b64_data)
    if len(cleaned) % 4 != 0:
        # 补齐 padding
        cleaned += '=' * (4 - len(cleaned) % 4)
    b64_data = cleaned

    # Resize
    try:
        data = base64.b64decode(b64_data)
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w > max_width or h > max_height:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_data = base64.b64encode(buf.getvalue()).decode("ascii")
            w, h = img.size
        return Screenshot(data_b64=b64_data, width=w, height=h)
    except ImportError:
        logger.debug("PIL 未安装，尝试 PNG header 解析尺寸")
        try:
            data = base64.b64decode(b64_data)
            dims = _parse_png_dimensions(data)
            if dims:
                return Screenshot(data_b64=b64_data, width=dims[0], height=dims[1])
        except Exception:
            pass
        return Screenshot(data_b64=b64_data)
    except Exception as e:
        logger.warning("Resize 失败: %s，尝试 PNG header fallback", e)
        try:
            data = base64.b64decode(b64_data)
            dims = _parse_png_dimensions(data)
            if dims:
                return Screenshot(data_b64=b64_data, width=dims[0], height=dims[1])
        except Exception:
            pass
        return Screenshot(data_b64=b64_data)


def _parse_png_dimensions(data: bytes) -> tuple[int, int] | None:
    """从 PNG 原始字节中解析宽度和高度（不依赖 PIL）。

    PNG 文件格式：
      Bytes 0-7:   89 50 4E 47 0D 0A 1A 0A (signature)
      Bytes 8-11:  IHDR chunk length (big-endian, always 13)
      Bytes 12-15: "IHDR"
      Bytes 16-19: width  (big-endian uint32)
      Bytes 20-23: height (big-endian uint32)

    返回 (width, height) 或 None（如果不是 PNG）。
    """
    if len(data) < 24:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    import struct
    w = struct.unpack(">I", data[16:20])[0]
    h = struct.unpack(">I", data[20:24])[0]
    return (w, h)


def _looks_like_base64(s: str) -> bool:
    """判断字符串是否看起来像 base64 编码的二进制数据。"""
    import re
    if not s or len(s) < 20:
        return False
    # base64 字符集 + 可选 padding
    return bool(re.match(r'^[A-Za-z0-9+/]+=*$', s))
