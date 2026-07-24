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
from collections.abc import Callable, Awaitable
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


def _extract_text_from_result(result_str: str) -> str:
    """从 MCP tool call 返回的 JSON 字符串中提取第一个 text content。

    处理格式: {"content": [{"type": "text", "text": "..."}, ...], ...}
    返回提取的文本，失败时返回空字符串。
    """
    try:
        result = json.loads(result_str)
    except json.JSONDecodeError:
        return result_str
    if isinstance(result, dict):
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
        for key in ("text", "structuredContent"):
            if key in result:
                return str(result[key])
    return ""


# ---- MCP 结果解包（公共入口，全仓统一调用） --------------------------------


def mcp_parse_result(raw: str | Any | None) -> Any:
    """解析 JSON-RPC result 为 Python 对象。JSON 字符串尝试 parse，否则原样返回。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def mcp_extract_text(raw: str | Any | None, fallback: str | None = None) -> str | None:
    """从 MCP content array 提取首个 text 内容。

    格式: {"content": [{"type": "text", "text": "..."}, ...]}
    遇 image 类型返回 "[image: mimeType]" 标记文本。
    """
    parsed = mcp_parse_result(raw)
    if parsed is None:
        return fallback
    if isinstance(parsed, dict):
        content = parsed.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
            if isinstance(item, dict) and item.get("type") == "image":
                return f"[image: {item.get('mimeType', 'unknown')}]"
    if fallback is not None:
        return fallback
    return str(parsed)


def mcp_unwrap_return_value(text: str) -> dict | None:
    """解包 ToolsetRegistry returnValue 包装。

    格式: {"returnValue": "<json_string>"}
    返回内层 dict 或 None（表示不是 returnValue 格式）。
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and "returnValue" in parsed:
        rv = parsed["returnValue"]
        if isinstance(rv, str):
            try:
                inner = json.loads(rv)
                return inner if isinstance(inner, dict) else None
            except (json.JSONDecodeError, TypeError):
                return None
        if isinstance(rv, dict):
            return rv
    return None


