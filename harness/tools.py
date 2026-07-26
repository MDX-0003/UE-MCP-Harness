"""Harness 自有工具分发协议 — ToolContext / HarnessTool / 结果构造器 / 本地事件工具。

涉及的 Issue：002（透传边界）、005（Skill）、015（Vision Session）、016（参考图）、018（注册表化）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from mcp.types import CallToolResult, TextContent

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor

Handler = Callable[["ToolContext", dict[str, Any]], Awaitable[CallToolResult]]

_tlog = logging.getLogger("harness.tools")


# ---- Handler 依赖注入容器 ----


@dataclass
class ToolContext:
    """handler 依赖注入容器——替代 build_server 的闭包捕获参数。

    Issue 020 会将 ref_* 临时字段替换为 ReferenceImageSession。
    """

    config: Any                                    # Config
    ue_client: Any                                 # McpClientSession
    world_state: Any | None                        # WorldState | None
    skill_registry: Any                            # SkillRegistry
    skill_ref: list[dict | None] | None            # 可变列表, [0] = 当前活跃 Skill 或 None
    context_providers: list[Any]                   # list[ContextProvider]
    snapshot_recorder: Any | None
    pending_screenshot_ref: list[Any] | None       # [Screenshot | None]
    vision_session_manager: Any | None             # VisionSessionManager | None
    post_interceptors: list[ToolCallInterceptor] = field(default_factory=list)
    tool_logger: Any | None = None                 # ToolCallLogger 直接引用
    ref_session: Any | None = None                 # ReferenceImageSession | None (Issue 019/020 落地)


# ---- 自有工具注册表项 ----


@dataclass
class HarnessTool:
    """一个 Harness 自有工具——spec 与 handler 同址定义，杜绝漂移。"""

    name: str
    description: str
    input_schema: dict
    handler: Handler

    def to_mcp_tool(self) -> dict:
        """转为 MCP SDK Tool 构造参数。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# ---- 结果构造器（消灭 ~25 处重复）----


def tool_ok(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def tool_fail(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


# ---- 本地工具事件处理 ----


async def emit_local_event(ctx: ToolContext, event: ToolCallCompleted) -> None:
    """对后拦截器全链广播本地工具事件（仅 vision_screenshot 使用）。

    Contract 1 语义：post_call 异常仅记日志，不阻断。
    """
    for ic in ctx.post_interceptors:
        try:
            await ic.post_call(event)
        except Exception as e:
            _tlog.error("后拦截 %s 失败: %s", type(ic).__name__, e)


def log_local_call(
    ctx: ToolContext,
    name: str,
    args: dict,
    result_text: str,
    t0: float,
    error: Exception | None = None,
) -> None:
    """向 ToolCallLogger 写入本地工具调用日志（替代 type(ic).__name__ 字符串匹配）。"""
    if ctx.tool_logger is None:
        return
    duration_ms = (time.monotonic() - t0) * 1000
    event = ToolCallCompleted(
        name=name, args=args,
        raw_result={"content": [{"type": "text", "text": result_text}]},
        parsed_text=result_text,
        error=error, duration_ms=duration_ms,
    )
    try:
        ctx.tool_logger.post_call(event)
    except Exception as e:
        _tlog.error("日志写入失败 %s: %s", name, e)


def require_vision_manager(ctx: ToolContext) -> tuple[Any | None, CallToolResult | None]:
    """返回 (vision_session_manager, error_result)。None 时返回错误。"""
    if ctx.vision_session_manager is None:
        return None, tool_fail("Vision Session Manager 未初始化。")
    return ctx.vision_session_manager, None


# ---- 徽章常量 ----


VISION_BADGES: dict[str, str] = {"high": "🟢", "medium": "🟡", "low": "🔴"}
