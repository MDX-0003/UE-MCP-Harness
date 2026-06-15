"""SSE Transport — 将 mcp Server 桥接到 HTTP。

使用 Starlette + uvicorn，在指定端口上提供 MCP SSE 端点。
LLM 客户端通过 http://{host}:{port}/sse 连接，消息通过 /messages/ 路由。

与 UE MCP Server 的通信方式一致：JSON-RPC 2.0 over HTTP POST + SSE。
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

logger = logging.getLogger("harness.transport")


def create_app(server: Server) -> Starlette:
    """创建 Starlette ASGI 应用，挂载 MCP SSE 端点。

    Args:
        server: 已配置（注册了 list_tools/call_tool）的 mcp Server 实例。

    Returns:
        配置好的 Starlette 应用。
    """
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        """处理 SSE 连接——每个 LLM 客户端连接对应一个 SSE stream。"""
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )

    app = Starlette(
        debug=False,
        routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    # CORS: 允许本地开发工具（MCP Inspector 等）连接
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


async def serve(
    server: Server,
    host: str = "127.0.0.1",
    port: int = 9000,
) -> None:
    """启动 Harness MCP Server。

    阻塞直到收到停止信号。

    Args:
        server: 已配置的 mcp Server 实例。
        host: 监听地址。
        port: 监听端口。
    """
    app = create_app(server)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=None,  # 使用 structlog / 标准 logging
    )
    uvicorn_server = uvicorn.Server(config)

    logger.info("Harness MCP Server 启动中: http://%s:%d/sse", host, port)

    try:
        await uvicorn_server.serve()
    except asyncio.CancelledError:
        logger.info("Harness MCP Server 收到停止信号。")
        await uvicorn_server.shutdown()
