# Handoff: CaptureAssetImage SSE Timeout — 疑难 Bug 调查全记录

**日期:** 2026-06-21
**状态:** 未解决 — UE 服务端挂死，需重启 UE 建立基线后继续

---

## 建议技能

接手此问题的 agent 应优先加载:
- `mattpocock-skills:diagnose` — 继续诊断循环
- `mattpocock-skills:grill-with-docs` — 与领域知识对照审问

## 相关文档

- 开发计划: [PLAN_0621_HTTP_STREAM.md](PLAN_0621_HTTP_STREAM.md) (已实施但未验证通过)
- 领域词汇: [CONTEXT.md](CONTEXT.md)
- ADR 0001: [0001-harness-topology.md](adr/0001-harness-topology.md) — Harness 双角色拓扑
- ADR 0006: [0006-vision-sub-agent.md](adr/0006-vision-sub-agent.md) — Vision Sub-Agent 设计
- UE MCP Server 源码: `Engine/Plugins/Experimental/ModelContextProtocol/Source/ModelContextProtocol/Private/ModelContextProtocolServer.cpp`
- Harness 客户端: `harness/client.py` (已改为 SDK 传输层)
- 测试文件: `tests/tool_verify_ue_vision.py`, `tests/tool_verify_harness_vision.py`

---

## Bug 核心症状

`CaptureAssetImage` (UE 视口截图工具) 在通过 Harness 调用时始终超时 (60-120s ReadTimeout)，但用户声称直连 UE MCP Server 的测试 (`tool_verify_ue_vision.py`) 可以成功。

**关键时间线:**
1. 初始: `tool_verify_ue_vision.py` 通过 → `tool_verify_harness_vision.py` 超时
2. 一次偶然: Harness 版首次调用成功 (~89s, 1024x489 截图)
3. 后续: Harness 版连续失败，即使重启 Harness
4. 最终: 直连测试 (`tool_verify_ue_vision.py`) 也超时 — **UE 服务端已挂死**

---

## 全部尝试记录 (按时间顺序)

### 尝试 1: 初始诊断 — `response.content` 阻塞 SSE
**假设:** `McpClientSession.call_tool()` 使用 `response.content` 同步读取 SSE 流，而 UE 服务器的 SSE `MultipleWriteStream` 保持连接打开，导致 `content` 永远阻塞。

**变更:** `harness/client.py:call_tool()` — 用 `httpx.stream()` + `aiter_lines()` 增量读取替换 `response.content`

**结果:** 超时仍然发生，但错误栈从 `post()` 内部移到 `_read_sse_stream()` 的 `aiter_lines()` 内部——证明流式读取已生效，但服务器未发送任何数据。

**学习:** `response.content` 不是根因，但 `stream()` 是正确的方向。

---

### 尝试 2: URL 收拢 — 消除 `/mcp/mcp` 重复
**假设:** `ue_base_url` 已包含 `/mcp`，`ue_url_path` 又追加 `/mcp`，导致请求发到 `/mcp/mcp`。

**变更:** 
- `config.py`: 移除 `ue_url_path` 字段，`ue_base_url` 直接硬编码完整 URL
- `client.py`: 所有 POST/DELETE 改用完整 URL，不再追加路径

**结果:** URL 变为 `http://127.0.0.1:8000/mcp`（正确），但超时未改善。

**学习:** URL 重复确实是 bug，但不是超时根因（`CaptureEditorImage` 和 `GetSelectedAssets` 走同样 URL 但能成功）。

---

### 尝试 3: `sse_read_timeout` 默认值统一
**假设:** 类默认值 120s 与 `from_env` 默认值 60s 冲突，生产环境用了 60s。

**变更:** `config.py:from_env()` — 移除 `"60.0"` env 默认值，未设置环境变量时依赖类默认值 120s。

**结果:** 超时从 60s 变为 120s，但 `CaptureAssetImage` 仍超时。

**学习:** 超时太短不是根因——服务器根本不发数据，120s 也不够。

---

### 尝试 4: `Accept` header
**假设:** SDK 设置了 `Accept: text/event-stream, application/json` 而我们没有。

**变更:** `client.py:_build_headers()` — 添加 `Accept` header。

**结果:** HTTP event hook 验证请求完全一致，超时未改善。

**学习:** Header 差异不是问题。

---

### 尝试 5: GET SSE 后台流
**假设:** MCP Streamable HTTP 规范要求客户端建立 GET SSE 连接。缺少此连接导致长耗时工具被服务器阻塞。

**变更:** `client.py:connect()` — `initialized` 后启动 `_read_get_sse()` 后台任务，维持 GET SSE 连接。

