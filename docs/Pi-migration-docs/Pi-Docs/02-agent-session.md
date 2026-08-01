# 02 — AgentSession：编排层

**对应的源文件**：[packages/coding-agent/src/core/agent-session.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/agent-session.ts)（~3333 行）
**依赖**：[packages/coding-agent/src/core/agent-session-runtime.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/agent-session-runtime.ts)、[agent-session-services.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/agent-session-services.ts)

**前置阅读**：[00-overview](00-overview.md)、[01-agent-core](01-agent-core.md)
**阅读目标**：理解 prompt() 从用户输入到 Agent 循环再到后处理的完整链路，理解 ModelRuntime 和 AgentSessionRuntime

---

## 全局地图：AgentSession 在完整链路中的位置

```
                    ┌─────────────────────────────────────────┐
                    │  07-main-and-modes: main()              │
                    │  CLI 启动                                │
                    │    → createAgentSessionServices()        │
                    │      → 创建 ModelRuntime                │
                    │      → 创建 SettingsManager             │
                    │      → 创建 ResourceLoader              │
                    │    → createAgentSession()                │
                    │      → 创建 Agent ← 见 01-agent-core    │
                    │      → 创建 AgentSession ← 见本文档     │
                    │    → 创建 AgentSessionRuntime            │
                    │      (session 容器) ← 见第 8 节         │
                    │    → 进入模式 (TUI/Print/RPC)           │
                    │    → 等待用户输入                        │
                    └────────────────┬────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────┐
│  02-agent-session: AgentSession ←── 本文档范围              │
│                                                             │
│  用户输入到达                                               │
│    ├── 1. 扩展命令检查 (/command → 同步执行)                │
│    ├── 2. input 事件 (扩展可拦截/转换)                       │
│    ├── 3. Skill 展开 (/skill:name → 读文件 → 包装 XML)     │
│    ├── 4. Prompt 模板展开                                   │
│    ├── 5. 流式/非流式分支 (steer/followUp 排队)             │
│    ├── 6. before_agent_start 事件 (注入 system prompt)      │
│    │                                                        │
│    ├── 7. ╔═══════════════════════════════════════════╗     │
│    │      ║  01-agent-core: Agent ← 跳转参考          ║     │
│    │      ║  agent.prompt(messages)                   ║     │
│    │      ║    → runAgentLoop() ... agent_end         ║     │
│    │      ╚═══════════════════════════════════════════╝     │
│    │                                                        │
│    └── 8. 循环后处理                                        │
│          ├── auto-retry 检查 ← 见第 5 节                    │
│          ├── auto-compaction 检查 ← 见第 4 节               │
│          ├── agent.hasQueuedMessages() → agent.continue()   │
│          └── agent_settled 事件                             │
│                                                             │
│  Session 切换 (用户按 /new /fork /resume)                    │
│    └── AgentSessionRuntime ← 见第 8 节                      │
└─────────────────────────────────────────────────────────────┘
```

**本文档覆盖**：上图中 AgentSession 的全部范围。也包括 AgentSessionRuntime 和 ModelRuntime。

---

## 调用上下文：AgentSession 怎么被创建、怎么被调用、调用后发生什么

### 谁创建了 AgentSession

在 `07-main-and-modes` 的 `createAgentSession()` 中：

```typescript
// 1. 先创建基础设施（AgentSessionServices）
const services = await createAgentSessionServices({
  cwd: "/path/to/project",
  // → 输入：工作目录
  // → 返回：{ modelRuntime, settingsManager, resourceLoader }
  // → 修改：auth.json/models.json 被读取到内存
});

// 2. 创建 Agent（见 01-agent-core 的调用上下文）
const agent = new Agent({ /* ... */ });

// 3. 创建 AgentSession
const session = new AgentSession({
  agent,                        // ← 输入：已创建好的 Agent 实例
  sessionManager: SessionManager.create(cwd, sessionDir),
                                // ← 输入：Session 持久化管理器
  settingsManager: services.settingsManager,
                                // ← 输入：用户设置
  resourceLoader: services.resourceLoader,
                                // ← 输入：Skills/Prompts/AGENTS.md 等文件资源
  modelRuntime: services.modelRuntime,
                                // ← 输入：模型认证 + 调用能力（见第 7 节）
  cwd,                          // ← 输入：工作目录
  initialActiveToolNames: ["read", "bash", "edit", "write"],
                                // ← 输入：默认启用的工具名列表
  customTools: [...],           // ← 输入：SDK 注册的额外工具
});

// 构造函数内部立即做了这些事：
// 1. agent.subscribe(_handleAgentEvent) → Agent 事件 → 映射为 Session 事件 + 持久化
// 2. _installAgentToolHooks()        → agent.beforeToolCall/afterToolCall 被覆盖
// 3. _installAgentNextTurnRefresh()  → agent.prepareNextTurnWithContext 被覆盖
// 4. _buildRuntime()                 → 创建 ExtensionRunner + 刷新工具注册表
```

