# 00 — PiAgent 全景概览

**前置知识**：理解 UEMCPHarness 的 Interceptor 链、MCP Server/Client 模式、Skill 系统
**阅读目标**：形成 PiAgent 的宏观地图，理解 8 个 package 各自做什么、互相怎么粘合
**深度**：概念级，不涉及函数签名

---

## 1. PiAgent 是什么

PiAgent 是一个**可扩展的编程 Agent 框架**，核心能力：

- 连接 LLM（Claude / GPT / Gemini / 本地模型 …），执行 Agent 循环（发 prompt → 收回复 → 调工具 → 回收结果 → 再发 prompt …）
- 提供内置编程工具（read / write / edit / bash / grep / find / ls）
- 支持**扩展**（Extension）—— 第三方可以注册自己的工具、监听 Agent 生命周期事件、注入 system prompt
- 管理对话会话（Session）—— 持久化、恢复、分支、压缩
- 支持多种运行模式：交互式 TUI、单次运行、RPC 远程控制

**它不做什么**：
- 不管你用什么编辑器（只提供终端 TUI）
- 不内建权限沙箱（依赖外部容器/沙箱）
- 不绑定特定编程语言（工具是语言无关的）

---

## 2. 包的职责一句话总结

| Package | npm 包名 | 一句话 |
|---------|----------|--------|
| **agent** | `@earendil-works/pi-agent-core` | Agent 运行时的核心——Agent 循环、工具执行、事件发射。**不管工具是什么、session 怎么存、UI 怎么画** |
| **coding-agent** | `@earendil-works/pi-coding-agent` | **最大的包**——把 agent + ai + server + tui 全部粘合成一个可用的 CLI。包含 AgentSession、扩展系统、Session 管理、工具定义、技能系统 |
| **ai** | `@earendil-works/pi-ai` | 多 LLM provider 统一 API——发起 LLM 请求、处理流式响应。**只管"怎么跟 LLM 说话"** |
| **protocol** | `@earendil-works/pi-protocol` | 自定义二进制通信协议——客户端到服务器的消息格式（CBOR + 长度帧 + TypeBox 校验） |
| **server** | `@earendil-works/pi-server` | Agent 会话服务器——管理连接、认证、session CRUD、事件广播。**只管"怎么把 AgentSession 暴露给远程客户端"** |
| **client** | `@earendil-works/pi-client` | 远程客户端的协议层——连接服务器、获取 session handle、发送命令 |
| **tui** | `@earendil-works/pi-tui` | 终端 UI 库——组件树、diff 渲染、键盘处理、主题 |
| **storage** | — | SQLite 存储适配层（内部使用，迁移不涉及） |
| **evals** | — | 评估测试框架（内部使用，迁移不涉及） |

---

## 3. 包依赖关系

```
            ┌──────────────────────────────────┐
            │         coding-agent              │
            │  (CLI + Session + Extensions)     │
            └───┬───────┬───────┬──────┬────────┘
                │       │       │      │
       ┌────────┘  ┌────┘  ┌────┘ ┌────┘
       ▼           ▼       ▼      ▼
┌─────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐
│  agent  │  │   ai    │  │ protocol │  │  tui    │
│ (core)  │  │(LLM API)│  │ (wire)   │  │ (UI)    │
└─────────┘  └─────────┘  └──────────┘  └─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              ┌──────────┐ ┌──────────┐  ┌──────────┐
              │  server  │ │  client  │  │ storage  │
              └──────────┘ └──────────┘  └──────────┘
```

**关键依赖关系**：
- `coding-agent` 依赖其他所有包（它是"总装厂"）
- `agent` 只依赖 `ai`（Agent 循环需要 LLM 调用能力）
- `server` 和 `client` 都依赖 `protocol`（使用同一套消息格式）
- `server` 中的 `PiSessionRuntime` 接口 = `AgentSession` 的远程代理

**对我们迁移的意义**：
- 我们只需要理解 `agent` + `coding-agent` + `server` + `protocol` 四个包
- `tui`（UI 渲染）、`ai`（LLM HTTP 细节）、`storage`（SQLite）可以不深挖

---

## 4. 一个请求从头到尾经过哪些层

```
用户输入 "/skill:match-atmosphere 把 PointLight 改成红色"
  │
  ▼
┌─ CLI 层 (main.ts) ──────────────────────────────────────────┐
│ 参数解析 → 扩展加载 → session 创建/恢复 → 选择模式 (TUI)      │
│ → 调用 AgentSession.prompt(text)                            │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ AgentSession 层 (agent-session.ts) ─────────────────────────┐
│ 扩展命令检查 → input 事件（扩展可拦截）                        │
│ → Skill 展开 (/skill:name → 读文件 → 包装 XML)               │
│ → Prompt 模板展开 → before_agent_start 事件（改 system prompt）│
│ → Agent.prompt(messages)                                    │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ Agent 层 (agent.ts) ───────────────────────────────────────┐
│ 调用 runAgentLoop() → 进入 Agent 循环                        │
│                                                              │
│   ◄─── 循环 ───►                                             │
│                                                              │
│   1. transformContext (AgentMessage[] 变换)                  │
│   2. convertToLlm (过滤掉 UI-only 消息)                      │
│   3. prepareNextTurn (刷新 systemPrompt + tools)             │
│   4. streamFn → LLM 调用                                     │
│   5. LLM 返回 assistant message (可能含 toolCalls)           │
│   6. beforeToolCall → 扩展 tool_call 事件 (可 block/改参数)   │
│   7. tool.execute() → 工具实际执行                            │
│   8. afterToolCall → 扩展 tool_result 事件 (可改结果)         │
│   9. 工具结果发回 LLM → 下一轮                                │
│  10. shouldStopAfterTurn? → 检查是否停止                     │
│  11. getSteeringMessages? → 注入排队中的用户指令              │
│  12. getFollowUpMessages? → 最后再补一轮                     │
│                                                              │
│  → agent_end 事件                                            │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ AgentSession 层 (后处理) ───────────────────────────────────┐
│ auto-retry 检查 → auto-compaction 检查 → SessionManager 持久化 │
│ → agent_settled 事件 → UI 更新                               │
└─────────────────────────────────────────────────────────────┘
```

