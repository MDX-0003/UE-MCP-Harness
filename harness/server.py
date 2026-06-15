"""面向 LLM 的 MCP Server。

使用 `mcp` Python SDK，通过 SSE transport 暴露 Harness 的 MCP 能力。
LLM 连接到此 Server（而非直接连 UE），Harness 在中间做代理。

工具透传模式（P0）：LLM 看到的 tools/list 和 tools/call 直接转发到 UE MCP Server。
后续 Issue 将在此处注入 Context Assembly、Skill 匹配、State Cache 查询。

拦截器链（003+008）：
  ToolCallInterceptor 的 pre/post 钩子挂在此处。每个工具调用穿越拦截器链后到达 UE。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

from harness.client import McpClientSession
from harness.config import Config
from harness.interceptor import ToolCallCompleted, ToolCallInterceptor, DebugPreCallInterceptor

logger = logging.getLogger("harness.server")


def build_server(
    config: Config,
    ue_client: McpClientSession,
    interceptors: list[ToolCallInterceptor] | None = None,
) -> Server:
    """构建 MCP Server 实例。

    Args:
        config: Harness 配置。
        ue_client: 已连接的 UE MCP Client。
        interceptors: ToolCallInterceptor 列表。如果未提供，使用仅含 DebugPreCallInterceptor 的默认列表。

    Returns:
        配置好的 mcp Server 实例。
    """
    server = Server("ue-agent-harness")

    if interceptors is None:
        interceptors = [DebugPreCallInterceptor()]

    # ---- tools/list ----

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """返回 UE 端所有可用工具（P0 阶段全量透传）。

        后续 Issue：
          - #004: Context Assembly — 按模式过滤工具
          - #005: Skill 注入 — 仅暴露 tools_allowlist
        """
        try:
            raw_tools = await ue_client.list_tools()
        except Exception as e:
            logger.error("获取工具列表失败: %s", e)
            return []

        result: list[Tool] = []
        for t in raw_tools:
            name = t.get("name", "")
            description = t.get("description", "")
            input_schema = t.get("inputSchema", {"type": "object"})

            result.append(
                Tool(
                    name=name,
                    description=description or f"UE 工具: {name}",
                    inputSchema=input_schema,
                )
            )

        logger.info("LLM tools/list: 返回 %d 个工具", len(result))
        return result

    # ---- tools/call ----

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """将 LLM 的工具调用穿越拦截器链后透传到 UE，等待结果后通知拦截器。

        Contract 1 — 拦截器调用顺序：
          pre_call (顺序) → UE call_tool → parse result → post_call (顺序)
        """
        t_start = time.monotonic()
        error: Exception | None = None

        # === pre 阶段 ===
        for ic in interceptors:
            try:
                arguments = await ic.pre_call(name, arguments)
            except Exception as e:
                logger.error("预拦截 %s 失败: %s", type(ic).__name__, e)
                error = e
                break

        # === 实际调用 ===
        result_text: str | None = None
        if error is None:
            try:
                result_text = await ue_client.call_tool(name, arguments)
            except Exception as e:
                error = e

        duration_ms = (time.monotonic() - t_start) * 1000

        # === 解析（只做一次，各 interceptor 共享） ===
        parsed_raw = _parse_raw_result(result_text)
        parsed_text = _extract_parsed_text(parsed_raw, result_text)

        # === post 阶段 ===
        event = ToolCallCompleted(
            name=name, args=arguments,
            raw_result=parsed_raw, parsed_text=parsed_text,
            error=error, duration_ms=duration_ms,
        )
        for ic in interceptors:
            try:
                await ic.post_call(event)
            except Exception as e:
                logger.error("后拦截 %s 失败: %s", type(ic).__name__, e)

        if error:
            logger.error("工具调用失败: %s(%s) -> %s", name, arguments, error)
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {error}")],
                isError=True,
            )

        logger.info("LLM tools/call: %s 完成 (%.0fms)", name, duration_ms)
        return CallToolResult(
            content=[TextContent(type="text", text=result_text)]
        )

    return server


def _parse_raw_result(result_text: str | None) -> Any:
    """解析 JSON-RPC result 文本为 Python 对象。"""
    if result_text is None:
        return None
    if isinstance(result_text, str):
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            return result_text
    return result_text


def _extract_parsed_text(parsed_raw: Any, fallback: str | None) -> str | None:
    """从 MCP content array 格式中提取纯文本。

    MCP 规范的工具结果格式：
      {"content": [{"type": "text", "text": "..."}]}
      {"content": [{"type": "image", "data": "base64...", "mimeType": "image/png"}]}

    对于 image 类型，保留 raw_result 但 parsed_text 返回标记字符串。
    """
    if parsed_raw is None:
        return fallback
    if isinstance(parsed_raw, dict):
        content = parsed_raw.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
            if isinstance(item, dict) and item.get("type") == "image":
                return f"[image: {item.get('mimeType', 'unknown')}]"
    # 回退到原始文本
    if fallback is not None:
        return fallback
    return str(parsed_raw)