**输入**：Agent + 5 个服务（SessionManager/SettingsManager/ResourceLoader/ModelRuntime/cwd）+ 初始配置。

**修改**：Agent 的三个钩子被 AgentSession 覆盖（`beforeToolCall`、`afterToolCall`、`prepareNextTurnWithContext`）。这意味着 Agent 原本构造时的这些钩子的值被**丢弃**了。

### AgentSession 被包装在什么里面

```typescript
// main() 中：
const runtime = await createAgentSessionRuntime(createRuntime, options);
// → AgentSessionRuntime._session = session
// → AgentSessionRuntime._services = services
// → AgentSessionRuntime.createRuntime = createRuntime (工厂函数，供后续 new/fork 重用)
```

**返回**：一个 `AgentSessionRuntime` 实例。外部代码通过 `runtime.session` 访问 AgentSession。

### 谁调用了 AgentSession.prompt()，传入什么

取决于运行模式：

```typescript
// TUI 模式：用户在输入框中按回车
// InteractiveMode → runtime.session.prompt(userText, { streamingBehavior: "steer" })
//                                                         ← 如果 Agent 正在运行，排队为"打断"消息

// Print 模式：命令行直接传入
// PrintMode → runtime.session.prompt(args.text)
//                                 ← Agent 不在运行，直接执行

// RPC 模式：远程客户端发来 prompt 命令
// RpcMode → runtime.session.prompt(request.text)
```

### prompt() 调用后发生了什么（连锁影响）

```
session.prompt("把 PointLight 改成红色")
  │
  ├── ▶ 步骤 1-6（前处理，都在 AgentSession 内部）：
  │     ├── _tryExecuteExtensionCommand()  → 可能匹配 /command → 直接返回（不调 Agent）
  │     ├── ExtensionRunner.emitInput()    → 修改：扩展可以重写输入文本或标记为"已处理"
  │     ├── _expandSkillCommand()          → 修改：输入从 "/skill:name args" 变为完整 skill 内容
  │     ├── expandPromptTemplate()         → 修改：输入中的 /template 被展开
  │     ├── 流式分支：
  │     │   → isStreaming=true → steer()/followUp() → 消息入队 → 返回（不调 Agent）
  │     │   → isStreaming=false → 继续
  │     ├── ExtensionRunner.emitBeforeAgentStart()
  │     │   → 修改：agent.state.systemPrompt（可能被扩展覆盖为本轮专用提示词）
  │     │   → 修改：消息列表（可能被扩展追加 custom messages）
  │     └── modelRuntime.getAuth()         → 读取：如果认证失败，抛出异常
  │
  ├── ▶ 步骤 7（调用 Agent，参考 01-agent-core）：
  │     agent.prompt(messages)
  │       → 修改：agent.state.messages（每轮 turn 追加 assistant + toolResult）
  │       → 修改：agent.state.isStreaming = true → false
  │       → 发射：所有 AgentEvent（被 AgentSession._handleAgentEvent 消费）
  │       → 返回：void（Agent 不返回值，结果通过 agent.state 和事件获取）
  │       → 连锁影响：
  │         _handleAgentEvent:
  │           message_end → sessionManager.appendMessage()  ← 写入 JSONL 文件
  │           message_end → ExtensionRunner.emit()          ← 扩展收到事件
  │
  ├── ▶ 步骤 8（后处理，回到 AgentSession）：
  │     _handlePostAgentRun():
  │       ├── _isRetryableError()? → 移除最后一条 error message → 返回 true
  │       │   → agent.continue() → 重复步骤 7
  │       ├── _checkCompaction()? → compact() → 替换 agent.state.messages
  │       │   → 修改：agent.state.messages 被整体替换（旧消息 → 摘要 + 最近消息）
  │       │   → 修改：session 文件追加 compaction entry
  │       │   → agent.continue() → 重复步骤 7
  │       └── agent.hasQueuedMessages()?
  │           → agent.continue() → 重复步骤 7
  │
  └── ▶ 最终：
        _systemPromptOverride = undefined  ← 清除临时 system prompt
        _isAgentRunActive = false         ← 标记空闲
        _emitAgentSettled()               ← 通知 UI + 扩展
```

