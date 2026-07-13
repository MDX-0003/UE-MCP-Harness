# HANDOFF 0624 — CaptureAssetImage ReadTimeout 调查

**日期：** 2026-06-24
**状态：** 待验证（截图独立 session + 600s 超时方案）
**关联文档：** [PLAN_0621_HTTP_STREAM.md](PLAN_0621_HTTP_STREAM.md)

---

## 问题概述

Harness 调用 `take_screenshot` → `CaptureAssetImage` 在第二次调用时稳定报 `httpx.ReadTimeout`（120s）。UE 直连测试（`tool_verify_ue_vision.py`）可以成功。UE 端日志始终显示截图已完成（`Tracing Screenshot taken`），但 SSE 结果数据无法到达 Python 客户端。

---

## 根因链（已确认）

### 1. `_cancel_request` 误杀回调（已修复）

`client.py` 的 `call_tool()` 超时后发送 `notifications/cancelled`，删除了 UE Server 端 `Session->ActiveRequests` 中的请求条目。异步截图完成后回调检查 `ActiveRequests.Contains()` → false → 静默 return → SSE 数据永不发送。

**修复：** 移除 `_cancel_request` 调用。

### 2. UE HTTP Server `MultipleWriteStream` 跨线程写入缺陷（无法修复——UE 引擎代码）

`ProcessToolCallJsonRpcCall` (ModelContextProtocolServer.cpp:863-916) 分两次 `OnComplete`：

- **第 1 次（请求线程）：** 空 body + `MultipleWriteStream | HasAdditionalWrites` → 发 HTTP 头，保持连接
- **第 2 次（异步工具完成线程）：** SSE result + `MultipleWriteStream | SkipHeaderWrite` → 写数据

`GetSelectedAssets` 等瞬时工具在同一线程完成两次写入 → 正常。`CaptureAssetImage` 跨线程延迟写入 → 第二次 `OnComplete` 的数据可能不进入 TCP socket。这是 UE HTTP Server (`HttpConnection.cpp`) 的线程安全缺陷。

### 3. DELETE 杀死未完成的 session（已缓解）

Harness `close()` 发送 DELETE → UE Server `ProcessDeleteRequest` → `Sessions.RemoveAll()`。如果异步截图回调还未执行，session 被删除 → 回调中 `FindSession` 返回 null → 静默丢弃结果。

**修复：** 每次截图创建独立 `McpClientSession`，用完即关。

---

## 代码变更历史

### `harness/client.py`

| 变更 | 说明 |
|---|---|
| `post()` → `stream()` + `aiter_lines()` | SSE 增量读取（替代阻塞 `response.content`） |
| `Accept: text/event-stream, application/json` | 添加 MCP 规范要求的 Accept header |
| `ue_url_path` 移除 | URL 不再重复拼接（`/mcp/mcp` → `/mcp`） |
| `sse_read_timeout` 统一 120s | 消除类默认值 120s 与 `from_env` 默认值 60s 的冲突 |
| `_cancel_request` 移除 | 防止误删 UE Server `ActiveRequests` |
| `http2=False` | 明确禁用 HTTP/2 |
| 新增 `call_tool_blocking()` | `_rpc` 路径封装（post + response.content），用于长耗时工具 |
| SDK 重写 + 回退 | 尝试用 MCP SDK `streamablehttp_client`，引入 `notifications/initialized` 丢失 bug，已回退 |

### `harness/verification/capturer.py`

| 变更 | 说明 |
|---|---|
| 每次截图独立 `McpClientSession` | 全新 TCP 连接 + MCP session，用完 `close()` |
| `call_tool()` → `call_tool()` (流式) | 独立 session 第一个 SSE 流可靠 |
| 截图 session 超时 600s | `sse_read_timeout * 5`，防止异步调度延迟 >120s |

### `harness/config.py`

| 变更 | 说明 |
|---|---|
| `ue_url_path` 移除 | `ue_base_url` = `http://{host}:{port}/mcp`，直接完整 URL |
| `sse_read_timeout` 默认值统一 | `from_env` 不再覆盖为 60s，缺省使用类默认值 120s |

### UE C++ 诊断日志（项目副本）

`ModelContextProtocolServer.cpp` 中添加 `[DIAG]` 标记的 `UE_LOG`：

- 异步回调入口/出口
- `FindSession` 失败时 dump 所有 session ID
- `notifications/cancelled` 接收
- `DELETE` 请求接收（session 生命周期）

---

## 关键诊断发现

### UE 日志证据（第二次调用失败时）

```
[DIAG] About to OnComplete SSE result for 'CaptureAssetImage'    ← 回调执行到发送点
[DIAG] Async callback ENTERED: 'CaptureAssetImage'               ← 另一个回调进入
[DIAG] FindSession FAILED: SessionId=X, Total sessions=1, IDs: [Y]  ← 独立 session 已被 DELETE
[DIAG] Callback RETURN: Session valid=0                           ← 结果丢弃
Tracing Screenshot taken                                           ← 截图实际已完成
```

### Harness 日志证据

| 调用 | `[sse-stream]` 首行 | 结果 |
|---|---|---|
| `GetSelectedAssets` | 0.0s | ✅ |
| `GetOpenAssets` | 0.0s | ✅ |
| `CaptureAssetImage` 第 1 次 | 52.1s | ✅ |
| `CaptureAssetImage` 第 2 次 | 从未出现 | ❌ 120s ReadTimeout |

---

## 当前方案（待验证）

1. `capturer.py`：每次截图独立 `McpClientSession` + 600s 超时
2. `client.py`：流式 SSE（`stream()` + `aiter_lines()`），不含 `_cancel_request`
3. UE 端 `MultipleWriteStream` 问题通过独立 session 绕过（第一个 SSE 流总是可靠的）

---

## 建议下一步

1. 重启 Harness + UE，连续两次运行 `tool_verify_harness_vision.py`
2. 观察第二次测试是否在 52-150s 内返回（而非 120s ReadTimeout）
3. 如果成功：移除 UE 侧的 `[DIAG]` 诊断日志，恢复原始引擎插件
4. 如果仍然 120s 超时：在 `_read_sse_stream` 中启用 `[sse-stream]` DEBUG 日志，确认零字节问题是否仍然存在

## Suggested Skills

- `mattpocock-skills:diagnose` — 如果仍然超时，继续定位数据丢失点
- `mattpocock-skills:grill-with-docs` — 终结讨论、更新 CONTEXT.md
- `mattpocock-skills:handoff` — 将后续进展记录为新的 HANDOFF