**结果:** GET `/mcp` 返回 **405 Method Not Allowed**。UE MCP Server 不支持 GET SSE。废除该方案。

**学习:** UE MCP Server 不实现 MCP 规范中的 GET SSE 通道。`ProcessGetRequest` (C++ line 1044) 直接返回 405 BadMethod。

---

### 尝试 6: `httpx.AsyncClient` 配置差异
**假设:** `base_url` vs 完整 URL 导致 httpx 行为不同。

**变更:** 移除 `base_url`，改用完整 URL；复刻 SDK 的 `httpx.Timeout(timeout, read=...)`。

**结果:** HTTP event hook 确认请求完全一致（Method, URL, Headers, Content），超时未改善。

**学习:** 传输层配置与 SDK 完全对齐。

---

### 尝试 7: 纯 httpx 绕过 McpClientSession
**假设:** `McpClientSession` 包装层有问题。

**变更:** 直接用 `httpx.AsyncClient` → `stream("POST", ...)` → `aiter_lines()`，绕过所有 Harness 代码。

**结果:** 同样超时。证明问题不在 `McpClientSession`。

**学习:** 手写的任何 HTTP 传输方式都超时。

---

### 尝试 8: `response.aclose()` + `notifications/cancelled`
**假设:** ReadTimeout 后客户端关闭 TCP，但 UE 服务器的 async tool 回调仍尝试写 SSE 结果到 dead connection → 服务端 HTTP writer 阻塞 → 后续请求被卡死。

**代码证据 (UE C++):**
- `ModelContextProtocolServer.cpp:863-869`: 立即发送空 SSE 响应头 + `MultipleWriteStream` flag
- `ModelContextProtocolServer.cpp:873-917`: `Tool->RunAsync(...)` 异步执行，完成回调中第二次 `OnComplete` 发送结果
- `ModelContextProtocolServer.cpp:1072`: DELETE 只是移除 session，不取消进行中的 async tool
- 完成回调中 `FindSession` → 若 session 已移除则静默返回——但如果 TCP 还"存活"（keep-alive），`OnComplete` 可能阻塞

**变更:** 
- `call_tool()`: 异常时显式 `await response.aclose()` → 发送 TCP RST
- `call_tool()`: 异常时发送 `notifications/cancelled` → 服务端取消工具执行
- `close()`: 改进日志和错误处理

**结果:** `_cancel_request` 成功发送 (`HTTP 202`)，但超时仍发生——cancel 在 120s 后才发，为时已晚。

**学习:** 取消机制正确但不够及时。需要更早检测超时或在 120s 内完成渲染。

---

### 尝试 9: `Connection: close` header
**假设:** UE 服务器对 SSE 设置了 `Connection: keep-alive` (C++ line 864)，导致 TCP 连接复用问题。

**结果:** **UE 编辑器崩溃！** 断言失败: `EHttpConnectionState::AwaitingProcessing == SharedThisPtr->GetState()` at `HttpConnection.cpp:184`。`Connection: close` 与 `MultipleWriteStream` 冲突。

**学习:** 不能在 SSE 流上强制 `Connection: close`。立即回退。

---

### 尝试 10: 成功路径 `response.aclose()` 关闭连接
**假设:** 成功读取 SSE 后不显式关闭连接，httpx 可能将连接归还连接池，后续请求复用时混淆。

**变更:** SSE 成功读取后显式 `await response.aclose()`。

**结果:** 第一次调用仍然失败（全新 session + 全新 UE 重启后首次调用就超时）。

**学习:** 连接复用不是根因。即使是全新的一切，第一次调用也超时。

---

### 尝试 11: `read=None` 无超时
**假设:** 服务端数据最终会到达，只是非常慢。

**变更:** `httpx.Timeout(read=None)` 禁用读超时，`post()` 阻塞等待。

**结果:** 等待 4+ 分钟，无任何响应体数据到达。服务器永不返回 SSE 结果。

**学习:** 不是超时问题——服务器根本不完成 `CaptureAssetImage` 的 SSE 响应。

---

### 尝试 12: 用 MCP SDK 替换手写传输层
**假设:** SDK 的 `streamablehttp_client` + `ClientSession` 与手写传输有某种未被发现的差异。

**变更:** `McpClientSession` 内部完全用 `streamablehttp_client` + `ClientSession` 替代手写的 HTTP/SSE 代码。

**结果:** **仍然超时。** 即使使用与直连测试完全相同的 SDK 传输层，`CaptureAssetImage` 也超时。

**学习:** 问题不在传输层。SDK 本身通过我们的代码调用也失败了。

---

### 尝试 13: 最终验证
**假设:** UE 服务端当前已挂死，直连测试也无法通过。

**变更:** 直接运行 `tool_verify_ue_vision.py`。

