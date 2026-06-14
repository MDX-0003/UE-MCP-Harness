# 002 — 工具发现与透传执行

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

Harness 连接 UE MCP Server，完成 MCP 握手（`initialize` → `notifications/initialized`），发现 UE 工具，预加载所有工具集，然后将工具调用从 LLM 透传到 UE 并返回结果。

这是 Harness 的第一个功能增量——LLM 可以通过 Harness 调用 UE 工具并获得结果，Harness 内部处理 `Mcp-Protocol-Version` header 管理、SSE 两阶段解析、`notifications/tools/list_changed` 拦截。LLM 和 UE 彼此不知道对方的存在。

## 验收标准

- [ ] `harness start --ue-port 8000` 连接运行中的 UE 编辑器（MCP Server 已启用）
- [ ] 启动时自动完成 MCP 握手：`initialize` → 获取 `Mcp-Session-Id` → `notifications/initialized`
- [ ] 启动时自动预加载工具：调 `list_toolsets` → 遍历 → 逐个 `load_toolset` → 等 `list_changed` SSE notification → 重取 `tools/list`
- [ ] LLM 调 `tools/list` → 返回完整的 UE 工具列表（~157 工具）——P0 阶段先全量透传，暂不过滤
- [ ] LLM 调 `tools/call`（如 `SceneTools.find_actors`）→ Harness 转发给 UE → 等 SSE 流结束 → 返回最终 `result` 帧给 LLM
- [ ] `Mcp-Protocol-Version` header 在所有 post-initialize 请求中正确携带
- [ ] UE 端 SSE 进度通知（`notifications/progress`）被 Harness 内部消费，不转发给 LLM
- [ ] UE 端 `notifications/tools/list_changed` 被 Harness 拦截 → 自动重取 `tools/list` → 更新内部缓存
- [ ] `harness/client.py` 的 `McpClientSession` 类有独立的单元测试（mock UE HTTP endpoint）
- [ ] 连接失败时输出可读错误信息（UE 未启动 / 端口不对 / MCP Server 未启用）

## 阻塞

- #001（Harness 骨架）

## 设计说明

UE MCP Server 的 `tools/call` 响应是 SSE 两阶段流：

1. UE 立即返回 `Content-Type: text/event-stream` 的空 SSE 流头
2. 工具完成后写入：`event: message\r\ndata: {jsonrpc: "2.0", id: N, result: {...}}\r\n\r\n`

Harness 的 `client.py` 必须实现 SSE 解析器处理这个两阶段流程。不是简单的 `response.json()`。