---

## 1. AgentSession 的定位

**谁持有 AgentSession？** `AgentSessionRuntime._session` 持有当前活跃的 AgentSession。每次 /new 或 /fork 会销毁旧实例、创建新实例。

---

## 1. AgentSession 的定位

**一句话**：AgentSession 不跑 Agent 循环，它编排"谁在什么时候做什么"。

```
                         AgentSession
                    （编排层，~3333 行）
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐
│    Agent     │  │ SessionManager   │  │ ExtensionRunner  │
│ (跑循环)     │  │ (持久化消息)     │  │ (事件总线+工具)  │
└──────────────┘  └──────────────────┘  └──────────────────┘
        │                                          │
        ▼                                          ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  ModelRuntime│  │  ResourceLoader  │  │SettingsManager  │
│ (模型认证)   │  │ (Skills/Prompts) │  │ (用户偏好)      │
└──────────────┘  └──────────────────┘  └──────────────────┘
```

**AgentSession 和 Agent 的分工**：

| AgentSession 管 | Agent 管 |
|---|---|
| 用户输入到达后、到达 Agent 前的全部处理 | Agent 循环内部的状态管理 |
| 扩展命令 (/command) 的同步执行 | LLM 调用 (streamFn) |
| Skill 展开、模板展开 | 工具执行三阶段 |
| before_agent_start 事件（system prompt 注入点） | turn 级别的事件发射 |
| auto-retry、auto-compaction（循环后处理） | 消息队列 (steer/followUp) |
| Session 持久化（message_end → 写入磁盘） | |
| Model / Thinking Level 切换 | |
| Extension 绑定和生命周期管理 | |

---

## 2. prompt() 的完整链路（深度级）

