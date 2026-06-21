"""面向 UE MCP Server 的 JSON-RPC 2.0 客户端。

UE MCP Server 的 tools/call 响应使用 SSE event-stream（两阶段）：
  1. 立即返回空 SSE 流头（Connection: keep-alive, Content-Type: text/event-stream）
  2. 工具完成后写入 final result frame
  3. 期间可能插入 progress 通知

请求 header 管理：
  - initialize 前: 仅 Content-Type
  - initialize 后: + Mcp-Session-Id + Mcp-Protocol-Version
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
import asyncio
import httpx

from harness.config import Config

logger = logging.getLogger("harness.client")

# ---- SSE 解析 -------------------------------------------------------------


@dataclass
class SseEvent:
    """解析后的 SSE 事件。"""
    event: str | None = None
    data: str | None = None


def parse_sse_stream(raw: bytes) -> list[SseEvent]:
    """解析原始 SSE 字节流，返回事件列表。"""
    text = raw.decode("utf-8", errors="replace")
    events: list[SseEvent] = []
    current = SseEvent()

    for line in text.splitlines():
        if line == "":
            if current.data is not None or current.event is not None:
                events.append(current)
            current = SseEvent()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event: "):
            current.event = line[7:]
        elif line.startswith("event:"):
            current.event = line[6:]
        elif line.startswith("data: "):
            current.data = line[6:]
        elif line.startswith("data:"):
            current.data = line[5:]

    if current.data is not None or current.event is not None:
        events.append(current)

    return events


# ---- JSON-RPC 2.0 消息 ----------------------------------------------------


class JsonRpcError(Exception):
    """JSON-RPC 2.0 错误。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}")


@dataclass
class JsonRpcResponse:
    """解析后的 JSON-RPC 2.0 响应。"""
    id: int
    result: Any = None
    error: JsonRpcError | None = None


# ---- MCP Client Session --------------------------------------------------


