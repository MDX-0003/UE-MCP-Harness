---
name: mcp-transport-streamable-http
description: Harness MCP Server 传输层同时支持 Streamable HTTP 和 SSE
metadata:
  type: project
---

# MCP Transport Layer

Harness 的 `transport.py` 同时支持两种 MCP 传输协议：

- **Streamable HTTP** (`POST /mcp`): MCP 2025 规范，Claude Code VS Code 扩展使用此协议。`StreamableHTTPServerTransport` + `_StreamableHttpMiddleware`。
- **SSE** (`GET /sse` + `POST /messages/`): 旧版协议，保留向后兼容。

**Claude Code 连接配置** (`.claude/mcp.json`):
```json
{
  "mcpServers": {
    "ue-harness": { "type": "http", "url": "http://127.0.0.1:9000/mcp" }
  }
}
```

**Python 测试注意事项**:
- `streamablehttp_client()` 返回 3 元组 `(read, write, get_session_id)`，解包用 `streams[0], streams[1]`
- 截图工具异步耗时 18-30s，需要 `sse_read_timeout=120`
- Harness 自身的 `McpClientSession` 默认 `sse_read_timeout=60`

Why: MCP 协议升级（SSE → Streamable HTTP）是连接 VS Code 的必要条件。旧 SSE 客户端返回 "Not Found"。
How to apply: 新增 MCP Client 时，使用 `type: "http"` 指向 `/mcp`。Python 测试记得加 sse_read_timeout。
