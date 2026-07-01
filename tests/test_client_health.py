"""测试 McpClientSession 连接健康检测 — Issue 012

覆盖: 翻旗逻辑、ping 存活确认、重连钩子、_ensure_connected 升级。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from harness.client import McpClientSession, JsonRpcError
from harness.config import Config


# ---- helpers ----------------------------------------------------------------

def _make_session() -> McpClientSession:
    """创建一个未连接的 session 实例。"""
    return McpClientSession(Config())


# ---- 翻旗逻辑 ---------------------------------------------------------------


class TestConnectionFlag:
    """验证 call_tool() 异常处理中 _connected 标志位的翻旗决策。"""

    @pytest.mark.asyncio
    async def test_connect_error_flips_flag(self) -> None:
        """mock httpx.stream 内部抛 ConnectError → _connected 翻 False"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()
        session._http.stream.side_effect = httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            await session.call_tool("test_tool", {})
        assert session._connected is False

    @pytest.mark.asyncio
    async def test_read_timeout_does_not_flip_flag(self) -> None:
        """mock httpx.stream 内部抛 ReadTimeout → _connected 保持 True"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()
        session._http.stream.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(httpx.ReadTimeout):
            await session.call_tool("test_tool", {})
        assert session._connected is True

    @pytest.mark.asyncio
    async def test_remote_protocol_error_ping_success_keeps_flag(self) -> None:
        """mock RemoteProtocolError + ping 成功 → _connected 保持 True"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()
        session._http.stream.side_effect = httpx.RemoteProtocolError("reset")

        with patch.object(session, "ping", AsyncMock(return_value=True)):
            with pytest.raises(httpx.RemoteProtocolError):
                await session.call_tool("test_tool", {})
        assert session._connected is True

    @pytest.mark.asyncio
    async def test_remote_protocol_error_ping_fail_flips_flag(self) -> None:
        """mock RemoteProtocolError + ping 失败 → _connected 翻 False"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()
        session._http.stream.side_effect = httpx.RemoteProtocolError("reset")

        with patch.object(session, "ping", AsyncMock(return_value=False)):
            with pytest.raises(httpx.RemoteProtocolError):
                await session.call_tool("test_tool", {})
        assert session._connected is False

    @pytest.mark.asyncio
    async def test_generic_exception_does_not_flip_flag(self) -> None:
        """mock 其他异常（如 ValueError）→ _connected 不动"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()
        session._http.stream.side_effect = ValueError("unexpected")

        with pytest.raises(ValueError):
            await session.call_tool("test_tool", {})
        assert session._connected is True

    @pytest.mark.asyncio
    async def test_json_rpc_error_does_not_flip_flag(self) -> None:
        """mock SSE 流中的 JSON-RPC error → _connected 不动"""
        session = _make_session()
        session._connected = True

        class _ErrorResponse:
            status_code = 200
            headers = MagicMock()
            headers.get.return_value = "text/event-stream"

            async def aiter_lines(self):
                yield 'data: {"error": {"code": -32000, "message": "Tool not found"}}'
                yield ""
                # 迭代器在这里结束，不再 yield

        session._http = MagicMock()
        session._http.stream.return_value.__aenter__.return_value = _ErrorResponse()

        with pytest.raises(JsonRpcError):
            await session.call_tool("test_tool", {})
        assert session._connected is True

    @pytest.mark.asyncio
    async def test_http_502_flips_flag(self) -> None:
        """mock UE 返回 HTTP 502 → _connected 翻 False（代理层报告后端不可达）"""
        session = _make_session()
        session._connected = True

        class _BadGatewayResponse:
            status_code = 502
            text = "<html>502 Bad Gateway</html>"
            headers = MagicMock()
            headers.get.return_value = "text/plain"

            async def aread(self):
                pass

        session._http = MagicMock()
        session._http.stream.return_value.__aenter__.return_value = _BadGatewayResponse()

        with pytest.raises(JsonRpcError):
            await session.call_tool("test_tool", {})
        assert session._connected is False

    @pytest.mark.asyncio
    async def test_http_503_flips_flag(self) -> None:
        """mock UE 返回 HTTP 503 → _connected 翻 False"""
        session = _make_session()
        session._connected = True

        class _UnavailableResponse:
            status_code = 503
            text = "503 Service Unavailable"
            headers = MagicMock()
            headers.get.return_value = "text/plain"

            async def aread(self):
                pass

        session._http = MagicMock()
        session._http.stream.return_value.__aenter__.return_value = _UnavailableResponse()

        with pytest.raises(JsonRpcError):
            await session.call_tool("test_tool", {})
        assert session._connected is False

    @pytest.mark.asyncio
    async def test_http_500_does_not_flip_flag(self) -> None:
        """mock UE 返回 HTTP 500 → _connected 不动（内部错误，UE 仍可达）"""
        session = _make_session()
        session._connected = True

        class _InternalErrorResponse:
            status_code = 500
            text = "500 Internal Server Error"
            headers = MagicMock()
            headers.get.return_value = "text/plain"

            async def aread(self):
                pass

        session._http = MagicMock()
        session._http.stream.return_value.__aenter__.return_value = _InternalErrorResponse()

        with pytest.raises(JsonRpcError):
            await session.call_tool("test_tool", {})
        assert session._connected is True


