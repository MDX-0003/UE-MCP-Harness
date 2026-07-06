"""MCP Transport — 将 mcp Server 桥接到 HTTP。

支持两种传输协议：
  - Streamable HTTP (2025 规范): POST /mcp — Claude Code VS Code 扩展的主协议
  - SSE (旧规范): GET /sse + POST /messages/ — 保留向后兼容

默认同时启用两种传输。MCP SDK 的 StreamableHTTPServerTransport 处理 /mcp 端点，
SseServerTransport 处理 /sse + /messages/ 端点。

涉及 Issue: 002 (MCP 握手), 004 (Context Assembly)
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("harness.transport")


class _MessagesMiddleware:
    """纯 ASGI 中间件：拦截 /messages/* POST 和 /sse GET (SSE 旧协议)。

    Starlette 1.0 的 Mount 会对 ASGI app 做 request_response 包裹，期望返回 Response 对象。
    但 MCP SDK 的 handle_post_message 是原生 ASGI app（通过 send() 操作，返回 None），
    被 request_response 包裹后 None 被当作 Response 调用 → TypeError。
    此中间件在 Starlette 路由层之前拦截，直接以原始 ASGI 方式调用 MCP SDK handler。
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

        # GET /sse — SSE 长连接
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


class _StreamableHttpMiddleware:
    """ASGI 中间件：拦截 /mcp 请求，路由给 StreamableHTTPServerTransport。

    Streamable HTTP 是 MCP 2025 规范的主传输协议，单端点 (/mcp) 同时处理
    JSON-RPC 消息和 SSE 事件流。Claude Code VS Code 扩展使用此协议。
    """

    def __init__(self, app: ASGIApp, transport: StreamableHTTPServerTransport) -> None:
        self.app = app
        self._transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Streamable HTTP: POST /mcp 或 GET /mcp（含 query string）
        if path == "/mcp" or path.startswith("/mcp?"):
            await self._transport.handle_request(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_app(
    server: Server,
    enable_sse: bool = True,
    enable_streamable_http: bool = True,
) -> Starlette:
    """创建 Starlette ASGI 应用，挂载 MCP 端点。

    默认同时启用 Streamable HTTP (/mcp) 和 SSE (/sse + /messages/) 两种传输。
    通过 enable_sse / enable_streamable_http 开关控制。

    Args:
        server: 已配置（注册了 list_tools/call_tool）的 mcp Server 实例。
        instructions: 初始化时发送给 LLM 的系统指令（可选）。
        enable_sse: 是否启用旧版 SSE 传输（GET /sse + POST /messages/）。
        enable_streamable_http: 是否启用新版 Streamable HTTP 传输（POST /mcp）。

    Returns:
        配置好的 Starlette 应用。
    """
    _init_opts = server.create_initialization_options()

    middlewares: list[Middleware] = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    # SSE 传输（旧协议）
    if enable_sse:
        sse = SseServerTransport("/messages/")
        middlewares.append(
            Middleware(_MessagesMiddleware, sse=sse, server=server, init_opts=_init_opts)
        )

    # Streamable HTTP 传输（新协议，2025 规范）
    sh_transport: StreamableHTTPServerTransport | None = None
    if enable_streamable_http:
        sh_transport = StreamableHTTPServerTransport(mcp_session_id=None)
        middlewares.append(
            Middleware(_StreamableHttpMiddleware, transport=sh_transport)
        )

    app = Starlette(
        debug=False,
        routes=[],  # 所有路由由中间件处理
        middleware=middlewares,
    )

    # 挂载 Streamable HTTP transport 引用，供 serve() 使用 lifecycle
    app._sh_transport = sh_transport

    return app


async def serve(
    server: Server,
    host: str = "127.0.0.1",
    port: int = 9000,
    enable_sse: bool = True,
    enable_streamable_http: bool = True,
) -> None:
    """启动 Harness MCP Server。

    SSE 传输由 _MessagesMiddleware 在每次 GET /sse 连接时通过 connect_sse 启动 server.run()。
    Streamable HTTP 传输需要一个长期的 connect() 上下文管理器来维持 server.run()。

    当两种传输都启用时：
      - SSE: server.run() 由每个 SSE 连接独立驱动（每个连接一个 server.run()）
      - Streamable HTTP: 启动一个后台 server.run()，通过 connect() 提供读写流

    Args:
        server: 已配置的 mcp Server 实例。
        host: 监听地址。
        port: 监听端口。
        instructions: 初始化时发送给 LLM 的系统指令。
        enable_sse: 是否启用旧版 SSE 传输。
        enable_streamable_http: 是否启用新版 Streamable HTTP 传输。
    """
    app = create_app(
        server,
        enable_sse=enable_sse,
        enable_streamable_http=enable_streamable_http,
    )
    sh_transport: StreamableHTTPServerTransport | None = getattr(app, '_sh_transport', None)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=None,  # 使用 structlog / 标准 logging
    )
    uvicorn_server = uvicorn.Server(config)

    # 初始化选项
    _init_opts = server.create_initialization_options()

    # 如果启用了 Streamable HTTP，需要在 connect() 上下文中运行 server
    if sh_transport is not None and enable_streamable_http:
        async with sh_transport.connect() as (read_stream, write_stream):
            # 后台运行 MCP server
            async def _run_mcp() -> None:
                await server.run(read_stream, write_stream, _init_opts)

            mcp_task = asyncio.create_task(_run_mcp())

            # 同时运行 uvicorn HTTP server
            _log_startup(host, port, enable_sse, enable_streamable_http)
            try:
                await uvicorn_server.serve()
            except asyncio.CancelledError:
                logger.info("Harness MCP Server 收到停止信号。")
            finally:
                mcp_task.cancel()
                try:
                    await mcp_task
                except asyncio.CancelledError:
                    pass
                try:
                    await uvicorn_server.shutdown()
                except AttributeError:
                    pass  # 服务器未完全启动，servers 属性不存在
    else:
        # 仅 SSE — 由 _handle_sse 按连接驱动 server.run()
        _log_startup(host, port, enable_sse, enable_streamable_http)
        try:
            await uvicorn_server.serve()
        except asyncio.CancelledError:
            logger.info("Harness MCP Server 收到停止信号。")
            try:
                await uvicorn_server.shutdown()
            except AttributeError:
                pass


def _log_startup(host: str, port: int, sse: bool, sh: bool) -> None:
    """输出启动日志，列出启用的端点。"""
    endpoints: list[str] = []
    if sh:
        endpoints.append(f"http://{host}:{port}/mcp (Streamable HTTP)")
    if sse:
        endpoints.append(f"http://{host}:{port}/sse (SSE)")
    logger.info("Harness MCP Server 启动中，端点: %s", ", ".join(endpoints))
    logger.info("接下来可以：1.Agent端重连一次MCP Server 2.开始与外部Agent对话")
