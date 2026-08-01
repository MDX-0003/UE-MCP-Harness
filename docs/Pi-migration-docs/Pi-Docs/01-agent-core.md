# 01 — Agent 核心：Agent 类 + Agent 循环

**对应的源文件**：[packages/agent/src/agent.ts](d:/Programs/2024-2/pi/packages/agent/src/agent.ts)、[agent-loop.ts](d:/Programs/2024-2/pi/packages/agent/src/agent-loop.ts)、[types.ts](d:/Programs/2024-2/pi/packages/agent/src/types.ts)

**前置阅读**：[00-overview](00-overview.md)
**阅读目标**：理解 Agent 循环的每一轮发生了什么、工具怎么执行、在哪些位置可以插入自定义逻辑

---

## 全局地图：Agent 在完整链路中的位置

```
                    ┌─────────────────────────────────────────┐
                    │  07-main-and-modes: main()              │
                    │  CLI 启动 → 创建 Agent → 创建            │
                    │  AgentSession → 等待用户输入              │
                    └────────────────┬────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────┐
│  02-agent-session: AgentSession                            │
│                                                             │
│  session.prompt(text)                                       │
│    ├── 扩展命令检查、input 事件、Skill 展开                  │
│    ├── before_agent_start 事件                              │
│    │                                                        │
│    └── ╔═══════════════════════════════════════════╗        │
│        ║  01-agent-core: Agent ←── 本文档范围      ║        │
│        ║                                           ║        │
│        ║  agent.prompt(messages)                   ║        │
│        ║    → runAgentLoop()                       ║        │
│        ║      → 外层 followUp loop                 ║        │
│        ║        → 内层 turn loop                   ║        │
│        ║          → streamAssistantResponse()      ║        │
│        ║            → transformContext             ║        │
│        ║            → convertToLlm (过滤消息)      ║        │
│        ║            → streamFn (LLM 调用)          ║        │
│        ║          → executeToolCalls()             ║        │
│        ║            → beforeToolCall               ║        │
│        ║            → tool.execute()               ║        │
│        ║            → afterToolCall                ║        │
│        ║          → prepareNextTurn                ║        │
│        ║          → shouldStopAfterTurn            ║        │
│        ║          → 检查 steering/followUp 队列    ║        │
│        ║    → agent_end 事件                       ║        │
│        ╚═══════════════════════════════════════════╝        │
│                                                             │
│  ← 循环回到 AgentSession                                    │
│    ├── auto-retry 检查                                      │
│    ├── auto-compaction 检查                                 │
│    └── agent_settled 事件                                   │
└─────────────────────────────────────────────────────────────┘
```

**本文档覆盖**：上图中粉色框的全部内容。

---

## 调用上下文：Agent 怎么被创建、怎么被调用、调用后发生什么

### 谁创建了 Agent

在 `07-main-and-modes` 的 `createAgentSession()` 中：

```typescript
const agent = new Agent({
  // 输入：Agent 需要的全部能力，以回调函数形式注入
  streamFn: (model, ctx, opts) => modelRuntime.streamSimple(model, ctx, opts),
  convertToLlm: (msgs) => /* 过滤掉 custom/UI 消息 */,
  transformContext: async (msgs, sig) => /* 可选：裁剪/注入上下文 */,
  beforeToolCall: async (ctx, sig) => /* 可选：由 AgentSession 后续覆盖 */,
  afterToolCall: async (ctx, sig)  => /* 可选：由 AgentSession 后续覆盖 */,
  prepareNextTurn: async (ctx)     => /* 可选：每轮刷新 systemPrompt+tools */,
  getApiKey: (provider) => /* 可选：动态获取 API key */,
  // 输入：初始状态
  initialState: {
    systemPrompt: "...",   // 由 ResourceLoader + Skills 组装
    model: someModel,       // 由用户设置或默认模型
    tools: initialTools,    // ["read", "bash", "edit", "write"]
    messages: restoredMsgs, // 从 session 文件恢复（新 session 为空）
  },
  steeringMode: "one-at-a-time",
  followUpMode: "one-at-a-time",
  toolExecution: "parallel",
});
```