def mcp_unwrap_return_text(text: str) -> str:
    """解包 returnValue JSON 包装，返回内层字符串。非 returnValue 格式时原样返回。"""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, dict) and "returnValue" in parsed:
        rv = parsed["returnValue"]
        if isinstance(rv, str):
            return rv
        return json.dumps(rv) if isinstance(rv, dict) else str(rv)
    return text


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
        self._on_reconnect: list[Callable[[], Awaitable[None]]] = []

    @property
    def ue_port(self) -> int:
        """UE MCP Server 端口号（供截图窗口激活等辅助功能使用）。"""
        return self._config.ue_port

    # ---- 连接生命周期 ----

    async def connect(self) -> None:
        """连接到 UE MCP Server，完成 MCP 握手。"""
        if self._connected:
            return

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._config.sse_read_timeout,
                read=self._config.sse_read_timeout,
            ),
            http2=False,
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
        """断开与 UE MCP Server 的连接。

        关闭顺序至关重要：
        1. 标记 _connected=False，阻止新请求
        2. 发送 DELETE 通知服务器终止 session（让服务器清理资源）
        3. 关闭 HTTP 客户端（释放所有 TCP 连接）
        """
        if self._http is None:
            return

        was_connected = self._connected
        self._connected = False

        if was_connected and self._session_id:
            logger.debug("正在终止 MCP session: %s", self._session_id[:20])
            try:
                resp = await self._http.delete(
                    self._config.ue_base_url,
                    headers=self._build_headers(),
                )
                logger.debug("Session 终止响应: HTTP %d", resp.status_code)
            except Exception as e:
                # 静默处理——DELETE 失败不阻塞 Harness 关闭。
                # 若频繁出现，检查 UE 端是否有残留 session（UE log: "Session initialized" 无对应 DELETE）。
                logger.debug("Session 终止请求失败（非致命）: %s", e)

        await self._http.aclose()
        self._http = None
        self._session_id = ""
        logger.info("已断开 UE MCP Server 连接。")

    async def reconnect(self) -> None:
        """重新连接到 UE MCP Server。

        步骤：清理旧连接 → 完整 MCP 握手 → 通知所有关注者（钩子链）。
        旧 session 已随 UE 进程消失，不做 DELETE。
        """
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

        self._session_id = ""
        self._negotiated_version = ""
        self._request_id = 0

        await self.connect()

        for hook in self._on_reconnect:
            try:
                await hook()
            except Exception as e:
                logger.warning(
                    "重连回调 %s 失败（非致命）: %s",
                    getattr(hook, "__name__", hook), e,
                )

    def add_reconnect_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """注册重连成功后的回调。按注册顺序同步执行。

        钩子在 reconnect() 末尾、_ensure_connected() 返回之前执行。
        单个钩子失败不阻断后续钩子或原工具调用。
        """
        self._on_reconnect.append(hook)

    async def ping(self, timeout: float = 3.0) -> bool:
        """轻量存活检测——确认 UE MCP Server 进程是否在监听端口。

        使用独立 httpx 客户端，不依赖 self._http 或 self._session_id。
        """
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(self._config.ue_base_url)
                    return resp.status_code < 500
        except Exception:
            return False

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
        await self._ensure_connected()
        resp, _ = await self._rpc("tools/list", {})
        if resp.error:
            raise resp.error
        return resp.result.get("tools", [])

    async def call_tool_blocking(self, name: str, arguments: dict | None = None) -> str:
        """调用 UE 端工具，使用阻塞式 post() 读取完整 SSE 响应。

        与 call_tool() 的 stream() + aiter_lines() 不同，此方法使用
        httpx.post() 的 response.content 阻塞读取——等服务器在最后
        一次 OnComplete 后关闭连接再返回。用于避开 UE HTTP Server
        MultipleWriteStream 跨线程写入的延迟/丢失问题。
        """
        await self._ensure_connected()
        resp, _ = await self._rpc("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        if resp.error:
            raise resp.error
        return json.dumps(resp.result, ensure_ascii=False)

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """调用 UE 端的指定工具，增量处理 SSE 事件流。

        使用 httpx.stream() 而非 post()——post() 在非流式模式下会预读整个
        响应体后才返回。stream() 在收到 HTTP 响应头后立即返回，
        body 通过 aiter_lines() 增量消费。

        每次 SSE 响应后显式 aclose() 关闭底层 TCP 连接，防止 httpx 连接池
        复用导致 UE Server 的 MultipleWriteStream 与旧连接混淆。
        """
        await self._ensure_connected()

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

        response = None
        try:
            async with self._http.stream(
                "POST",
                self._config.ue_base_url,
                json=payload,
                headers=self._build_headers(),
            ) as stream_response:
                response = stream_response
                if response.status_code != 200:
                    await response.aread()
                    # 502/503/504 = 代理/网关层报告后端不可达，等价于连接断开
                    if response.status_code in (502, 503, 504):
                        self._connected = False
                        raise JsonRpcError(
                            -32000,
                            "与 UE MCP Server 的连接已断开（HTTP "
                            f"{response.status_code}）。"
                            "请确认 UE 编辑器正在运行且 MCP Server 已启动。",
                        )
                    raise JsonRpcError(
                        -32000, f"HTTP {response.status_code}: {response.text}"
                    )

                content_type = response.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    result = await self._read_sse_stream(rid, response)
                    await response.aclose()
                    return result
                elif "application/json" in content_type:
                    await response.aread()
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
                    await response.aread()
                    raise JsonRpcError(-32000, f"未知响应类型: {content_type}")
        except httpx.ConnectError:
            self._connected = False
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            raise
        except httpx.ReadTimeout:
            # UE 大概率活着，工具执行超时。不翻 _connected。
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if self._connected and self._session_id:
                try:
                    await self._cancel_request(rid)
                except Exception:
                    pass
            raise
        except httpx.RemoteProtocolError:
            # 灰色地带：先 ping 确认
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if not await self.ping(timeout=2.0):
                self._connected = False
            raise
        except Exception:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            raise

    # ---- 私有方法 ----

    async def _ensure_connected(self) -> None:
        """确认与 UE MCP Server 的连接有效。

        快速路径：_connected=True + _http 存在 → 直接通过（零 I/O）
        急救路径：_connected=False → ping 确认 UE 恢复 → 自动重连
        失败路径：ping 不通 → 抛 RuntimeError
        """
        if self._connected and self._http is not None:
            return

        if not await self.ping(timeout=2.0):
            raise RuntimeError(
                "与 UE MCP Server 的连接已断开。"
                "请确认 UE 编辑器正在运行且 MCP Server 已启动，然后重试。"
            )

        await self.reconnect()

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
            if response.status_code in (502, 503, 504):
                self._connected = False
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

    async def _read_sse_stream(self, request_id: int, response, *, log: bool = False) -> str:
        """增量读取 SSE 事件流，遇 result/error 立即返回。

        与 _rpc() 的阻塞式 response.content 不同，此方法使用 aiter_lines()
        逐行消费 SSE 流——收到最终事件后立即返回，不等待 HTTP 连接关闭。
        处理 progress 通知、多行 data、注释行。

        log: 为 True 时才输出 logger.info 日志。默认 False，避免高频调用污染上下文。
        """
        import time as _time
        data_parts: list[str] = []
        line_count = 0
        t_start = _time.monotonic()

        logger.debug(
            "[sse-stream] id=%d 开始, ct=%s, Connection=%s",
            request_id,
            getattr(response, "headers", {}).get("content-type", "?"),
            getattr(response, "headers", {}).get("connection", "?"),
        )

        async for line in response.aiter_lines():
            line_count += 1
            t_elapsed = _time.monotonic() - t_start
            if line_count == 1 and log:
                logger.info("[sse-stream] id=%d 首行到达, 耗时=%.1fs, 内容: %s",
                            request_id, t_elapsed, line[:120])
            elif line_count <= 3:
                logger.debug("[sse-stream] id=%d line[%d] (+%.1fs): %s",
                             request_id, line_count, t_elapsed, line[:120])
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
                        t_total = _time.monotonic() - t_start
                        if log:
                            logger.info("[sse-stream] id=%d 收到 result, 总耗时=%.1fs, 共 %d 行",
                                        request_id, t_total, line_count)
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
        """延迟加载模式下，预加载所有工具集。

        使用 call_tool() 的 SSE 流式读取（而非 _rpc 的阻塞 POST），
        正确处理 UE MCP Server 的 MultipleWriteStream + keep-alive 连接。
        """
        await self._ensure_connected()

        # 1. list_toolsets — 用 call_tool 的 SSE 流式读取
        try:
            result_str = await self.call_tool("list_toolsets", {})
            catalog_text = _extract_text_from_result(result_str)
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
        async def load_one(name: str) -> bool:
            try:
                logger.info("正在加载工具集: %s", name)
                result_str = await self.call_tool("load_toolset",
                                                   {"toolset_name": name})
                result = json.loads(result_str)
                if isinstance(result, dict) and result.get("isError"):
                    err_text = _extract_text_from_result(result_str)
                    logger.warning("加载 %s 失败: %s", name, err_text[:200])
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