**结果:** Exit code 124 (timeout) — 连 `"1. initialize UE..."` 都没打印。**直连测试也挂了。**

**学习:** UE MCP Server 当前处于挂死状态，大概率是之前多次 Harness 测试遗留的僵尸 session 或未清理的 `ActiveRequests`/`MultipleWriteStream` 导致的。重启 UE 编辑器后应恢复。

---

## 当前代码状态

### 已修改并保留的文件

| 文件 | 关键变更 |
|---|---|
| `harness/config.py` | 移除 `ue_url_path`; `ue_base_url` 完整 URL; `sse_read_timeout` 默认值统一 120s |
| `harness/client.py` | `McpClientSession` 改为 SDK 传输层 (最新版本); `_cancel_request`; `Accept` header; 旧手写代码仍保留但不再调用 |
| `tests/test_client.py` | 新增 6 个 `_read_sse_stream` 流式 SSE 测试 |
| `tests/test_config.py` | 移除 `ue_url_path` 引用 |
| `docs/PLAN_0621_HTTP_STREAM.md` | 完整开发计划 |

### `client.py` 当前架构

`McpClientSession` 现在内部使用 MCP SDK:
- `connect()`: `streamablehttp_client().__aenter__()` → `ClientSession` → `initialize()`
- `call_tool()`: 委托给 `_sdk_session.call_tool()`
- `close()`: `streamablehttp_client().__aexit__()`
- 旧的手写代码 (`_rpc`, `_read_sse_stream`, `_cancel_request`, `_build_headers`, `parse_sse_stream`, `SseEvent`, `JsonRpcError`, `JsonRpcResponse`) 仍保留在文件中但不再被调用

---

## 根因分析

### 确定的事实
1. `CaptureAssetImage` 不是超时问题——服务器根本不发送任何 SSE 数据
2. 不是传输层问题——手写 HTTP、SDK 传输、纯 httpx 都失败
3. `CaptureEditorImage` 和 `GetSelectedAssets` 始终正常工作（亚秒~16s）
4. 服务器发送 HTTP 200 + SSE headers 后，从不发送 SSE body
5. 直连测试最终也超时，说明 UE 服务端进入了不可恢复的状态

### 最可能的根因
UE MCP Server 的 `MultipleWriteStream` 机制有缺陷。`ProcessToolCallJsonRpcCall` (C++ line 779-920) 中:
1. 立即通过 `OnComplete` 发送空 SSE 响应头 (开启 `MultipleWriteStream`)
2. 异步执行工具 (`Tool->RunAsync`)
3. 完成回调中再次 `OnComplete` 发送 SSE 结果

**如果客户端在步骤 2 期间断开 TCP 连接，`OnComplete` 回调可能:**
- 尝试写 dead connection → UE HTTP server 内部 writer 阻塞
- `MultipleWriteStream` 标志使得 HTTP server 一直等待更多写入
- 后续请求 (即使是新 session) 被阻塞在同一个 HTTP writer 队列中

**证据:** `Connection: close` header 直接触发 UE HTTP Server 断言崩溃 (`HttpConnection.cpp:184`)，证明 SSE 连接状态管理在 UE 端有严格的状态机约束，容易出现非法状态转换。

### 为什么第一次偶尔成功
Harness 的一次成功调用 (~89s) 与失败的调用使用同一 UE session，但在成功那次，服务端 HTTP writer 尚未进入错误状态。一旦某次调用触发 writer 阻塞，所有后续调用 (包括新 session) 都受影响——直到重启 UE。

---

## 下一步建议

### 立即行动
1. **重启 UE 编辑器** — 清除挂死的 HTTP writer 状态
2. 重启后立即运行 `tool_verify_ue_vision.py` 建立基线
3. 基线通过后，运行 `tool_verify_harness_vision.py`
4. 观察第一次调用是否成功，第二次是否失败

### 后续调查方向
如果 Harness 在 UE 重启后仍然无法截图 (即使基线通过):

1. **在 Harness 启动时跳过 preload** — 测试是否是加载 211 个工具集导致的 UE 端副作用
2. **对比 SDK 版本** — 确认 Harness 导入的 `mcp` 包版本与直连测试一致
3. **UE 端加日志** — 在 `ProcessToolCallJsonRpcCall` 和完成回调中添加 UE_LOG，追踪工具执行和 OnComplete 调用是否正常触发
4. **考虑使用 `notifications/cancelled` 作为超时前的预防措施** — 在客户端检测到 SSE 流空闲超过 N 秒时主动取消并重试
5. **调查 UE HTTP Server 的 `MultipleWriteStream` 生命周期** — 理解 `OnComplete` 在 TCP 断开后的行为，是否需要 UE 端修复