**输入**：以上所有回调函数 + 初始状态。Agent 不创建任何东西——所有能力都从外部注入。

**Agent 本身不返回什么**——它是个状态机。调用者通过 `agent.state` 读取结果。

### 谁调用了 Agent，传入什么

只有一处调用：`AgentSession._runAgentPrompt()`。

```typescript
// AgentSession._runAgentPrompt() 内部：
this._isAgentRunActive = true;          // ← 副作用：标记"正在运行"，
                                        //   后续新的 prompt() 调用会被排队而不是直接执行
try {
  await this.agent.prompt(messages);    // ← 输入：AgentMessage[]
                                        //   其中至少一条 role:"user" 的消息
                                        //   可能还有 custom messages（扩展注入的）
  while (await this._handlePostAgentRun()) {
    await this.agent.continue();        // ← 无额外输入，从当前状态继续
  }
} finally {
  this._systemPromptOverride = undefined;  // ← 副作用：清除临时 system prompt
  this._flushPendingBashMessages();        // ← 副作用：刷新排队的 bash 消息
  await this._emitAgentSettled();          // ← 副作用：通知所有监听器
  this._isAgentRunActive = false;          // ← 副作用：标记空闲，
}                                          //   后续新的 prompt() 可以直接执行
```

### 调用后 Agent 内部发生了什么（连锁影响）

```
agent.prompt(messages)
  │
  ├── ▶ 修改 agent.state（内部 mutable state）：
  │     isStreaming = true            ← 外部通过 agent.state.isStreaming 可见
  │     streamingMessage = undefined  ← 清空上一轮的流式消息
  │     errorMessage = undefined      ← 清空上一轮的错误
  │     pendingToolCalls = {}         ← 清空上一轮的工具调用
  │
  ├── ▶ 发射事件（agent.subscribe 的监听器收到）：
  │     agent_start → turn_start → message_start(user)... →
  │     【进入 runAgentLoop】
  │     ... → message_start(assistant) → message_update* → message_end(assistant)
  │     ... → tool_execution_start → tool_execution_end* → ...
  │     ... → turn_end → 【下一轮 turn 或结束】
  │     ... → agent_end
  │
  ├── ▶ 监听器的连锁影响（AgentSession._handleAgentEvent）：
  │     message_end → sessionManager.appendMessage()  ← 写入磁盘
  │     message_end → ExtensionRunner.emit()          ← 扩展收到事件
  │     agent_end  → 触发 auto-retry / auto-compaction
  │
  └── ▶ 返回给 AgentSession 后：
        _handlePostAgentRun() 循环：
          → auto-retry? → agent.continue() → 重复以上过程
          → auto-compaction? → compact() → 替换 agent.state.messages
          → hasQueuedMessages? → agent.continue() → 重复以上过程
          → 否则 → agent_settled → isStreaming = false
```

**关键点**：Agent 只负责循环的执行。循环结束后，AgentSession 检查是否需要更多操作（重试/压缩/排队消息），如果需要就再次调用 `agent.continue()` 启动下一个循环。这个 while 循环**可能反复多次**。

---

## 1. Agent 是什么

`Agent` 是一个**状态机包装器**。它：

- **持有**对话的状态（当前有哪些消息、哪些工具、用哪个模型）
- **提供**用户-facing 操作（发 prompt、继续、中止、重置）
- **发射**结构化事件（消息开始/结束、工具开始/结束、turn 开始/结束）
- **管理**消息队列（用户打断 vs 补充）
- **自己不跑循环**——它调用 `runAgentLoop()`（一个纯函数）

**关键认知**：Agent 是"状态"层，`runAgentLoop()` 是"行为"层。Agent 通过 `AgentContext` 快照把状态传给循环，循环通过 event 回调把结果写回状态。

---

## 2. Agent 类的结构

### 2.1 构造参数：AgentOptions