# ---- ping 存活确认 ---------------------------------------------------------


class TestPing:
    """验证 ping() 的独立存活检测逻辑。"""

    @pytest.mark.asyncio
    async def test_ping_returns_true_when_ue_alive(self) -> None:
        """mock GET 返回 200 → True"""
        session = _make_session()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value.status_code = 200

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await session.ping(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_returns_true_on_404(self) -> None:
        """mock GET 返回 404 → True（UE 在但路径不对）"""
        session = _make_session()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value.status_code = 404

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await session.ping(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_returns_false_on_timeout(self) -> None:
        """mock GET 超时 → False"""
        session = _make_session()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = TimeoutError()

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await session.ping(timeout=1.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_ping_returns_false_on_connect_error(self) -> None:
        """mock GET 抛 ConnectError → False"""
        session = _make_session()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("refused")

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await session.ping(timeout=1.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_ping_uses_independent_client(self) -> None:
        """ping 不依赖 self._http——即使 self._http 为 None 也能正常执行。"""
        session = _make_session()
        session._http = None  # 模拟连接断开后的状态

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value.status_code = 200

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await session.ping(timeout=1.0)
        assert result is True


# ---- 重连钩子 --------------------------------------------------------------


class TestReconnectHooks:
    """验证 add_reconnect_hook() + reconnect() 的钩子链行为。"""

    @pytest.mark.asyncio
    async def test_hooks_called_after_reconnect(self) -> None:
        """reconnect() 成功后 → 所有已注册 hook 被执行"""
        session = _make_session()
        hook_a = AsyncMock()
        hook_b = AsyncMock()
        session.add_reconnect_hook(hook_a)
        session.add_reconnect_hook(hook_b)

        with patch.object(session, "connect", AsyncMock()):
            await session.reconnect()

        hook_a.assert_awaited_once()
        hook_b.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hooks_executed_in_registration_order(self) -> None:
        """hook 按 add_reconnect_hook 的注册顺序执行"""
        session = _make_session()
        order: list[str] = []

        async def hook_a() -> None:
            order.append("a")

        async def hook_b() -> None:
            order.append("b")

        session.add_reconnect_hook(hook_a)
        session.add_reconnect_hook(hook_b)

        with patch.object(session, "connect", AsyncMock()):
            await session.reconnect()

        assert order == ["a", "b"]

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_block_others(self) -> None:
        """一个 hook 抛异常 → 后续 hook 仍执行"""
        session = _make_session()
        hook_b_called = False

        async def hook_a() -> None:
            raise RuntimeError("hook_a failed")

        async def hook_b() -> None:
            nonlocal hook_b_called
            hook_b_called = True

        session.add_reconnect_hook(hook_a)
        session.add_reconnect_hook(hook_b)

        with patch.object(session, "connect", AsyncMock()):
            await session.reconnect()

        assert hook_b_called is True

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_block_caller(self) -> None:
        """hook 抛异常 → reconnect() 不抛，调用方不受影响"""
        session = _make_session()

        async def hook_a() -> None:
            raise RuntimeError("hook_a failed")

        session.add_reconnect_hook(hook_a)

        with patch.object(session, "connect", AsyncMock()):
            # 不应抛出
            await session.reconnect()

    @pytest.mark.asyncio
    async def test_no_hooks_does_not_error(self) -> None:
        """没有注册任何 hook → reconnect() 正常完成"""
        session = _make_session()

        with patch.object(session, "connect", AsyncMock()):
            await session.reconnect()  # 不应抛异常


# ---- _ensure_connected 升级 ------------------------------------------------


class TestEnsureConnected:
    """验证 async _ensure_connected() 的三条路径。"""

    @pytest.mark.asyncio
    async def test_fast_path_no_io(self) -> None:
        """_connected=True + _http 存在 → 直接通过，不调 ping"""
        session = _make_session()
        session._connected = True
        session._http = MagicMock()

        with patch.object(session, "ping", AsyncMock()) as mock_ping:
            await session._ensure_connected()

        mock_ping.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_path_ping_success(self) -> None:
        """_connected=False + ping 成功 → 调 reconnect()"""
        session = _make_session()
        session._connected = False

        with patch.object(session, "ping", AsyncMock(return_value=True)):
            with patch.object(session, "reconnect", AsyncMock()) as mock_reconnect:
                await session._ensure_connected()

        mock_reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_path_ping_fail_raises(self) -> None:
        """_connected=False + ping 失败 → 抛 RuntimeError"""
        session = _make_session()
        session._connected = False

        with patch.object(session, "ping", AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="连接已断开"):
                await session._ensure_connected()

    @pytest.mark.asyncio
    async def test_disconnected_then_recovered(self) -> None:
        """端到端：模拟 UE 崩溃→用户重启→LLM 重试→自动重连的完整链路"""
        session = _make_session()
        session._connected = False  # UE 崩溃后标志位已翻

        async def _fake_reconnect():
            session._connected = True

        with patch.object(session, "ping", AsyncMock(return_value=True)):
            with patch.object(session, "reconnect", _fake_reconnect):
                session.add_reconnect_hook(AsyncMock())
                await session._ensure_connected()

        assert session._connected is True
