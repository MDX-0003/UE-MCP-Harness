"""SSE Transport — 将 mcp Server 桥接到 HTTP。

使用 Starlette + uvicorn，在指定端口上提供 MCP SSE 端点。
LLM 客户端通过 http://{host}:{port}/sse 连接，消息通过 /messages/ 路由。

与 UE MCP Server 的通信方式一致：JSON-RPC 2.0 over HTTP POST + SSE。

注意：Starlette 1.0 的 Mount 对 ASGI app 会错误包裹 request_response，
      因此 /messages/ POST 端点使用纯 ASGI 中间件直接透传，绕过 Starlette 路由。
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("harness.transport")


class _MessagesMiddleware:
    """纯 ASGI 中间件：拦截 /messages/* POST 请求，直接透传给 MCP SDK 的 handle_post_message。

    Starlette 1.0 的 Mount 会对 ASGI app 做 request_response 包裹，期望返回 Response 对象。
    但 MCP SDK 的 handle_post_message 是原生 ASGI app（通过 send() 操作，返回 None），
    被 request_response 包裹后 None 被当作 Response 调用 → TypeError。
    此中间件在 Starlette 路由层之前拦截，直接以原始 ASGI 方式调用 handle_post_message。
    """

    def __init__(self, app: ASGIApp, sse: SseServerTransport, server: Server, init_opts) -> None:
        self.app = app
        self._sse = sse
        self._server = server
        self._init_opts = init_opts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # POST /messages/* — MCP 消息投递，由 MCP SDK 原生 ASGI handler 处理
        if scope["method"] == "POST" and path.startswith("/messages/"):
            remaining = path[len("/messages"):]  # "/" 或 "/?session=..." 或 "/session_id"
            if not remaining:
                remaining = "/"
            child_scope = dict(scope)
            child_scope["path"] = remaining
            child_scope["root_path"] = scope.get("root_path", "") + "/messages"
            await self._sse.handle_post_message(child_scope, receive, send)
            return

        # GET /sse — SSE 长连接，Starlette 1.0 的 request_response 包装
        # 也会对 ASGI 端点错误处理。走原始 ASGI 路径。
        if scope["method"] == "GET" and path == "/sse":
            await self._handle_sse(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _handle_sse(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理 SSE 连接——直接以 ASGI 方式运行，绕过 Starlette 的 request_response。"""
        async with self._sse.connect_sse(scope, receive, send) as streams:
            await self._server.run(
                streams[0],
                streams[1],
                self._init_opts,
            )


def create_app(server: Server, instructions: str = "") -> Starlette:
    """创建 Starlette ASGI 应用，挂载 MCP SSE 端点。

    Args:
        server: 已配置（注册了 list_tools/call_tool）的 mcp Server 实例。
        instructions: 初始化时发送给 LLM 的系统指令（可选）。

    Returns:
        配置好的 Starlette 应用。
    """
    sse = SseServerTransport("/messages/")

    # 构建初始化选项，注入系统指令
    _init_opts = server.create_initialization_options()
    if instructions:
        _init_opts.instructions = instructions

    app = Starlette(
        debug=False,
        routes=[],  # 所有路由由 _MessagesMiddleware 处理
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
            Middleware(_MessagesMiddleware, sse=sse, server=server, init_opts=_init_opts),
        ],
    )

    return app


async def serve(
    server: Server,
    host: str = "127.0.0.1",
    port: int = 9000,
    instructions: str = "",
) -> None:
    """启动 Harness MCP Server。

    阻塞直到收到停止信号。

    Args:
        server: 已配置的 mcp Server 实例。
        host: 监听地址。
        port: 监听端口。
        instructions: 初始化时发送给 LLM 的系统指令（可选）。
    """
    app = create_app(server, instructions)

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
