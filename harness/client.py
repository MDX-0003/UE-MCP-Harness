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
    """一个到 UE MCP Server 的 MCP 会话。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._http: httpx.AsyncClient | None = None
        self._request_id: int = 0
        self._session_id: str = ""
        self._negotiated_version: str = ""
        self._connected: bool = False

    # ---- 连接生命周期 ----

    async def connect(self) -> None:
        """连接到 UE MCP Server，完成 MCP 握手。"""
        if self._connected:
            return

        self._http = httpx.AsyncClient(
            base_url=self._config.ue_base_url,
            timeout=httpx.Timeout(self._config.request_timeout),
        )

        logger.info("正在连接 UE MCP Server: %s", self._config.ue_base_url)

        # MCP 握手: initialize
        resp, headers = await self._rpc("initialize", {
            "protocolVersion": self._config.mcp_protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "ue-agent-harness", "version": "0.1.0"},
        })

        if resp.error:
            raise resp.error

        self._negotiated_version = resp.result.get(
            "protocolVersion", self._config.mcp_protocol_version
        )
        # Mcp-Session-Id 在 initialize 的 HTTP response header 中
        self._session_id = headers.get("mcp-session-id", "")
        if not self._session_id:
            logger.warning("未在 initialize 响应中找到 Mcp-Session-Id header")

        logger.info(
            "MCP 握手成功。Session: %s, 协议版本: %s",
            self._session_id or "(无)",
            self._negotiated_version,
        )

        # 通知: initialized
        resp, _ = await self._rpc("notifications/initialized", {}, expect_response=False)
        if resp.error:
            raise resp.error

        self._connected = True
        logger.info("UE MCP Server 连接就绪。")

    async def close(self) -> None:
        """断开与 UE MCP Server 的连接。"""
        if self._http is None:
            return
        if self._connected:
            try:
                await self._http.delete(
                    self._config.ue_url_path,
                    headers=self._build_headers(),
                )
            except Exception:
                pass

        await self._http.aclose()
        self._http = None
        self._connected = False
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
        resp, _ = await self._rpc("tools/list", {})
        if resp.error:
            raise resp.error
        return resp.result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """调用 UE 端的指定工具，处理 SSE 两阶段响应。"""
        self._ensure_connected()

        if arguments is None:
            arguments = {}

        rid = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

        logger.info("调用工具: %s (id=%d)", name, rid)

        response = await self._http.post(
            self._config.ue_url_path,
            json=payload,
            headers=self._build_headers(),
            timeout=httpx.Timeout(self._config.sse_read_timeout),
        )

        if response.status_code != 200:
            raise JsonRpcError(-32000, f"HTTP {response.status_code}: {response.text}")

        content_type = response.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return self._extract_tool_result(rid, response.content)
        elif "application/json" in content_type:
            data = response.json()
            if "error" in data:
                err = data["error"]
                raise JsonRpcError(
                    err.get("code", -1),
                    err.get("message", "Unknown error"),
                    err.get("data"),
                )
            return json.dumps(data.get("result", {}), ensure_ascii=False)
        else:
            raise JsonRpcError(-32000, f"未知响应类型: {content_type}")

    # ---- 私有方法 ----

    def _ensure_connected(self) -> None:
        if not self._connected or self._http is None:
            raise RuntimeError("未连接到 UE MCP Server。请先调用 connect()。")

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _build_headers(self) -> dict[str, str]:
        """构建带 session 和 protocol version 的请求 header。"""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._negotiated_version:
            headers["Mcp-Protocol-Version"] = self._negotiated_version
        return headers

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
            self._config.ue_url_path,
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

    def _extract_tool_result(self, request_id: int, raw: bytes) -> str:
        """从 tools/call 的 SSE 响应中提取最终工具结果。"""
        events = parse_sse_stream(raw)

        for evt in events:
            if not evt.data:
                continue
            try:
                data = json.loads(evt.data)
            except json.JSONDecodeError:
                continue
            if data.get("method") == "notifications/progress":
                continue
            if "result" in data:
                result = data["result"]
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
                return str(result)
            if "error" in data:
                err = data["error"]
                raise JsonRpcError(
                    err.get("code", -1),
                    err.get("message", "Unknown error"),
                    err.get("data"),
                )

        raise JsonRpcError(-32000, f"SSE 响应中未找到工具结果 (request_id={request_id})")

    async def preload_all_toolsets(self) -> int:
        """延迟加载模式下，预加载所有工具集。"""
        self._ensure_connected()

        # 1. list_toolsets
        resp, _ = await self._rpc("tools/call", {
            "name": "list_toolsets",
            "arguments": {},
        })

        if resp.error:
            logger.warning("list_toolsets 失败: %s", resp.error)
            return 0

        catalog_text = ""
        if isinstance(resp.result, dict):
            # MCP tools/call 结果格式: {"content": [{"type": "text", "text": "..."}]}
            content = resp.result.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    catalog_text = item.get("text", "")
                    break
            if not catalog_text:
                # 兜底: 尝试 flat 格式 {"text": "..."} 或 {"structuredContent": "..."}
                for key in ("text", "structuredContent"):
                    if key in resp.result:
                        catalog_text = str(resp.result[key])
                        break
            if not catalog_text:
                logger.debug("无法解析 list_toolsets 结果格式: %s",
                             json.dumps(resp.result, ensure_ascii=False)[:200])

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
        async def load_one(name: str) -> bool:
            try:
                logger.info("正在加载工具集: %s", name)
                resp, _ = await self._rpc("tools/call", {
                    "name": "load_toolset",
                    "arguments": {"toolset_name": name},
                })
                if resp.error:
                    logger.debug("加载 %s 失败: %s", name, resp.error)
                    return False
                return True
            except Exception as e:
                logger.warning("加载 %s 出错: %s", name, e)
                return False

        results = await asyncio.gather(*[load_one(name) for name in toolset_names])
        loaded = sum(1 for r in results if r)
        logger.info("已预加载 %d/%d 个工具集", loaded, len(toolset_names))
        tools = await self.list_tools()
        logger.info("最终工具列表: %d 个工具", len(tools))
        return len(tools)