```typescript
interface AgentOptions {
  initialState?: {
    systemPrompt?: string;   // 每次 LLM 请求都会附带的系统提示词
    model?: Model<any>;      // 当前使用的模型（含 id/provder/baseUrl/contextWindow 等全部元数据）
    thinkingLevel?: string;  // "off" | "low" | "medium" | "high" | ...
    tools?: AgentTool[];     // 初始工具列表
    messages?: AgentMessage[]; // 初始消息历史（恢复 session 时用）
  };
  // ── 核心回调 ──
  streamFn: (model, context, options?) => AssistantMessageEventStream;
  //         ↑ 发起 LLM 请求的唯一入口。Agent 循环不关心 LLM 是 Claude 还是本地模型，
  //           它只管调用这个函数并消费返回值。

  convertToLlm: (messages: AgentMessage[]) => Message[];
  //             ↑ 每次 LLM 调用前，把 AgentMessage[] 转换为 LLM 能理解的 Message[]。
  //               AgentMessage 可能包含 UI-only 消息（custom message），这个函数负责过滤。

  // ── 可选钩子 ──
  transformContext?: (messages, signal?) => Promise<AgentMessage[]>;
  // 在 convertToLlm 之前执行。用于裁剪旧消息、注入外部上下文。

  beforeToolCall?: (context, signal?) => Promise<{block?: boolean, reason?: string} | undefined>;
  // 工具执行前调用。可以阻止执行（block: true）。

  afterToolCall?: (context, signal?) => Promise<{
  // 工具执行后调用。可以修改结果的内容、标记 isError、设置 usage。
    content?: (TextContent | ImageContent)[];
    details?: unknown;
    isError?: boolean;
    usage?: Usage;
    terminate?: boolean;  // true = 此工具要求本轮结束后停止
  } | undefined>;

  prepareNextTurn?: (context, signal?) => Promise<{
  // 每轮 turn 结束后调用。可以替换下一轮的 model / thinkingLevel / context。
    context?: AgentContext;
    model?: Model<any>;
    thinkingLevel?: ThinkingLevel;
  } | undefined>;

  // ── 消息队列 ──
  steeringMode?: "all" | "one-at-a-time";  // 默认 "one-at-a-time"
  followUpMode?: "all" | "one-at-a-time";  // 默认 "one-at-a-time"

  // ── 其他选项 ──
  getApiKey?: (provider: string) => Promise<string | undefined>;
  // 每次 LLM 调用前，动态获取 API key（适合短期 OAuth token）
  onPayload?: (payload, model) => void;    // 观察发往 LLM 的原始 payload
  onResponse?: (response, model) => void;  // 观察 LLM 返回的原始 response
  sessionId?: string;      // 转发给 LLM provider（用于 cache-aware 后端）
  transport?: "auto" | ...; // 网络传输方式
  toolExecution?: "sequential" | "parallel"; // 默认 "parallel"
}
```

**这些参数分别对应在工具迁移中的用途**：

| 参数 | 迁移用途 |
|------|---------|
| `streamFn` | Vision 验证需要独立调 LLM，可以复用同一套 streamFn 或提供不同配置 |
| `convertToLlm` | 如果注册了 UE 工具，转换时需确保 tool 定义正确传入 Context |
| `beforeToolCall` | Safety Guardrails — 在工具执行前检查参数、决定是否禁止 |
| `afterToolCall` | Vision 验证 / Readback 验证 / State Cache 更新 — 全部在这里 |
| `transformContext` | 注入 UE State Cache 快照到 LLM 上下文 |
| `prepareNextTurn` | 每轮刷新 UE 工具列表、更新 System Prompt |

### 2.2 AgentState — Agent 的状态

```typescript
interface AgentState {
  systemPrompt: string;     // 当前系统提示词（可读写）
  model: Model<any>;        // 当前模型（可读写）
  thinkingLevel: string;    // 当前思考级别（可读写）
  tools: AgentTool[];       // 当前工具列表（可读写 — setter 会复制数组）
  messages: AgentMessage[]; // 对话历史（可读写 — setter 会复制数组）

  // 以下只读，由 Agent 内部管理
  readonly isStreaming: boolean;         // 是否正在处理中
  readonly streamingMessage?: AgentMessage;  // 当前正在流式输出的消息
  readonly pendingToolCalls: ReadonlySet<string>; // 正在执行中的工具调用 ID
  readonly errorMessage?: string;        // 最近一次失败的 assistant message 的错误信息
}
```

