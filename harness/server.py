"""面向 LLM 的 MCP Server。

使用 `mcp` Python SDK，通过 SSE transport 暴露 Harness 的 MCP 能力。
LLM 连接到此 Server（而非直接连 UE），Harness 在中间做代理。

工具透传模式（P0）：LLM 看到的 tools/list 和 tools/call 直接转发到 UE MCP Server。
后续 Issue 将在此处注入 Context Assembly、Skill 匹配、State Cache 查询。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

from harness.client import McpClientSession
from harness.config import Config

logger = logging.getLogger("harness.server")


def build_server(
    config: Config,
    ue_client: McpClientSession,
) -> Server:
    """构建 MCP Server 实例。

    Args:
        config: Harness 配置。
        ue_client: 已连接的 UE MCP Client。

    Returns:
        配置好的 mcp Server 实例。
    """
    server = Server("ue-agent-harness")

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
        """将 LLM 的工具调用透传到 UE，等待 SSE 完成，返回结果。

        P0 阶段无任何修改——纯透传。
        """
        logger.info("LLM tools/call: %s(%s)", name, arguments)
        try:
            result_text = await ue_client.call_tool(name, arguments)
            return CallToolResult(
                content=[TextContent(type="text", text=result_text)]
            )
        except Exception as e:
            logger.error("工具调用失败: %s(%s) -> %s", name, arguments, e)
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {e}")],
                isError=True,
            )

    return server