> **📍 当前位置**：在 [全局地图](#全局地图agentsession-在完整链路中的位置) 的"用户输入到达"节点之后。本章覆盖步骤 1-8（从用户按下回车到 Agent 循环完成后的后处理）。

这是理解 PiAgent 的最核心路径。从用户按下回车到 LLM 开始回复，经过了 8 个步骤。

### 步骤 1：扩展命令检查

```typescript
// 如果输入以 / 开头，先检查是否为扩展命令
if (text.startsWith("/")) {
  const handled = await this._tryExecuteExtensionCommand(text);
  if (handled) return; // 扩展命令已处理，不发往 Agent
}
```

**副作用**：如果匹配了扩展命令，整个 prompt 链路到此结束。扩展命令**不可能排队**——只能在 Agent 空闲时执行。

### 步骤 2：input 事件（扩展可拦截）

```typescript
// 发射到所有扩展的 "input" handler
const inputResult = await this._extensionRunner.emitInput(text, images, source, streamingBehavior);

if (inputResult.action === "handled") return;           // 扩展消费了，不发往 Agent
if (inputResult.action === "transform") {
  text = inputResult.text;                               // 扩展改了输入文本
  images = inputResult.images ?? images;                 // 扩展可能也改了图片
}
```

**副作用**：扩展可以**重写**用户的输入。这是"用户输入 → 实际发给 LLM 的内容"之间的第一个变换点。

### 步骤 3：Skill 展开

```typescript
// "/skill:match-atmosphere 把 PointLight 改成红色"
// → skillName = "match-atmosphere", args = "把 PointLight 改成红色"
// → 读 .pi/skills/match-atmosphere.md
// → body = stripFrontmatter(skillFileContent)
// → 返回:
//   <skill name="match-atmosphere" location="/path/to/skill.md">
//   内容从 /path/to/skill.md 所在的 baseDir 开始相对路径
//
//   {完整的 skill markdown 内容}
//   </skill>
//
//   把 PointLight 改成红色
```

**副作用**：用户输入从一条 `"/skill:match-atmosphere ..."` 变成一条包含完整 skill 内容的超长消息。Skill 文件内容直接 inline 展开。

### 步骤 4：Prompt 模板展开

```typescript
// 如果输入匹配某个 prompt template（如 "/report"），展开为模板内容
expandedText = expandPromptTemplate(expandedText, this.promptTemplates);
```

**副作用**：类似于 Skill 展开，但使用的是 `.pi/prompts/*.md` 中的模板。

### 步骤 5：流式/非流式分支

```typescript
if (this.isStreaming) {
  // Agent 正在运行中 → 不能直接 prompt()，必须排队
  if (options.streamingBehavior === "followUp") {
    await this._queueFollowUp(expandedText, images);  // "干完再说"
  } else {
    await this._queueSteer(expandedText, images);     // "打断当前"
  }
  return;
}
// 否则 → 空闲状态 → 继续下一步
```

**副作用**：
- steer → 消息进入 steering queue，在下一轮 turn 开始前注入
- followUp → 消息进入 followUp queue，在 Agent 即将空闲时注入

### 步骤 6：before_agent_start 事件（system prompt 注入点）

```typescript
// 发射 before_agent_start 事件，扩展可以：
// - 注入 custom messages（如系统通知、状态更新）
// - 修改 systemPrompt（"把系统提示词换成我的版本"）
const result = await this._extensionRunner.emitBeforeAgentStart(
  expandedText, images,
  this._baseSystemPrompt,          // 当前的基础 system prompt
  this._baseSystemPromptOptions,   // 构建 system prompt 所用的选项
);

// 应用扩展注入的 custom messages
if (result?.messages) {
  for (const msg of result.messages) {
    messages.push(msg);  // 追加到 user message 前面
  }
}

// 应用扩展修改的 system prompt
if (result?.systemPrompt !== undefined) {
  this._systemPromptOverride = result.systemPrompt;
  this.agent.state.systemPrompt = result.systemPrompt;
} else {
  // 重置回基础 system prompt（上轮可能被改了）
  this._systemPromptOverride = undefined;
  this.agent.state.systemPrompt = this._baseSystemPrompt;
}
```

**副作用**：
- `agent.state.systemPrompt` 可能被覆盖。这个覆盖只在**本轮**有效（下一轮会被 `prepareNextTurn` 刷新回 `_baseSystemPrompt`）
- Custom message 追加到消息列表，被发送给 Agent 循环
- **这是 UEMCPHarness 中 `_build_instructions()` 的等价位置**

### 步骤 7：调用 Agent.prompt()

```typescript
// 拼装最终消息列表
const userMessage = {
  role: "user",
  content: [{ type: "text", text: expandedText }, ...images],
  timestamp: Date.now(),
};
messages.push(userMessage);

// 注入 "nextTurn" 类型的排队消息（扩展通过 sendCustomMessage 发的）
for (const msg of this._pendingNextTurnMessages) {
  messages.push(msg);
}

// 调用 Agent
await this._runAgentPrompt(messages);
```

### 步骤 8：_runAgentPrompt() 内部

```typescript
async _runAgentPrompt(messages) {
  this._isAgentRunActive = true;  // 标记"正在运行"
  try {
    await this.agent.prompt(messages);  // 启动 Agent 循环

    // Agent 循环结束后的 while 循环：
    // 每次 agent 停下后，检查是否有更多事情要做
    while (await this._handlePostAgentRun()) {
      await this.agent.continue();  // 继续下一轮
    }
  } finally {
    this._systemPromptOverride = undefined;  // 清除临时 system prompt
    this._flushPendingBashMessages();
    await this._emitAgentSettled();          // 发射 agent_settled 事件
  }
}
```

**`_handlePostAgentRun()` 做的事**：
1. 检查上次 assistant message 是否可重试 → `_prepareRetry()`
2. 检查是否需要 auto-compaction → `_checkCompaction()`
3. 检查是否有队列中的消息 → `agent.hasQueuedMessages()`
4. 如果以上任一返回 true → 返回 true → 外层 while 循环调用 `agent.continue()`

**副作用**：
- `_isAgentRunActive` 控制 `this.isStreaming` 的返回值。当它为 true 时，新的 prompt() 调用会走 steering/followUp 排队路径。
- `_systemPromptOverride` 在 finally 中清除，确保下一轮恢复基础 system prompt。

---

## 3. Agent 事件 → Extension 事件 → Session 事件的映射

> **📍 当前位置**：这个映射贯穿整个 prompt() 链路。`agent.subscribe(_handleAgentEvent)` 是 AgentSession 构造时注册的**内部 handler**——每次 Agent 循环发射任何事件，这个 handler 都会运行。本章讲解 handler 内部做了什么。

### 3.1 事件处理链

```
Agent 循环发射 AgentEvent
  │
  ▼
agent.subscribe(_handleAgentEvent)  ← AgentSession 的内部 handler
  │
  ├──► _emitExtensionEvent(event)  ← 映射为扩展事件
  │      extensionRunner.emit(extensionEvent)
  │
  ├──► this._emit(agentSessionEvent) ← 映射为 Session 事件
  │      通知 UI 层
  │
  └──► 持久化
        message_end → sessionManager.appendMessage(event.message)
        custom message → sessionManager.appendCustomMessageEntry(...)
```

### 3.2 事件映射表

| AgentEvent | 何时发生 | → ExtensionEvent | → SessionEvent | → 磁盘写入 |
|-----------|---------|-----------------|---------------|-----------|
| `agent_start` | 循环开始 | `agent_start` | 同 | 无 |
| `turn_start` | 每轮开始 | `turn_start` | 同 | 无 |
| `message_start` (user) | 用户消息开始 | `message_start` | 同（同时清理 steering/followUp 队列显示） | 无 |
| `message_start` (assistant) | LLM 开始回复 | `message_start` | 同 | 无 |
| `message_update` (assistant) | 流式 token 到达 | `message_update` | 同 | 无 |
| `message_end` (assistant) | LLM 回复完成 | `message_end`（可被扩展替换消息内容） | 同 | `appendMessage()` — **此时消息写入 JSONL** |
| `message_end` (user) | 用户消息完成 | `message_end` | 同 | `appendMessage()` |
| `message_end` (toolResult) | 工具结果完成 | `message_end` | 同 | `appendMessage()` |
| `tool_execution_start` | 工具开始执行 | `tool_execution_start` | 同 | 无 |
| `tool_execution_update` | 工具执行中 | `tool_execution_update` | 同 | 无 |
| `tool_execution_end` | 工具执行结束 | `tool_execution_end` | 同 | 无 |
| `turn_end` | 每轮结束 | `turn_end`（带 toolResults） | 同 | 无 |
| `agent_end` | 循环结束 | `agent_end` | 同（附加 `willRetry` 标记） | 无 |
| — | — | — | `agent_settled`（所有后处理完成） | 无 |
| — | — | — | `compaction_start/end`（压缩开始/结束） | `appendCompaction()` |
| — | — | — | `auto_retry_start/end`（自动重试） | 无 |

**关键设计**：
- `message_end` 是唯一的**持久化触发点**。在此之前，消息只存在于内存中。
- Extension 可以通过 `message_end` 事件的 handler **替换消息内容**（`_replaceMessageInPlace`），这会影响持久化时的内容。但消息的角色（user/assistant/toolResult）不能改变。
- Extension 可以**不返回**任何结果，此时原始消息原样持久化。

---

## 4. Compaction（上下文压缩）流程（深度级）

> **📍 当前位置**：[全局地图](#全局地图agentsession-在完整链路中的位置) 中步骤 8 的 `auto-compaction 检查` 分支。在 Agent 循环的 `agent_end` 之后，`_handlePostAgentRun()` 调用 `_checkCompaction()`。

### 4.1 为什么要 Compaction

LLM 有上下文窗口限制。当对话历史超过阈值，AgentSession 自动触发压缩——用 LLM 总结旧消息，只保留摘要 + 最近的消息。**注意**：Agent 循环本身不做 compaction。它由 AgentSession 在循环**之外**管理。

### 4.2 两种触发方式

| 触发方式 | 条件 | 压缩后是否自动重试 |
|---------|------|-----------------|
| **overflow** | LLM 返回 context overflow 错误，或 usage 超过 contextWindow | 是（仅当 stopReason != "stop" 时） |
| **threshold** | context tokens 超过配置的百分比阈值 | 否 |
| **manual** | 用户调用 `/compact` 命令 | 否 |

### 4.3 完整流程

```
触发（overflow / threshold / manual）
  │
  ▼
1. agent.abort() + agent.disconnect() → 暂停 Agent 事件流
  │
  ▼
2. prepareCompaction(sessionEntries, settings)
  → 从 SessionManager 读取当前分支的所有 entries
  → 根据配置决定保留哪些（最近 N 轮、最近 N 条消息）
  → 返回 CompactionPreparation { entriesToKeep, entriesToSummarize }
  │
  ▼
3. 发射 session_before_compact 扩展事件
  → 扩展可以 cancel（返回 { cancel: true }）
  → 扩展可以提供自定义摘要（返回 { compaction: {...} }）
  → 如果扩展没有提供 → 走默认逻辑
  │
  ▼
4. 默认逻辑：compact()
  → 调用 streamFn 让 LLM 总结 entriesToSummarize
  → LLM 返回摘要文本
  │
  ▼
5. sessionManager.appendCompaction(summary, firstKeptEntryId, tokensBefore)
  → 在 session 文件中写入一条 compaction entry
  │
  ▼
6. 重建 agent.state.messages
  → sessionManager.buildSessionContext()
  → 只包含：compaction 摘要 + 最近保留的原始消息
  → 赋给 agent.state.messages
  │
  ▼
7. 发射 compaction_end + session_compact 事件
```

**副作用**：
- `agent.state.messages` 被**整体替换**——旧的 message 对象不再存在于 agent 状态中
- session 文件中追加了一条 compaction entry（标记哪些旧 entry 被压缩了）
- 如果原因是 overflow 且 stopReason != "stop"：压缩后自动调用 `agent.continue()` 重试
- 如果是 threshold 但没有后续 queued messages：agent 停在空闲状态，等待下一条用户输入

---

## 5. Auto-Retry 机制（接口级）

```
agent_end 事件 → 最后一条 assistant message 是 error？
  │
  ├── 不是 → 不重试
  │
  └── 是 → _isRetryableError(message)
         │
         ├── stopReason == "error" 且 errorMessage 含 "overloaded" / "rate_limit" / "529" → 可重试
         ├── context overflow → 不重试（走 compaction 路径）
         └── 其他 error → 不重试
              │
              ▼
          _prepareRetry()
            → retryAttempt < maxRetries（默认 3 次）?
              → 从 agent.state.messages 移除最后一条 error message
              → 发射 auto_retry_start 事件
              → 指数退避延迟（retryAttempt^2 * 1000ms）
              → 返回 true → 外层调用 agent.continue()
```

**副作用**：重试前会从 `agent.state.messages` **移除** error message（它仍然写入了 session 文件用于历史记录，但不进入 LLM 上下文）。

---

## 6. Model / Thinking Level 管理（接口级）

```typescript
// 设置模型（必须先有 API key 或 OAuth token）
await session.setModel(model);   // → 持久化到 session + settings

// 循环切换模型（Ctrl+P）
await session.cycleModel("forward" | "backward");
// 如果有 scopedModels（--models 标志），只在 scoped 范围内循环
// 否则，在所有可用模型中循环

// 设置思考级别
session.setThinkingLevel("medium");  // 自动 clamp 到模型支持的范围内

// 循环切换思考级别（Ctrl+T）
session.cycleThinkingLevel();  // off → minimal → low → medium → high
```

这些操作会触发 `model_select` 和 `thinking_level_select` 扩展事件。

---

## 7. ModelRuntime 深读

> **📍 当前位置**：ModelRuntime 不在 Agent 循环中，也不在 prompt() 链路中。它在 [全局地图](#全局地图agentsession-在完整链路中的位置) 的最顶部——`main()` 启动时创建的 `AgentSessionServices` 中。它是个**基础设施**，被注入到 AgentSession，然后传递给 Agent 的 `streamFn`。本章从零开始解释它是什么。

1. **认证**：管理各 provider 的 API key / OAuth token
2. **模型目录**：维护可用模型的列表（provider → model id → model 元数据）
3. **LLM 调用**：提供 `streamSimple()` 供 Agent 使用

它不是"Agent 循环内部"的东西——它在 AgentSession 创建之前就存在，被注入到 AgentSession 中。

### 7.2 ModelRuntime 由谁创建

```typescript
// 在 createAgentSessionServices() 中创建
const modelRuntime = await ModelRuntime.create({
  authPath: join(agentDir, "auth.json"),     // API key / OAuth token 存储位置
  modelsPath: join(agentDir, "models.json"),  // 模型目录缓存位置
});
```

**创建时机**：每次新 session 或 session 切换时，`AgentSessionServices` 被重建，ModelRuntime 也会重新创建（除非调用方显式传入已有的）。

### 7.3 核心方法

```typescript
// ── 认证 ──
async getAuth(model: Model): Promise<{
  auth: { apiKey?: string, headers?: Record<string, string | null> },
  env?: Record<string, string>
}>
// 获取调用此模型所需的完整认证信息。
// 优先级：OAuth token > API key 环境变量 > auth.json 中存储的 API key > 模型定义中的 apiKey
// 如果都找不到 → throw Error("No API key found for ...")

async checkAuth(provider: string): Promise<AuthResult | undefined>
// 检查某个 provider 是否有可用认证（不抛异常，返回 undefined 表示没有）

hasConfiguredAuth(provider: string): boolean
// 同步检查（比 checkAuth 快，但不检测 OAuth token 是否过期）

isUsingOAuth(provider: string): boolean
// 此 provider 是否使用 OAuth 认证

// ── 模型目录 ──
async getAvailable(): Promise<Model[]>
// 返回所有有可用认证的模型列表

getModel(provider: string, id: string): Model | undefined
// 同步查找一个模型

async refresh(options?: { allowNetwork?: boolean })
// 刷新模型目录（从 provider 拉取最新模型列表）

// ── Provider 注册 ──
registerProvider(name: string, config: ProviderConfig): void
// 注册或覆盖一个 LLM provider（扩展使用）

registerNativeProvider(provider: Provider): void
// 注册一个自定义 provider（实现了完整的 Provider 接口）

unregisterProvider(name: string): void
// 移除一个已注册的 provider，恢复内置模型
```

### 7.4 认证流程

```
需要调用模型 "claude-sonnet-5" (provider: "anthropic")
  │
  ▼
modelRuntime.getAuth(model)
  │
  ├── OAuth credentials 存在且未过期
  │   → 调用 oauth.getApiKey(credentials) → 返回 apiKey
  │
  ├── 环境变量 ANTHROPIC_API_KEY 存在
  │   → 返回环境变量值
  │
  ├── auth.json 中有 "anthropic" 的配置
  │   → 返回存储的 API key
  │
  └── 模型定义中的 apiKey 字段（如 "$ANTHROPIC_API_KEY"）
      → 解析环境变量引用
```

### 7.5 streamSimple 与 Agent 的关系

`ModelRuntime.streamSimple()` 被用作 Agent 的 `streamFn`：

```typescript
// 在 createAgentSession() 中：
const agent = new Agent({
  streamFn: (model, context, options) => {
    return modelRuntime.streamSimple(model, context, options);
  },
  // ...
});
```

`streamSimple()` 内部：
1. 根据 `model.provider` 和 `model.api` 选择正确的 provider 实现
2. 构造 HTTP 请求（URL、headers、body）
3. 发送请求 → 返回 `AssistantMessageEventStream`（AsyncIterable）
4. 处理重试、超时、错误转换

### 7.6 对迁移的意义

如果需要为 Vision 验证用**不同的 API**（比如 MiMo），有两种方式：

- **方式 A**：通过 `modelRuntime.registerProvider("mimo", {...})` 注册新的 provider，然后在 Vision 验证时用这个 provider 的 model 调 `modelRuntime.streamSimple()`
- **方式 B**：不经过 ModelRuntime，直接在扩展中调 fetch / axios 发 HTTP 请求（完全绕过 PiAgent 的模型系统）

---

## 8. AgentSessionRuntime 深读

> **📍 当前位置**：[全局地图](#全局地图agentsession-在完整链路中的位置) 中最底部的"Session 切换"路径。当用户执行 `/new`、`/fork`、`/resume` 时，AgentSessionRuntime 负责销毁旧 session、创建新 session。它不参与 prompt() 链路，而是管理 session 之间的切换。

它和 AgentSession 的分工可以这样理解：

| | AgentSession | AgentSessionRuntime |
|---|---|---|
| 职责 | 一个 session **内部**发生了什么 | session **之间**的切换 |
| 生命周期 | 从创建到 dispose() | 可能经历多次 new/fork/switch |
| 持有 | Agent, SessionManager, ExtensionRunner | 当前 AgentSession + AgentSessionServices |
| 暴露 | prompt(), steer(), followUp(), compact() | newSession(), fork(), switchSession(), importFromJsonl() |

### 8.2 为什么需要 AgentSessionRuntime

PiAgent 支持用户在一个进程中**切换 session**。比如：
- 用户在 TUI 里按 `/new` → 创建新 session
- 用户按 `/resume other-session.jsonl` → 切换到另一个 session
- 用户按 `/fork entry-123` → 从历史某条消息分叉出新的 session 分支

每次切换都是一次"旧 session 销毁 + 新 session 创建"。AgentSessionRuntime 封装了这个过程。

### 8.3 核心方法

```typescript
// ── 创建新 session ──
async newSession(options?: {
  parentSession?: string;  // 父 session 文件路径
  setup?: (sessionManager) => Promise<void>;  // 新 session 初始化回调
  withSession?: (ctx) => Promise<void>;  // session 替换后的回调
}): Promise<{ cancelled: boolean }>

// 内部流程：
// 1. 发射 session_before_switch 扩展事件（可 cancel）
// 2. 创建新 SessionManager
// 3. 如果 parentSession 存在，设置父 session 关联
// 4. teardownCurrent("new") → session_shutdown → dispose()
// 5. 调用 createRuntime 工厂函数创建新 AgentSession + AgentSessionServices
// 6. 如果 setup 回调存在，调用它初始化新 session
// 7. finishSessionReplacement() → 重新绑定 UI

// ── 分叉 session ──
async fork(entryId: string, options?: {
  position?: "before" | "at";  // 从哪条消息分叉
}): Promise<{ cancelled: boolean }>

// 内部流程：
// 1. 发射 session_before_fork 扩展事件
// 2. 在当前 session 文件中找到目标 entry
// 3. 如果 position=="at": 分叉目标就是当前 entry
//    如果 position=="before": 分叉目标是 entry 的父节点
// 4. 创建新的 session 文件（基于旧 session 的引用）
// 5. teardownCurrent("fork") → 创建新 AgentSession

// ── 切换 session ──
async switchSession(sessionPath: string): Promise<{ cancelled: boolean }>

// ── 从 JSONL 导入 ──
async importFromJsonl(inputPath: string, cwdOverride?: string): Promise<{ cancelled: boolean }>
```

### 8.4 销毁旧 session 的流程 (teardownCurrent)

```
teardownCurrent(reason: "new" | "resume" | "fork")
  │
  ▼
1. agent.abort() → 等待当前 Agent run 结束
  │
  ▼
2. 发射 session_shutdown 扩展事件
  → 所有扩展的 "session_shutdown" handler 被调用
  → 扩展可以在这里清理资源（关闭文件、断开连接等）
  │
  ▼
3. beforeSessionInvalidate() 回调
  → 同步回调（如销毁 TUI 组件）
  │
  ▼
4. session.dispose()
  → 取消所有 subscribe
  → ExtensionRunner.invalidate()（标记扩展上下文为无效）
  → 清理 session 资源
```

### 8.5 对迁移的意义

这直接决定"UE 连接应该放在哪里"：

- **如果 UE 连接绑定到 AgentSession**：每次 /new 或 /fork 会断开并重连 UE
- **如果 UE 连接绑定到 AgentSessionRuntime**：连接在 session 切换时**保持**，持续可用
- **如果 UE 连接绑定到扩展内部**：连接在 `session_shutdown` 事件中需要清理，在 `session_start` 事件中需要重建

类比 UEMCPHarness：当前的 `McpClientSession` 是跟着 Harness 进程走的（全局单例），相当于绑定到 AgentSessionRuntime 级别。

---

下一篇 [03-extension-system](03-extension-system.md) 将详细解析扩展系统——30+ 事件的触发时机、工具注册流程、和 UEMCPHarness Interceptor 链的精确对应。