**关键设计**：`tools` 和 `messages` 的 setter 会**复制数组**再存储。这意味着你 `agent.state.messages.push(x)` 不会生效——必须 `agent.state.messages = [...agent.state.messages, x]` 或者用 `agent.prompt()` / `agent.continue()` 让循环自己追加。

### 2.3 三个队列

Agent 内部维护三个队列来控制消息注入时机：

```
用户意图           队列                 注入时机
─────────────────────────────────────────────────────
"打断当前的活"    steering queue    当前 assistant turn 完成后、下轮 LLM 调用前
"干完再说"       followUp queue    Agent 即将空闲时（无 tool call、无 steering）
"取消/替换结果"  pendingToolCalls  Set<toolCallId>（不是队列，是正在执行中的工具）
```

`Agent.steer(message)` — 放入 steering 队列。当 Agent 循环完成当前 turn 的工具执行后，下一轮 LLM 调用前，消息被注入。**类比 UEMCPHarness**：用户在中途说"等等，换个方向"。

`Agent.followUp(message)` — 放入 followUp 队列。只有当 Agent 循环内部判断"没有更多 tool call、没有 steering 消息"时才注入。**类比 UEMCPHarness**：验证完成后补充"刚才的效果不错，你看到了吗？"。

**QueueMode**：
- `"all"` — 一次性把所有排队消息全部注入
- `"one-at-a-time"` — 每轮只注入最早排队的那一条（默认模式）

### 2.4 subscribe() — 事件监听

```typescript
// 订阅
const unsubscribe = agent.subscribe(async (event: AgentEvent, signal: AbortSignal) => {
  switch (event.type) {
    case "agent_start":    // 循环启动
    case "agent_end":      // 循环结束（带 messages 列表）
    case "turn_start":     // 一轮 turn 开始
    case "turn_end":       // 一轮 turn 结束（带 message + toolResults）
    case "message_start":  // 一条消息开始（user / assistant / toolResult）
    case "message_update": // assistant 消息流式更新（带增量 token）
    case "message_end":    // 一条消息结束（此时消息已写入 agent.state.messages）
    case "tool_execution_start": // 工具开始执行（带 toolCallId + toolName + args）
    case "tool_execution_update": // 工具执行中部分结果
    case "tool_execution_end": // 工具执行结束（带 result + isError）
  }
});
// 取消订阅
unsubscribe();
```

**关键设计**：
- `subscribe` 的返回值是一个函数，调用它就取消订阅
- 监听器在订阅顺序中被 **await**，所以顺序很关键
- 监听器收到 `signal`（AbortSignal），可以知道当前 run 是否被中止
- `agent_end` 是循环的最后一条事件，但 `agent` 要到所有 `agent_end` 监听器的 await 完成后才标记为"空闲"

### 2.5 生命周期方法

```typescript
// 开始一次对话
await agent.prompt("把 PointLight 的颜色改成红色");
// 也支持带图片
await agent.prompt("这个场景看起来怎么样？", [{ type: "image", ... }]);
// 也支持直接传消息数组（恢复 session 时使用）
await agent.prompt([{ role: "user", content: [{ type: "text", text: "..." }], timestamp: ... }]);

// 继续已有对话（从最后一条 user/toolResult 消息继续）
await agent.continue();

// 中止当前 run
agent.abort();

// 等待 agent 空闲（所有 agent_end 监听器完成）
await agent.waitForIdle();

// 清空所有状态
agent.reset();
```

**`prompt()` vs `continue()`**：
- `prompt(x)` — 全新开始，x 可以是字符串、single message、或消息数组。如果 agent 正在处理中，会直接抛异常。用 `steer()` 或 `followUp()` 来在运行中加入消息。
- `continue()` — 从当前消息历史的最后一条消息继续。如果最后一条是 assistant message，先看 steering/followUp 队列有没有东西。**不能从 assistant message 直接继续**（因为 assistant 后面必须有 user 或 toolResult）。

---

## 3. Agent 循环的完整流程

