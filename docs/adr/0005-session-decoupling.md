# 0005 — MCP Session、Agent Session、Conversation Session 三层解耦

**背景：** `mcp` Python SDK 为 MCP 连接提供 session 管理。如果 Harness 的 Agent Session 和 Conversation Session 与 MCP Connection Session 绑定，MCP 断开（UE 崩溃、网络抖动、LLM 客户端重启）将导致任务状态丢失。

**决策：** 三种 Session 生命周期完全解耦，互不绑定。

**三种 Session 定义：**

| Session 类型 | 职责 | 生命周期 | 管理者 |
|---|---|---|---|
| **MCP Connection Session** | 传输层连接状态：协议版本协商、HTTP 连接、SSE 流、`Mcp-Session-Id` | 连接 → 握手 → 断开 | `mcp` SDK（面向 LLM）/ `harness/client.py`（面向 UE） |
| **Agent Session** | 任务执行状态：State Cache、Task Memory、当前步骤、WorldState 快照 | 任务启动 → 完成/失败。**跨 MCP 连接存活** | Harness Agent Loop Controller |
| **Conversation Session** | 对话状态：用户与 LLM 之间的多轮对话历史、已澄清的意图 | 用户第一条消息 → 多轮交互 → 任务达成 | Harness + LLM 共同管理 |

**核心原则：**
- MCP 断开**不**丢失 Agent Session。UE 崩溃 → MCP Session 断开 → Harness 保持运行 → 重连 UE → 恢复 Agent Session → 继续执行任务。
- LLM 客户端重启**不**丢失 Agent Session。Claude Code 关闭 → MCP Session 断开 → 用户重连 → Harness 将当前 Agent Session 状态注入 System Context → LLM 继续执行。
- Agent Session 完成/用户显式终止时，Conversation Session 进入 Archive 状态。

**实施：**
- `harness/server.py` 中的 MCP Server 使用 `mcp` SDK 管理 MCP Connection Session
- `harness/state/cache.py` 中的 Agent Session 由 Harness 独立管理，拥有自己的 session_id 和持久化机制
- 当新 MCP 连接到达时，Harness 通过 `Mcp-Session-Id` 关联到现有 Agent Session（如果有的话），或者创建新的

**后果：**
- ~~Harness 需要在 MCP 连接断开时保存 Agent Session 状态到磁盘~~ **→ 已由 ADR 0008 推翻。** 磁盘持久化（Issue 013）于 2026-07-07 作废——UE 编辑器瞬时快照比持久化有意义，磁盘文件可能落后于 UE 实际状态。
- Skill 执行状态（当前步骤、已完成步骤）存储在 Agent Session 中，不存储在 MCP Session 中
- ~~如果 Harness 自身进程崩溃，Agent Session 从最后的持久化快照恢复（最终一致性）~~ **→ 同上，已作废。**
