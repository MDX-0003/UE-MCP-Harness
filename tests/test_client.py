"""测试 harness.client 模块——SSE 解析器 + JSON-RPC 客户端 + 流式 SSE 读取。"""

import json

import pytest
import httpx

from harness.client import (
    JsonRpcError,
    JsonRpcResponse,
    McpClientSession,
    parse_sse_stream,
)
from harness.config import Config


# ---- SSE 解析测试 ----------------------------------------------------------


class TestSseParser:
    """测试 SSE 事件流解析。"""

    def test_single_event(self) -> None:
        raw = b'event: message\r\ndata: {"result": "ok"}\r\n\r\n'
        events = parse_sse_stream(raw)
        assert len(events) == 1
        assert events[0].event == "message"
        assert events[0].data == '{"result": "ok"}'

    def test_multiple_events(self) -> None:
        raw = b'event: progress\r\ndata: {"progress": 1}\r\n\r\nevent: message\r\ndata: {"result": "done"}\r\n\r\n'
        events = parse_sse_stream(raw)
        assert len(events) == 2
        assert events[0].event == "progress"
        assert events[1].event == "message"
        assert events[1].data == '{"result": "done"}'

    def test_event_without_event_field(self) -> None:
        """事件可能只有 data 没有 event type。"""
        raw = b'data: {"result": "bare"}\r\n\r\n'
        events = parse_sse_stream(raw)
        assert len(events) == 1
        assert events[0].event is None
        assert events[0].data == '{"result": "bare"}'

    def test_unix_line_endings(self) -> None:
        """\\n 而非 \\r\\n。"""
        raw = b'event: message\ndata: {"x": 1}\n\n'
        events = parse_sse_stream(raw)
        assert len(events) == 1
        assert events[0].data == '{"x": 1}'

    def test_empty_stream(self) -> None:
        raw = b""
        events = parse_sse_stream(raw)
        assert len(events) == 0

    def test_comment_lines_ignored(self) -> None:
        raw = b': this is a comment\r\ndata: {"real": "event"}\r\n\r\n'
        events = parse_sse_stream(raw)
        assert len(events) == 1

    def test_line_without_colon(self) -> None:
        """不符合 'field: value' 格式的行应被忽略。"""
        raw = b"garbage line\r\ndata: ok\r\n\r\n"
        events = parse_sse_stream(raw)
        assert len(events) == 1
        assert events[0].data == "ok"

    def test_trailing_data_without_blank_line(self) -> None:
        """末尾没有空行的事件仍应被解析。"""
        raw = b'data: last event'
        events = parse_sse_stream(raw)
        assert len(events) == 1

    def test_utf8_content(self) -> None:
        """包含中文的 SSE 数据。"""
        raw = 'data: {"message": "你好世界"}\r\n\r\n'.encode("utf-8")
        events = parse_sse_stream(raw)
        assert len(events) == 1
        data = json.loads(events[0].data or "")
        assert data["message"] == "你好世界"


# ---- 流式 SSE 读取测试 -----------------------------------------------------


class _FakeResponse:
    """模拟 httpx Response，提供 aiter_lines() 迭代器。"""

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_read_sse_stream_result() -> None:
    """SSE 流含 result 事件 → 正确返回。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        'event: message',
        'data: {"result": {"content": [{"type": "text", "text": "ok"}]}}',
        '',
    ])
    result = await session._read_sse_stream(1, response)
    assert "ok" in result


@pytest.mark.asyncio
async def test_read_sse_stream_error() -> None:
    """SSE 流含 error 事件 → 正确抛出 JsonRpcError。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        'data: {"error": {"code": -32000, "message": "Tool not found"}}',
        '',
    ])
    with pytest.raises(JsonRpcError) as exc_info:
        await session._read_sse_stream(1, response)
    assert "Tool not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_sse_stream_skips_progress() -> None:
    """progress 通知被跳过，result 正确返回。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        'data: {"method": "notifications/progress", "params": {"progress": 50}}',
        '',
        'data: {"result": "done"}',
        '',
    ])
    result = await session._read_sse_stream(1, response)
    assert result == 'done'


@pytest.mark.asyncio
async def test_read_sse_stream_multiline_data() -> None:
    """多行 data 被正确拼接。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        'data: {"result":',
        'data: "multiline"}',
        '',
    ])
    result = await session._read_sse_stream(1, response)
    assert result == 'multiline'


@pytest.mark.asyncio
async def test_read_sse_stream_comment_ignored() -> None:
    """SSE 注释行被忽略。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        ': this is a comment',
        'data: {"result": "after_comment"}',
        '',
    ])
    result = await session._read_sse_stream(1, response)
    assert result == 'after_comment'


@pytest.mark.asyncio
async def test_read_sse_stream_no_result() -> None:
    """SSE 流在 result 出现前结束 → 抛出 JsonRpcError。"""
    cfg = Config()
    session = McpClientSession(cfg)
    response = _FakeResponse([
        'data: {"progress": 100}',
        '',
    ])
    with pytest.raises(JsonRpcError) as exc_info:
        await session._read_sse_stream(1, response)
    assert "未找到工具结果" in str(exc_info.value)


# ---- JSON-RPC 响应测试 ----------------------------------------------------


class TestJsonRpc:
    """测试 JSON-RPC 2.0 辅助类型。"""

    def test_success_response(self) -> None:
        resp = JsonRpcResponse(id=1, result={"tools": []})
        assert resp.id == 1
        assert resp.result == {"tools": []}
        assert resp.error is None

    def test_error_response(self) -> None:
        err = JsonRpcError(-32600, "Invalid Request")
        resp = JsonRpcResponse(id=1, error=err)
        assert resp.error is not None
        assert resp.error.code == -32600
        assert "Invalid Request" in str(resp.error)


# ---- 客户端集成测试（需要运行中的 UE）---------------------------------------


@pytest.mark.skip(reason="需要运行中的 UE MCP Server")
class TestClientIntegration:
    """需要 UE 编辑器运行且 MCP Server 已启用的集成测试。"""

    @pytest.fixture
    async def client(self) -> McpClientSession:
        cfg = Config(ue_port=8000)
        session = McpClientSession(cfg)
        await session.connect()
        yield session
        await session.close()

    @pytest.mark.asyncio
    async def test_initialize(self, client: McpClientSession) -> None:
        assert client.is_connected
        assert client.negotiated_version in (
            "2025-11-25",
            "2025-06-18",
            "2024-11-05",
        )

    @pytest.mark.asyncio
    async def test_ping(self, client: McpClientSession) -> None:
        """ping 应返回空 result。"""
        # ping 通过 _request 发送
        response, _ = await client._rpc("ping", {})
        assert response.error is None

    @pytest.mark.asyncio
    async def test_list_tools(self, client: McpClientSession) -> None:
        tools = await client.list_tools()
        # 至少应有 list_toolsets 等发现工具
        assert len(tools) > 0
        names = [t["name"] for t in tools]
        assert "list_toolsets" in names or len(names) > 3

    @pytest.mark.asyncio
    async def test_call_list_toolsets(self, client: McpClientSession) -> None:
        result = await client.call_tool("list_toolsets", {})
        assert result is not None
        # 应返回工具集目录文本
        assert isinstance(result, str)
        assert len(result) > 0