class McpClientSession:
    """一个到 UE MCP Server 的 MCP 会话。

    内部使用 MCP SDK 的 streamablehttp_client + ClientSession，
    确保与 UE 直连测试使用完全相同的传输实现。
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session_id: str = ""
        self._negotiated_version: str = ""
        self._connected: bool = False

        # SDK 内部对象
        self._transport_ctx: object = None
        self._sdk_session: object = None

    # ---- 连接生命周期 ----

    async def connect(self) -> None:
        """连接到 UE MCP Server，完成 MCP 握手。"""
        if self._connected:
            return

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        logger.info("正在连接 UE MCP Server: %s", self._config.ue_base_url)

        self._transport_ctx = streamablehttp_client(
            self._config.ue_base_url,
            timeout=self._config.sse_read_timeout,
            sse_read_timeout=self._config.sse_read_timeout,
        )
        read, write, get_id = await self._transport_ctx.__aenter__()
        self._sdk_session = ClientSession(read, write)

        result = await self._sdk_session.initialize()
        self._negotiated_version = result.protocolVersion
        self._session_id = get_id() or ""
        self._connected = True

        logger.info(
            "MCP 握手成功。Session: %s, 协议版本: %s",
            self._session_id or "(无)",
            self._negotiated_version,
        )

    async def close(self) -> None:
        """断开与 UE MCP Server 的连接。"""
        if self._transport_ctx is None:
            return

        self._connected = False

        try:
            await self._transport_ctx.__aexit__(None, None, None)
        except Exception:
            pass

        self._transport_ctx = None
        self._sdk_session = None
        self._session_id = ""
        logger.info("已断开 UE MCP Server 连接。")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def negotiated_version(self) -> str:
        return self._negotiated_version

    # ---- 工具操作 ----

    async def list_tools(self) -> list[dict]:
        """获取 UE 端所有可用工具的列表。"""
        self._ensure_connected()
        result = await self._sdk_session.list_tools()
        return [tool.model_dump() for tool in result.tools]

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """调用 UE 端工具，委托给 SDK ClientSession。"""
        self._ensure_connected()
        logger.info("调用工具: %s", name)
        result = await self._sdk_session.call_tool(
            name, arguments or {}
        )
        # 提取文本内容，保持与原有 str 返回类型的兼容
        parts: list[str] = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
        return "\n".join(parts)

    # ---- 私有方法 ----

    def _ensure_connected(self) -> None:
        if not self._connected or self._sdk_session is None:
            raise RuntimeError("未连接到 UE MCP Server。请先调用 connect()。")

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _build_headers(self) -> dict[str, str]:
        """构建带 session 和 protocol version 的请求 header。"""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._negotiated_version:
            headers["Mcp-Protocol-Version"] = self._negotiated_version
        return headers

    async def _cancel_request(self, request_id: int) -> None:
        """发送 notifications/cancelled 取消服务端正在执行的工具。

        当客户端 SSE 读取超时或发生异常时调用，通知 UE MCP Server
        停止对应的异步工具执行。防止服务端在 TCP 连接已断开后
        仍尝试通过 OnComplete 写入 SSE 结果，导致 HTTP writer 阻塞。
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": request_id,
                "reason": "Client read timeout",
            },
        }
        try:
            async with asyncio.timeout(5):
                await self._http.post(
                    self._config.ue_base_url,
                    json=payload,
                    headers=self._build_headers(),
                )
        except Exception:
            pass  # 尽最大努力——即使取消失败，后续 DELETE 仍会清理 session

    async def _rpc(
        self,
        method: str,
        params: dict,
        expect_response: bool = True,
    ) -> tuple[JsonRpcResponse, dict[str, str]]:
        """发送 JSON-RPC 2.0 请求到 UE，返回 (响应, HTTP response headers)。

        不检查 _ensure_connected——此方法被 connect() 自身调用。
        """
        if self._http is None:
            raise RuntimeError("HTTP 客户端未初始化。")

        rid = self._next_id()
        payload: dict = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if expect_response:
            payload["id"] = rid

        # tools/call 返回 SSE event-stream，需要更长的读取超时
        is_sse_method = (method == "tools/call")
        req_timeout = (
            httpx.Timeout(self._config.sse_read_timeout)
            if is_sse_method
            else httpx.Timeout(self._config.request_timeout)
        )
        response = await self._http.post(
            self._config.ue_base_url,
            json=payload,
            headers=self._build_headers(),
            timeout=req_timeout,
        )

        resp_headers: dict[str, str] = {
            k.lower(): v for k, v in response.headers.items()
        }

        if not expect_response:
            if response.status_code in (200, 202, 204):
                return JsonRpcResponse(id=rid, result={}), resp_headers
            return (
                JsonRpcResponse(
                    id=rid,
                    error=JsonRpcError(
                        -32000, f"Notification failed: HTTP {response.status_code}"
                    ),
                ),
                resp_headers,
            )

        if response.status_code != 200:
            return (
                JsonRpcResponse(
                    id=rid,
                    error=JsonRpcError(
                        -32000, f"HTTP {response.status_code}: {response.text}"
                    ),
                ),
                resp_headers,
            )

        content_type = response.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            events = parse_sse_stream(response.content)
            for evt in events:
                if evt.data:
                    try:
                        data = json.loads(evt.data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in data:
                        err = data["error"]
                        return (
                            JsonRpcResponse(
                                id=rid,
                                error=JsonRpcError(
                                    err.get("code", -1),
                                    err.get("message", "Unknown error"),
                                    err.get("data"),
                                ),
                            ),
                            resp_headers,
                        )
                    if "result" in data:
                        return JsonRpcResponse(id=rid, result=data["result"]), resp_headers
            return (
                JsonRpcResponse(
                    id=rid,
                    error=JsonRpcError(-32000, "SSE 响应中未找到 result"),
                ),
                resp_headers,
            )

        data = response.json()
        if "error" in data:
            err = data["error"]
            return (
                JsonRpcResponse(
                    id=rid,
                    error=JsonRpcError(
                        err.get("code", -1),
                        err.get("message", "Unknown error"),
                        err.get("data"),
                    ),
                ),
                resp_headers,
            )
        return JsonRpcResponse(id=rid, result=data.get("result", {})), resp_headers

    async def _read_sse_stream(self, request_id: int, response) -> str:
        """增量读取 SSE 事件流，遇 result/error 立即返回。

        与 _rpc() 的阻塞式 response.content 不同，此方法使用 aiter_lines()
        逐行消费 SSE 流——收到最终事件后立即返回，不等待 HTTP 连接关闭。
        处理 progress 通知、多行 data、注释行。
        """
        data_parts: list[str] = []
        line_count = 0

        logger.debug("[sse-stream] _read_sse_stream id=%d 开始, content-type=%s",
                     request_id,
                     getattr(response, "headers", {}).get("content-type", "?"))

        async for line in response.aiter_lines():
            line_count += 1
            if line_count <= 3:
                logger.debug("[sse-stream] id=%d line[%d]: %s", request_id, line_count, line[:120])
            if line == "":
                # 空行 = 事件边界，处理已累积的 data
                if data_parts:
                    data_str = "".join(data_parts)
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data_parts.clear()
                        continue

                    if data.get("method") == "notifications/progress":
                        data_parts.clear()
                        continue
                    if "result" in data:
                        result = data["result"]
                        return (
                            json.dumps(result, ensure_ascii=False)
                            if isinstance(result, dict)
                            else str(result)
                        )
                    if "error" in data:
                        err = data["error"]
                        raise JsonRpcError(
                            err.get("code", -1),
                            err.get("message", "Unknown error"),
                            err.get("data"),
                        )
                data_parts.clear()
                continue

            if line.startswith(":"):
                continue  # SSE 注释

            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_parts.append(value)

        raise JsonRpcError(
            -32000,
            f"SSE 流结束但未找到工具结果 (request_id={request_id}, "
            f"共收到 {line_count} 行)",
        )

    async def preload_all_toolsets(self) -> int:
        """延迟加载模式下，预加载所有工具集。"""
        self._ensure_connected()

        # 1. list_toolsets
        try:
            catalog_text = await self.call_tool("list_toolsets", {})
        except Exception as e:
            logger.warning("list_toolsets 失败: %s", e)
            return 0

        if not catalog_text:
            logger.info("list_toolsets 返回空——可能已在 eager 模式")
            tools = await self.list_tools()
            return len(tools)

        # 2. 解析工具集名称
        toolset_names: list[str] = []
        for line in catalog_text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                name = line[2:].split(":")[0].strip()
                if name and name not in ("list_toolsets", "describe_toolset", "load_toolset"):
                    toolset_names.append(name)

        logger.info("发现 %d 个工具集，开始预加载...", len(toolset_names))

        # 3. 并行预加载（工具集之间无依赖，并发加载可大幅提速）
        async def load_one(toolset_name: str) -> bool:
            try:
                logger.info("正在加载工具集: %s", toolset_name)
                await self.call_tool("load_toolset", {"toolset_name": toolset_name})
                return True
            except Exception as e:
                logger.warning("加载 %s 出错: %s", toolset_name, e)
                return False

        results = await asyncio.gather(*[load_one(name) for name in toolset_names])
        loaded = sum(1 for r in results if r)
        logger.info("已预加载 %d/%d 个工具集", loaded, len(toolset_names))
        tools = await self.list_tools()
        logger.info("最终工具列表: %d 个工具", len(tools))
        return len(tools)
