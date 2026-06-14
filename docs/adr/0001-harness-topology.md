# 0001 — Harness 作为 MCP Server + MCP Client 的双角色拓扑

**背景：** Harness 需要在外部 LLM 和 UE5.8 的 ModelContextProtocol MCP Server 之间中继工具调用。讨论了三种拓扑方案。

**决策：** Harness 是一个双角色进程——面向 LLM 是 MCP Server，面向 UE 是 MCP Client。两端均使用 JSON-RPC 2.0 over HTTP POST。Harness 是外部 Python 进程，不是 UE 插件。

LLM 永远不知道 UE MCP Server 的存在——Harness 是整个系统的唯一入口。

**否决的备选方案：**

- **A2（UE 端重写 MCP Server 插件）：** 需要复刻 `ModelContextProtocol` 插件的 session 管理、SSE framing、协议协商。现有 Server 已达到生产质量，重建没有价值。

- **A3（Harness 与 UE 间走自定义 HTTP/gRPC 协议）：** 增加第二条协议栈。Harness 需要维护两套协议（MCP for LLM + custom for UE）。UE 需要新建传输插件。MCP-to-MCP proxy 即可，无功能增益。

- **Harness 在 UE 内部作为 UEditorSubsystem：** 已否决——生命周期耦合导致 UE 崩溃 = Agent 状态丢失。`UToolsetRegistrySubsystem::Deinitialize()` 行为证实了这一点。

**实施细节：**

- 面向 UE 的 MCP Client 使用 `httpx` + 手写 JSON-RPC 2.0 + SSE 解析。UE MCP Server 的 `tools/call` 返回 SSE event-stream（两阶段：空流头 → 最终 result 帧），且 `initialize` 后所有请求必须携带 `Mcp-Protocol-Version` header。

- 面向 LLM 的 MCP Server 使用 `mcp` Python SDK。SDK 处理协议握手、SSE framing、session 管理——Harness 专注于业务逻辑。

- UE MCP Server 的延迟工具加载（CVar `ModelContextProtocol.DeferredToolLoading`）意味着 Harness 首次连接时必须调用 `list_toolsets` → `describe_toolset` → `load_toolset` 来预加载工具。`load_toolset` 从 LLM 可见工具列表中移除——Harness 内部管理。

**后果：**

- Harness 必须为 UE 端实现完整的 JSON-RPC 2.0 客户端语义（session 初始化、SSE 两阶段响应解析、`Mcp-Protocol-Version` header 管理、`notifications/tools/list_changed` 拦截）。
- Harness 必须为 LLM 端实现完整的 MCP Server 语义（session 管理、工具列表服务、SSE 流响应）。
- `mcp` Python 包提供 Server 端；`httpx` + 手写 SSE 解析处理 Client 端。