**关键观察**：
- AgentSession 不跑 Agent 循环，它**编排**"何时跑循环、循环跑完后做什么"
- Agent 循环本身是**无状态函数**（runAgentLoop），Agent 类给它注入状态
- 扩展事件贯穿全过程——从用户输入到 LLM 调用到工具执行到 compaction

---

## 5. 关键术语表

按出现顺序排列，后续文档反复使用这些术语。

| 术语 | 定义 | 类比 UEMCPHarness |
|------|------|------------------|
| **Agent** | 状态机包装器，持有 messages / tools / model，提供 `prompt()` / `continue()` / `abort()` | 无直接等价 — UEMCPHarness 的"Agent 循环"是 Claude Code 自己跑的 |
| **Agent 循环 (runAgentLoop)** | 核心 while 循环：发 prompt → 收 LLM 回复 → 执行工具 → 回收结果 → 再发 prompt | 对应 Claude Code 的 tool-use loop |
| **AgentContext** | 循环内的一帧快照：`{ systemPrompt, messages, tools }` | `assemble_system_prompt()` 的输出 + 当前消息历史 |
| **AgentMessage** | 消息的基类：user / assistant / toolResult / custom | 对应 `ToolCallCompleted` 的 `parsed_text` + UE 返回结果 |
| **AgentTool** | 工具定义：name + schema + execute() 函数 | `HarnessTool` |
| **AgentEvent** | Agent 生命周期事件：turn_start / message_start / tool_execution_start / agent_end / … | 无直接等价 — UEMCPHarness 没有事件总线 |
| **AgentSession** | 编排层，持有 Agent + SessionManager + ExtensionRunner + ResourceLoader + ModelRuntime。负责 prompt() 的完整生命周期 | `server.py` 的 `call_tool()` 外层 + `cli.py` 的 `cmd_start()` |
| **ExtensionRunner** | 扩展事件总线 + 工具/命令注册表。30+ 事件类型 | Interceptor 链的注册和执行 |
| **ExtensionAPI** | 扩展代码中可调用的 API（`pi.on("..."), pi.registerTool(...)` 等） | 无直接等价 — Harness 的 Interceptor 是硬编码的，没有动态注册 |
| **SessionManager** | 消息持久化：读写 JSONL 文件，管理分支/树结构 | `ToolCallLogger` + 部分 State Cache 持久化 |
| **ResourceLoader** | 加载文件资源：Skills / Prompts / Themes / AGENTS.md / CLAUDE.md / Extensions | `SkillRegistry` 的 YAML 加载 + `_build_instructions()` |
| **ModelRuntime** | 模型认证 + 注册 + 查找 + LLM 调用入口 | `VisionSubAgent._call_vision_api()` 的调用方式 |
| **Skill** | 前端 YAML 标记 + Markdown body 的 `.md` 文件，存储在 `.pi/skills/` | `.ue-harness/skills/*.yaml` |
| **ToolDefinition** | 扩展注册工具时使用的接口，比 AgentTool 多出 `promptSnippet` / `promptGuidelines` 等 system prompt 相关字段 | `HarnessTool` + `_build_instructions()` 中工具的文档说明 |
| **Steering** | 在 Agent 循环运行中排队注入的"打断"消息（比如用户中途说"等等，换个方向"） | 无直接等价 — MCP 协议无法在 tool call 过程中注入新指令 |
| **FollowUp** | 在 Agent 循环结束后才注入的"补充"消息（比如"刚才的改动不错，再看看这个"） | 无直接等价 |
| **Compaction** | 对话历史过长时，用 LLM 总结旧消息，只保留摘要 + 最近消息。释放上下文窗口 | 无直接等价 — UEMCPHarness 依赖 Claude Code 自身的 context 管理 |
| **Turn** | Agent 循环的一轮：一次 LLM 调用 + 可能的多次 tool call + 结果回收 | 一次 `call_tool()` 的 pre_call → post_call 全流程 |
| **PiSessionRuntime** | Server 端的接口：把 AgentSession 的能力（prompt / steer / abort）暴露给远程客户端 | MCP Server 的 `tools/call` 端点 |
| **PiServer** | 管理网络连接 + 认证 + session 列表 + 事件广播 | Harness MCP Server (`:9000`) |

---

## 6. 后续阅读路径

完成本文后，按以下顺序深入：

```
00-overview (你在这里)
  │
  ├──► 01-agent-core        Agent 循环怎么转的？工具怎么执行？
  ├──► 02-agent-session     AgentSession 怎么编排？prompt() 全链路
  ├──► 03-extension-system  扩展怎么注册？30+ 事件分别在什么时候触发？
  │
  ├──► 04-protocol-and-server   PiServer 怎么暴露 AgentSession？
  ├──► 05-session-manager       消息怎么存、怎么恢复？
  ├──► 06-tools-and-skills      工具怎么定义、Skill 怎么注入？
  │
  ├──► 07-main-and-modes    CLI 入口 → 全部组件怎么串起来？
  ├──► 08-full-lifecycle    用 UE 场景把 01-07 全部串联一遍
  │
  └──► 09-extension-tutorial    如果我要在 PiAgent 里加自定义逻辑，该写什么？
```
