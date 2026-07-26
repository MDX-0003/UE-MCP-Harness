"""Vision 工具组 handlers — vision_screenshot / vision_ask / vision_tell / vision_reset / vision_status。

涉及的 Issue：015（Vision Session）、018（注册表化）。
"""

from __future__ import annotations

import time
from typing import Any

from mcp.types import CallToolResult

from harness.tools import (
    ToolContext, log_local_call, emit_local_event,
    tool_ok, tool_fail, require_vision_manager, VISION_BADGES,
)
from harness.interceptor import ToolCallCompleted
from harness.verification.capturer import capture_screenshot as _capture_screenshot
from harness.verification.debug import log_exception as _log_exception


async def handle_vision_screenshot(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """vision_screenshot handler — 截图 + 追加到 Vision Session。"""
    t0 = time.monotonic()
    try:
        mode = arguments.get("mode", "viewport")
        asset_path = arguments.get("asset_path", "")
        hide_ui = arguments.get("hide_ui", False)
        max_w, max_h = ctx.config.vision_max_size
        screenshot = await _capture_screenshot(
            ctx.ue_client, max_w, max_h,
            mode=mode, asset_path=asset_path, hide_ui=hide_ui,
        )
        if ctx.pending_screenshot_ref is not None:
            ctx.pending_screenshot_ref[0] = screenshot
        result_text = (
            f"Screenshot 已获取: {screenshot.width}x{screenshot.height}"
            f" {screenshot.mime_type} (mode={mode})"
        )
    except Exception as e:
        if ctx.pending_screenshot_ref is not None:
            ctx.pending_screenshot_ref[0] = None
        _log_exception(e, "vision_screenshot")
        return tool_fail(f"截图失败: {type(e).__name__}: {e}")

    # 手动触发 post 拦截器链 — 不经过 UE 透传路径，
    # 必须在此处让 VisionInterceptor / SnapshotRecorder 消费截图结果
    duration_ms = (time.monotonic() - t0) * 1000
    event = ToolCallCompleted(
        name="vision_screenshot",  # 保留原始工具名供 interceptor 路由
        args=arguments,
        raw_result={"content": [{"type": "text", "text": result_text}]},
        parsed_text=result_text,
        error=None,
        duration_ms=duration_ms,
    )
    await emit_local_event(ctx, event)

    # 008 / 007 / 015: 将 Vision 分析结果 + Session 状态追加到返回值
    vision_info = ""
    if ctx.world_state is not None and ctx.world_state.last_vision_verdict:
        v = ctx.world_state.last_vision_verdict
        confidence = v.get("confidence", "medium")
        badge = VISION_BADGES.get(confidence, "🟡")
        answer = v.get("answer", "")
        caveats = v.get("caveats", [])
        observations = v.get("observations", [])

        parts = ["\n\n[Vision 分析]"]
        parts.append(f"置信度: {badge} {confidence}")
        parts.append(f"回答: {answer if answer else '（Vision 返回为空或格式异常）'}")
        if caveats:
            parts.append(f"限制: {'; '.join(caveats[:3])}")
        if observations:
            obs_lines = []
            obs_badges = {"high": "✓", "medium": "~", "low": "?"}
            for o in observations[:8]:
                ob = obs_badges.get(o.get("confidence", ""), "~")
                obs_lines.append(
                    f"  {ob} {o.get('what', '')}: {o.get('finding', '')}"
                )
            parts.append("分项观察:\n" + "\n".join(obs_lines))
        vision_info = "\n".join(parts)

    # Issue 015: 注入 Session 状态和过期警告
    if ctx.vision_session_manager is not None:
        session = ctx.vision_session_manager.get_active()
        if session is not None:
            session_info = (
                f"\n\nSession: {session.id} "
                f"(截图 #{session.screenshot_count}，"
                f"累计 {session.question_count} 次提问)"
            )
            result_text += session_info
            warning = ctx.vision_session_manager.check_warning()
            if warning:
                vision_info = warning + "\n" + vision_info if vision_info else warning

    return tool_ok(result_text + vision_info)


async def handle_vision_ask(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """vision_ask handler — 向 Vision Session 追加定向提问。"""
    t0 = time.monotonic()
    manager, error_result = require_vision_manager(ctx)
    if error_result is not None:
        return error_result
    question = arguments.get("question", "")
    if not question.strip():
        return tool_fail("question 参数不能为空。")
    try:
        verdict = await manager.ask(question)
    except ValueError as e:
        err_text = str(e)
        log_local_call(ctx, "vision_ask", arguments, err_text, t0, error=ValueError(err_text))
        return tool_fail(err_text)
    warning = manager.check_warning()
    result_text = verdict.reason
    if warning:
        result_text = warning + "\n\n" + result_text
    log_local_call(ctx, "vision_ask", arguments, result_text, t0)
    return tool_ok(result_text)


async def handle_vision_tell(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """vision_tell handler — 注入非截图上下文到 Vision Session。"""
    t0 = time.monotonic()
    manager, error_result = require_vision_manager(ctx)
    if error_result is not None:
        return error_result
    info = arguments.get("info", "")
    if not info.strip():
        return tool_fail("info 参数不能为空。")
    manager.tell(info)
    result_text = f"已注入上下文到 Vision Session（{len(info)} 字符）。"
    log_local_call(ctx, "vision_tell", arguments, result_text, t0)
    return tool_ok(result_text)


async def handle_vision_reset(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """vision_reset handler — 关闭当前 Vision Session 并创建新 Session。"""
    t0 = time.monotonic()
    manager, error_result = require_vision_manager(ctx)
    if error_result is not None:
        return error_result
    old_session = manager.get_active()
    manager.reset()
    if old_session:
        result_text = f"Vision Session {old_session.id} 已关闭并归档。新 Session 已创建。"
    else:
        result_text = "新 Vision Session 已创建。"
    log_local_call(ctx, "vision_reset", arguments, result_text, t0)
    return tool_ok(result_text)


async def handle_vision_status(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """vision_status handler — 返回当前 Vision Session 状态摘要。"""
    t0 = time.monotonic()
    manager, error_result = require_vision_manager(ctx)
    if error_result is not None:
        return error_result
    result_text = manager.status_text()
    log_local_call(ctx, "vision_status", arguments, result_text, t0)
    return tool_ok(result_text)