> **📍 当前位置**：仍在 [全局地图](#全局地图agent-在完整链路中的位置) 的粉色框范围内。`agent.prompt(messages)` 被调用后，`runAgentLoop()` 启动。本章讲解两个嵌套循环的每一步。

这是整个 PiAgent 最核心的执行逻辑，在 `agent-loop.ts` 的 `runLoop()` 函数中。

### 3.1 runAgentLoop vs runAgentLoopContinue

```typescript
// runAgentLoop: 用新消息启动循环
await runAgentLoop(
  messages,           // 用户输入的消息（AgentMessage[]）
  contextSnapshot,    // 当前状态快照 { systemPrompt, messages, tools }
  config,             // AgentLoopConfig（包含所有钩子）
  eventCallback,      // 事件回调（Agent.processEvents）
  signal,             // AbortSignal
  streamFunction,     // LLM 调用函数
);

// runAgentLoopContinue: 用已有上下文继续循环
await runAgentLoopContinue(
  contextSnapshot,    // 快照中已包含完整消息历史
  config,
  eventCallback,
  signal,
  streamFunction,
);
```

**区别**：
- `runAgentLoop` 会先发射 `turn_start` 事件（因为它是循环开始），然后把新消息 append 到 context
- `runAgentLoopContinue` 直接进入循环，从当前 context 继续

### 3.2 两个嵌套循环

```
┌─ 外层循环 (follow-up loop) ────────────────────────────────┐
│                                                             │
│   检查 followUp 队列 → 有消息？→ 注入 → 启动新内层循环       │
│   无消息？→ agent_end → 结束                                 │
│                                                             │
│   ┌─ 内层循环 (turn loop) ──────────────────────────────┐   │
│   │                                                      │   │
│   │  1. turn_start (如果是第一轮且 runAgentLoop，已发射)  │   │
│   │                                                      │   │
│   │  2. 检查 steering/followUp 队列 → 注入排队消息        │   │
│   │                                                      │   │
│   │  3. streamAssistantResponse()                        │   │
│   │     ├─ transformContext (AgentMessage 级变换)         │   │
│   │     ├─ convertToLlm (过滤 → LLM Message[])           │   │
│   │     ├─ 拼装 Context { systemPrompt, messages, tools }│   │
│   │     ├─ 解析 API key                                  │   │
│   │     └─ streamFn(model, context, options)             │   │
│   │          → AssistantMessageEventStream               │   │
│   │          → 逐个消费事件，发射 message_start/update/end│   │
│   │          → 返回最终的 AssistantMessage               │   │
│   │                                                      │   │
│   │  4. 如果 stopReason == "error" | "aborted"           │   │
│   │     → 发射 turn_end + agent_end，退出                │   │
│   │                                                      │   │
│   │  5. 提取 toolCalls                                   │   │
│   │     如果 stopReason == "length"（输出被截断）          │   │
│   │     → 所有 toolCall 被标记为错误                      │   │
│   │                                                      │   │
│   │  6. 执行 toolCalls（见第 4 节）                       │   │
│   │                                                      │   │
│   │  7. 工具结果 append 到 context.messages               │   │
│   │                                                      │   │
│   │  8. 发射 turn_end                                    │   │
│   │                                                      │   │
│   │  9. prepareNextTurn → 可能替换 model/context/thinking │   │
│   │                                                      │   │
│   │ 10. shouldStopAfterTurn → true? → agent_end          │   │
│   │                                                      │   │
│   │ 11. 检查 steering 队列 → 有消息？→ 注入 → 回到步骤 2  │   │
│   │     无消息且无更多 tool call → 退出内层循环           │   │
│   │                                                      │   │
│   └──────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 每一轮 Turn 的关键副作用

以下副作用按照 turn 的执行顺序排列：

| 步骤 | 对 agent.state.messages 的影响 | 发射的事件 | 会不会"卡住" |
|------|-------------------------------|-----------|-------------|
| 2. 注入排队消息 | append 到 context（只在循环内，未回写到 Agent 状态） | message_start + message_end | 否 |
| 3. LLM 调用 | 在循环内累积 streamingMessage | message_start → message_update* → message_end | **是**（等待 LLM 网络响应，最长几十秒） |
| 5. 提取 toolCalls | 无（只是解析 assistant message） | 无 | 否 |
| 6. 执行工具 | 在循环内积累 toolResult messages | tool_execution_start → (update*) → tool_execution_end | **是**（工具执行期间） |
| 7. 结果 append | 在循环内 append 到 context | toolResult 的 message_start + message_end | 否 |
| 8. turn_end | 无（context 变化仅限循环内部） | turn_end | 否 |

**重要**：循环内部的消息追加只在循环的局部变量 `context.messages` 中进行。只有当 `message_end` 事件发射、Agent.processEvents() 处理时，才会写入 `agent.state.messages`。这意味着如果你在 `beforeToolCall` 中读 `agent.state.messages`，你看到的**不包含**这一轮刚追加的消息。

---

## 4. 工具执行的完整流程

> **📍 当前位置**：在内层循环的步骤 6（执行 toolCalls）。当 LLM 返回 `stopReason: "toolUse"` 后，`executeToolCalls()` 被调用。本章讲解 prepare → execute → finalize 三个阶段。

### 4.1 三阶段：prepare → execute → finalize

```
收到 assistant message (含 toolCalls: [{name: "set_properties", args: {...}}])
  │
  ▼
┌─ 阶段 1: prepare ──────────────────────────────────────────┐
│                                                             │
│  a. 在 agent.state.tools 中查找匹配的 AgentTool             │
│                                                             │
│  b. 如果工具定义了 prepareArguments()，用它预处理原始 args   │
│     → 这是"兼容性垫片"。LLM 可能传了旧版参数格式，           │
│       prepareArguments 可以把它们转为新版格式               │
│                                                             │
│  c. 用工具的 TypeBox schema 校验参数                         │
│     → 校验失败 → 返回错误 toolResult，跳过执行              │
│                                                             │
│  d. 调用 beforeToolCall 钩子                                │
│     → 返回 { block: true, reason: "..." } → 阻止执行        │
│     → 返回 undefined → 继续                                 │
│                                                             │
│  结果："prepared" (可以执行) 或 "immediate" (已被 block)    │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ 阶段 2: execute ──────────────────────────────────────────┐
│                                                             │
│  e. 创建 onUpdate 回调（工具通过它发送部分结果）             │
│     onUpdate → 发射 tool_execution_update 事件              │
│                                                             │
│  f. 调用 tool.execute(toolCallId, params, signal, onUpdate) │
│     → 这就是工具的实际实现代码                               │
│     → 如果抛出异常，异常被捕获并转为错误的 toolResult        │
│     → 如果正常返回，返回 AgentToolResult { content, details,│
│       usage, terminate, addedToolNames }                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ 阶段 3: finalize ─────────────────────────────────────────┐
│                                                             │
│  g. 调用 afterToolCall 钩子                                  │
│     → 可以覆盖 content / details / isError / usage /        │
│       terminate 中的任意字段                                 │
│     → 未指定的字段保持原值                                   │
│                                                             │
│  h. 发射 tool_execution_end 事件                            │
│                                                             │
│  i. 将最终的 toolResult 转为 AgentMessage (role: toolResult)│
│     发射 message_start + message_end                        │
│                                                             │
│  j. 检查 terminate：如果所有工具结果都标记了 terminate:true  │
│     → shouldTerminateToolBatch() = true → 循环提前退出      │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Sequential vs Parallel 执行

**Sequential** (`toolExecution: "sequential"`)：
```
toolCall_1: prepare → execute → finalize → emit events
toolCall_2: prepare → execute → finalize → emit events
toolCall_3: prepare → execute → finalize → emit events
```
`tool_execution_end` 事件和 toolResult 消息按工具顺序依次发射。

**Parallel** (`toolExecution: "parallel"`，默认)：
```
toolCall_1,2,3: prepare 依次执行（beforeToolCall 可能 block）
            ↓
toolCall_1 ──┐
toolCall_2 ──┼─ execute 并发执行（Promise.all）
toolCall_3 ──┘
            ↓
tool_execution_end 事件：按工具完成顺序发射（谁先完成谁先发）
toolResult 消息：按 assistant 中 toolCall 原始顺序发射（保持 LLM 理解的一致性）
```

### 4.3 terminate 提前终止

如果一个工具返回 `{ terminate: true }`，它不等于立即结束循环。终止只在**本轮所有工具的 finalize 阶段结束后**触发——只有当**每一个**工具的 `terminate` 都是 `true` 时，循环才会提前退出。

---

## 5. 流式响应处理

### 5.1 streamAssistantResponse() 内部

```typescript
// 简化逻辑
async function streamAssistantResponse(context, config, signal, streamFn) {
  // 1. 可选：transformContext (AgentMessage[] → AgentMessage[])
  if (config.transformContext) {
    context.messages = await config.transformContext(context.messages, signal);
  }

  // 2. 必须：convertToLlm (AgentMessage[] → Message[])
  const llmMessages = await config.convertToLlm(context.messages);

  // 3. 构建 LLM Context
  const llmContext = {
    systemPrompt: context.systemPrompt,
    messages: llmMessages,           // 已经过滤了 custom/UI-only 消息
    tools: context.tools,            // 当前活跃的工具列表
  };

  // 4. 获取 API key
  const apiKey = await config.getApiKey?.(model.provider);

  // 5. 发起 LLM 请求
  const stream = await streamFn(model, llmContext, {
    apiKey,
    signal,
    onPayload: config.onPayload,     // 观察发出去的内容
    onResponse: config.onResponse,   // 观察收到的响应头
    thinkingLevel: config.reasoning,
  });

  // 6. 消费流式事件
  for await (const event of stream) {
    if (event.type === "message_start") {
      emit({ type: "message_start", message: buildingMessage });
    } else if (event.type === "content_block_delta") {
      // 追加 token → 更新 message 内容 → 发射 message_update
      emit({ type: "message_update", message: buildingMessage, assistantMessageEvent: event });
    } else if (event.type === "message_stop" || event.type === "error") {
      // 最终消息
      emit({ type: "message_end", message: finalMessage });
      return finalMessage;
    }
  }
}
```

### 5.2 stopReason 的含义

`stopReason` 是 assistant message 的关键字段，决定了循环下一步做什么：

| stopReason | 含义 | 循环行为 |
|-----------|------|---------|
| `"stop"` | LLM 正常完成，没有 tool call | 如果没有排队消息，循环结束 |
| `"toolUse"` | LLM 要调用工具 | 提取 toolCalls → 执行 → 回收结果 → 继续循环 |
| `"length"` | 达到 max_tokens，输出被截断 | toolCalls 可能不完整，标记为错误 |
| `"error"` | LLM 返回错误（如 API 超时） | 发射 turn_end + agent_end，退出 |
| `"aborted"` | 用户调用了 agent.abort() | 同 error，退出 |

---

## 6. 从哪里插入自定义逻辑？

回顾 UEMCPHarness 的 interceptor 链，对应到 PiAgent：

| UEMCPHarness 做法 | PiAgent 插入点 | 具体调用 |
|---|---|---|
| 修改 LLM 上下文（注入 State Cache） | `transformContext` | 在 LLM 调用前，修改 AgentMessage[] |
| 工具执行前检查（Safety Guardrails） | `beforeToolCall` | 返回 `{ block: true }` 阻止执行 |
| 写入后 L2 读回验证 | `afterToolCall` | 工具执行后，追加验证结果 |
| 截图后 Vision 分析 | `afterToolCall` 或 `subscribe("tool_execution_end")` | 判断工具名是否为截图工具 |
| JSONL 日志记录 | `subscribe("message_end")` | 每条消息结束时记录 |
| System Prompt 注入 Skills | `prepareNextTurn` | 每轮刷新 systemPrompt + tools 列表 |

下一篇 [02-agent-session](02-agent-session.md) 将展示 AgentSession 如何把这些钩子**实际挂载**到 Agent 上，以及 prompt() 在到达 Agent 循环之前走了哪些扩展拦截步骤。
