# 03 — 扩展系统

**对应的源文件**：[packages/coding-agent/src/core/extensions/types.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/extensions/types.ts)（~1714 行）、[runner.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/extensions/runner.ts)、[loader.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/extensions/loader.ts)

**前置阅读**：[00-overview](00-overview.md)、[01-agent-core](01-agent-core.md)、[02-agent-session](02-agent-session.md)
**阅读目标**：理解扩展系统的事件模型、工具注册流程，以及每个事件在 UEMCPHarness 中的对应物

---

## 全局地图：扩展系统在完整链路中的位置

```
┌─────────────────────────────────────────────────────────────┐
│  07-main-and-modes: main()                                 │
│  CLI 启动 → 加载扩展 → 创建 AgentSession → 绑定扩展        │
└─────────────────────────────────────────────────────────────┘
                                │
    ┌───────────────────────────┼───────────────────────────┐
    │                           │                           │
    ▼                           ▼                           ▼
┌──────────┐           ┌──────────────────┐        ┌──────────────┐
│ 扩展文件 │ ──加载──► │ ExtensionRunner  │ ──绑定─► │ AgentSession │
│ .ts/.js  │           │ ←── 本文档范围   │        │ (prompt链路) │
└──────────┘           └───────┬──────────┘        └──────┬───────┘
                               │                          │
                               │ 30+ 事件                  │ 调用 Agent
                               │ 工具注册                  │
                               │ 命令注册                  │
                               │                          │
                               ▼                          ▼
                    ┌──────────────────┐        ┌──────────────┐
                    │ Agent 循环内部    │        │ TUI/RPC/Print│
                    │ beforeToolCall   │        │ 模式渲染     │
                    │ afterToolCall    │        └──────────────┘
                    │ message_end      │
                    └──────────────────┘
```

**本文档覆盖**：ExtensionRunner 的结构 + 30+ 事件类型及其 UEMCPHarness 对应物 + 工具注册流程。

---

## 调用上下文：扩展系统怎么被创建、怎么被注入、怎么被触发

### 谁创建了 ExtensionRunner

```typescript
// 在 loadExtensions() 中：
const extensionsResult = discoverAndLoadExtensions(cwd, agentDir, inlineExtensions);
// → 输入：工作目录、.pi 目录、内联扩展工厂函数
// → 返回：LoadExtensionsResult { extensions: Extension[], runtime: ExtensionRuntime }
// → 修改：运行时加载了 .pi/extensions/*.ts 并执行了 export default function(pi) { ... }

// runtime 包含了：
// - flagValues: Map<string, boolean|string>   ← CLI --flag 的值
// - pendingProviderRegistrations: Array       ← 扩展注册的 LLM provider（未生效，等待 bindCore）
// - registerProvider/registerNativeProvider   ← 注册 LLM provider 的方法
```

### 谁把 ExtensionRunner 注入 AgentSession

```typescript
// 在 AgentSession 构造函数中：
this._extensionRunner = new ExtensionRunner(extensionsResult);
// → 输入：LoadExtensionsResult（已加载的扩展列表 + 共享 runtime）

// 然后 AgentSession 覆盖 Agent 的钩子，把它们重定向到 ExtensionRunner：
this.agent.beforeToolCall = async ({ toolCall, args }) => {
  const runner = this._extensionRunner;
  if (!runner.hasHandlers("tool_call")) return undefined;
  return await runner.emitToolCall({ type: "tool_call", toolName, toolCallId, input: args });
};
// → 修改：agent.beforeToolCall 被覆盖（覆盖了 Agent 构造时的原值）
// → 效果：之后每次工具执行前，扩展的 "tool_call" handler 都会被调用

this.agent.afterToolCall = async ({ toolCall, args, result, isError }) => {
  const runner = this._extensionRunner;
  if (!runner.hasHandlers("tool_result")) return undefined;
  const hookResult = await runner.emitToolResult({ type: "tool_result", ... });
  return hookResult ? { content, details, isError, usage } : undefined;
};
// → 修改：agent.afterToolCall 被覆盖
// → 效果：之后每次工具执行后，扩展的 "tool_result" handler 都会被调用
```

### 扩展事件什么时候被触发

扩展事件有两个触发路径：

**路径 A — Agent 循环内部的钩子**（AgentSession._installAgentToolHooks 覆盖的）：
```
Agent 循环 → tool 即将执行
  → agent.beforeToolCall (此时是 AgentSession 的版本)
    → ExtensionRunner.emitToolCall()  ← 触发 "tool_call" 事件
      → 所有注册了 pi.on("tool_call", ...) 的扩展 handler 依次执行
```

**路径 B — AgentSession._emitExtensionEvent()**（AgentSession 的 subscribe handler 内部）：
```
Agent 循环 → 发射 AgentEvent
  → agent.subscribe → AgentSession._handleAgentEvent
    → _emitExtensionEvent(event)
      → 根据 AgentEvent 类型，映射为 ExtensionEvent 并发射
```

---

## 1. 扩展的三种加载方式

| 方式 | 位置 | 何时加载 | 示例 |
|------|------|---------|------|
| **文件扩展** | `.pi/extensions/*.ts` 或 `{agentDir}/extensions/*.ts` | PiAgent 启动时自动扫描 | `my-extension.ts` → `export default function(pi) { ... }` |
| **内联扩展** | 代码中直接传入 `ExtensionFactory` 函数 | SDK 调用 `createAgentSession()` 时通过 `extensionFactories` 参数 | `createAgentSession({ extensionFactories: [myFactory] })` |
| **内置扩展** | packages/coding-agent/src/extensions/ | 编译时打包在 CLI 中 | llama.cpp 扩展（隐藏，总是加载） |

---

## 2. ExtensionRunner 的结构

### 2.1 事件总线

```typescript
class ExtensionRunner {
  // 核心数据结构
  private handlers: Map<string, HandlerFn[]>;  // "tool_call" → [fn1, fn2, ...]
                                               // "tool_result" → [fn3]
                                               // "agent_end" → [fn4, fn5]
                                               // ...

  // 注册 handler（扩展调用 pi.on("tool_call", handler) 时触发）
  on(eventType: string, handler: HandlerFn): void {
    if (!this.handlers.has(eventType)) {
      this.handlers.set(eventType, []);
    }
    this.handlers.get(eventType)!.push(handler);
  }

  // 发射事件（调用方触发）
  async emit(event: ExtensionEvent): Promise<Result[]> {
    const handlers = this.handlers.get(event.type) ?? [];
    const results: Result[] = [];
    for (const handler of handlers) {
      const result = await handler(event, extensionContext);
      results.push(result);
      // 如果返回了 { cancel: true } → 后续 handler 不再执行
      // 如果返回了 { action: "handled" } → 后续 handler 不再执行
    }
    return results;
  }

  // 类型安全的发射方法（每种事件有专用方法）
  async emitInput(text, images, source, streamingBehavior): Promise<InputEventResult>
  async emitBeforeAgentStart(text, images, systemPrompt, options): Promise<BeforeAgentStartEventResult>
  async emitToolCall(event: ToolCallEvent): Promise<ToolCallEventResult | undefined>
  async emitToolResult(event: ToolResultEvent): Promise<ToolResultEventResult | undefined>
  // ...
}
```

**关键设计**：
- 同一种事件可以有**多个 handler**（按注册顺序执行）
- Handler 可以返回结果来**影响流程**（cancel、block、transform、handled）
- 某些返回结果会**短路**后续 handler 的执行（如 `cancel: true`）

### 2.2 工具注册表

```typescript
class ExtensionRunner {
  private tools: Map<string, RegisteredTool>;  // 名称 → { definition, sourceInfo }

  registerTool(toolDefinition: ToolDefinition): void {
    this.tools.set(toolDefinition.name, {
      definition: toolDefinition,
      sourceInfo: { filePath: extensionPath, ... },
    });
  }

  getAllRegisteredTools(): RegisteredTool[] {
    return Array.from(this.tools.values());
  }
}
```

工具注册后不会立即生效——它先存在 ExtensionRunner 的 tools Map 中。之后当 `AgentSession._refreshToolRegistry()` 被调用时，这些工具才会被 wrap、过滤、转为 Agent.state.tools。

### 2.3 命令/快捷键/Flag 注册表

```typescript
class ExtensionRunner {
  private commands: Map<string, RegisteredCommand>;  // "/my-cmd" → { handler, description }
  private shortcuts: Map<KeyId, ExtensionShortcut>;  // "ctrl+x" → { handler, description }
  private flags: Map<string, ExtensionFlag>;         // "--my-flag" → { type, default, description }
}
```

这些都独立于事件总线——命令在 AgentSession._tryExecuteExtensionCommand 中查询，快捷键在 TUI 层查询，flag 在 CLI 参数解析时读取。

---

## 3. 关键事件类型及其作用

> **📍 当前位置**：以下事件在 [02-agent-session 全局地图](02-agent-session.md) 的步骤 1-8 中被触发。每个事件的触发时间对应 prompt() 链路的特定步骤。

### 3.1 输入阶段事件

| 事件 | 触发时机 | 触发位置 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------|---------------|------------------|
| `input` | 用户输入到达后，Skill 展开前 | prompt() 步骤 2 | `{ action: "handled" }` — 消费输入 / `{ action: "transform", text, images }` — 改写输入 / `{ action: "continue" }` — 原样继续 | —（UEMCPHarness 无输入拦截） |
| `before_agent_start` | 即将调用 agent.prompt() 前 | prompt() 步骤 6 | `{ systemPrompt: "..." }` — 替换本轮 system prompt / `{ messages: [...] }` — 注入自定义消息 | `_build_instructions()` + `assemble_system_prompt()` |

### 3.2 Agent 生命周期事件

| 事件 | 触发时机 | 触发位置 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------|---------------|------------------|
| `agent_start` | Agent 循环启动 | Agent 循环内部 | 无 | `ToolCallLogger.start()` |
| `agent_end` | Agent 循环结束 | Agent 循环内部 | 无 | — |
| `agent_settled` | 所有后处理完成 | AgentSession._emitAgentSettled | 无 | — |
| `turn_start` | 每轮 turn 开始 | Agent 循环内部 | 无 | — |
| `turn_end` | 每轮 turn 结束（含 toolResults） | Agent 循环内部 | 无 | `call_tool()` 的 post_call 阶段 |

### 3.3 消息事件

| 事件 | 触发时机 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------------|------------------|
| `message_start` | 任何消息开始（user/assistant/toolResult） | 无 | — |
| `message_update` | assistant 消息流式更新（每收到一个 token） | 无 | — |
| `message_end` | 任何消息结束 | `{ message: replacementMessage }` — **替换消息内容**（保持原始角色） | — |

**`message_end` 的特殊性**：如果 handler 返回了 `{ message: replacement }`，AgentSession 会用 `_replaceMessageInPlace()` 修改原始消息对象。这会影响**后续持久化到磁盘的内容**和**后续事件中的 message 引用**。

### 3.4 工具执行事件

> **📍 当前位置**：[01-agent-core](01-agent-core.md) 第 4 节"工具执行三阶段"的对应事件。

| 事件 | 触发时机 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------------|------------------|
| `tool_call` | 工具执行前（prepare 阶段） | `{ block: true, reason: "..." }` — 阻止执行 / 可 mutate `event.input` 修改参数 | `SafetyGuardrails.pre_call()` |
| `tool_execution_start` | 工具开始执行（execute 阶段） | 无 | `ToolCallLogger.post_call()` 的开始部分 |
| `tool_execution_update` | 工具执行中部分结果 | 无 | — |
| `tool_execution_end` | 工具执行结束（execute 完成） | 无 | `ReadbackInterceptor.post_call()` / `VisionInterceptor.post_call()` |
| `tool_result` | 工具执行后（finalize 阶段） | `{ content, details, isError, usage }` — 覆盖结果字段 | `StateCacheInterceptor.post_call()` 的概念位置 |

**`tool_call` vs `tool_execution_start`**：
- `tool_call`：在 **prepare 阶段**，可以 block 或改参数。参数已经被 TypeBox 校验。
- `tool_execution_start`：在 **execute 阶段**，工具已经开始执行了。只能观察，不能阻止。

### 3.5 Provider / Context 事件

| 事件 | 触发时机 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------------|------------------|
| `context` | context 发送给 LLM 前（transformContext 钩子） | `{ messages: AgentMessage[] }` — 替换消息列表 | `transformContext` — 可注入 State Cache 快照 |
| `before_provider_request` | LLM payload 构造完成后、发送前 | 可替换整个 payload | `onPayload` — 调试/观察 |
| `before_provider_headers` | HTTP 请求 headers 组装后、发送前 | 可 mutate `headers`（in-place） | — |
| `after_provider_response` | LLM HTTP 响应收到后、解析前 | 无 | `onResponse` — 调试/观察 |

### 3.6 Session 生命周期事件

| 事件 | 触发时机 | Handler 可返回 | UEMCPHarness 类比 |
|------|---------|---------------|------------------|
| `session_start` | session 创建/恢复/reload/new/fork 时 | 无 | `cmd_start()` 中的初始化 |
| `session_before_switch` | /new 或 /resume 前 | `{ cancel: true }` — 阻止切换 | — |
| `session_before_fork` | /fork 前 | `{ cancel: true }` — 阻止分叉 | — |
| `session_before_compact` | compaction 前（手动或自动） | `{ cancel: true }` — 取消 / `{ compaction: {...} }` — 提供自定义摘要 | — |
| `session_compact` | compaction 完成后 | 无 | — |
| `session_before_tree` | 导航到 session 树的某个节点前 | `{ cancel: true }` / `{ summary: {...} }` | — |
| `session_tree` | 导航完成后 | 无 | — |
| `session_shutdown` | session 销毁前（quit/reload/new/resume/fork） | 无 | `cmd_start()` 的 finally 清理 |
| `session_info_changed` | session 名称变更时 | 无 | — |

---

## 4. 工具注册流程（深度级）

> **📍 当前位置**：这发生在 `AgentSession._refreshToolRegistry()` 中——AgentSession 构造时调用一次，reload 时再调用一次。

### 4.1 完整流程

```
扩展调用 pi.registerTool(toolDefinition)
  │
  ▼
ExtensionRunner.registerTool()
  → 存入 this.tools Map<名称, RegisteredTool>
  → 此时 Agent 还看不到这个工具
  │
  ▼
AgentSession._refreshToolRegistry()
  │
  ├── 1. 收集所有工具来源：
  │      ├── 基础工具（built-in）：read, bash, edit, write, grep, find, ls
  │      │   → 或者使用 baseToolsOverride（自定义基础工具）
  │      ├── SDK 注册工具：customTools 参数传入的
  │      └── 扩展注册工具：ExtensionRunner.getAllRegisteredTools()
  │
  ├── 2. 过滤：
  │      ├── allowedToolNames（白名单）→ 不在白名单的工具被丢弃
  │      └── excludedToolNames（黑名单）→ 在黑名单的工具被丢弃
  │
  ├── 3. 提取 promptSnippet + promptGuidelines：
  │      → 这些会被注入到 system prompt 的"可用工具"部分
  │      → 没有 promptSnippet 的自定义工具不会出现在 system prompt 的工具列表中
  │
  ├── 4. wrapRegisteredTools()：
  │      → 在每个工具的 execute() 外面包一层事件发射：
  │        执行前 → 发射 tool_execution_start
  │        执行中 → onUpdate 回调 → 发射 tool_execution_update
  │        执行后 → 发射 tool_execution_end
  │      → 返回：AgentTool[]（Agent 可理解的标准工具格式）
  │
  ├── 5. 构建 this._toolRegistry Map<名称, AgentTool>
  │
  └── 6. 调用 setActiveToolsByName(toolNames)：
         → agent.state.tools = 被选中的工具
         → _rebuildSystemPrompt(toolNames)
           → agent.state.systemPrompt 被重新生成
```

### 4.2 wrapRegisteredTools() 做了什么

这是关键包装函数——把扩展注册的 ToolDefinition 转为 Agent 可执行的 AgentTool：

```typescript
function wrapRegisteredTools(tools, runner): AgentTool[] {
  return tools.map(({ definition, sourceInfo }) => ({
    ...definition,  // name, description, parameters, label
    prepareArguments: definition.prepareArguments,  // 兼容垫片
    executionMode: definition.executionMode,        // 并行/串行

    // 核心：包装 execute()
    async execute(toolCallId, params, signal, onUpdate) {
      // 1. 发射 tool_execution_start
      await runner.emit({ type: "tool_execution_start", toolCallId, toolName, args: params });

      // 2. 调用扩展的原始 execute()
      const result = await definition.execute(toolCallId, params, signal, (partial) => {
        // 每次 onUpdate 回调 → 发射 tool_execution_update
        runner.emit({ type: "tool_execution_update", toolCallId, toolName, args: params, partialResult: partial });
        onUpdate?.(partial);  // 同时传递给 Agent 循环的 onUpdate
      });

      // 3. 发射 tool_execution_end
      await runner.emit({ type: "tool_execution_end", toolCallId, toolName, result, isError: false });

      return result;
    },
  }));
}
```

**效果**：扩展只写 `execute()` 的业务逻辑，wrap 函数自动帮它发射标准事件。这就是为什么扩展不需要手动发射 `tool_execution_start` 事件。

---

## 5. Command 系统

### 5.1 注册和调用

```typescript
// 扩展代码
pi.registerCommand("status", {
  description: "Show project status",
  handler: async (args: string, ctx: ExtensionCommandContext) => {
    // args = 用户在 /status 后面的所有文本
    // ctx = ExtensionCommandContext（包含所有 session 操作方法）
    await ctx.ui.notify("Project has 42 files");
  },
});

// 用户输入 "/status --verbose" → AgentSession._tryExecuteExtensionCommand()
// → ExtensionRunner.getCommand("status") → handler("--verbose", ctx)
```

### 5.2 Command 为何不能排队

```typescript
// AgentSession.prompt() 中：
if (text.startsWith("/")) {
  const handled = await this._tryExecuteExtensionCommand(text);
  if (handled) return;  // ← 直接返回，不发往 Agent
}

// steer() / followUp() 中：
if (text.startsWith("/")) {
  this._throwIfExtensionCommand(text);  // ← 直接抛异常
}
```

原因：Command handler 接收 `ExtensionCommandContext`，其中包含 `newSession()` / `fork()` / `switchSession()` 等方法。这些方法会销毁当前 session——如果在 Agent 运行中执行，会导致不可预期的状态。

---

## 6. 与 UEMCPHarness Interceptor 链的精确对应

| UEMCPHarness | PiAgent 插入点 | 实现方式 |
|---|---|---|
| **DebugPreCall** | `tool_call` 事件 | `pi.on("tool_call", (e) => { log(e.toolName, e.input) })` |
| **ReadbackInterceptor** | `tool_result` 事件 + 后处理 | `pi.on("tool_result", async (e) => { if (isWriteTool(e.toolName)) { /* L2 readback */ } })` |
| **ToolCallLogger** | `message_end` 事件 | `pi.on("message_end", (e) => { writeJSONL(e.message) })` |
| **StateCacheInterceptor** | `tool_result` 事件 + 自定义状态 | `pi.on("tool_result", (e) => { updateCache(e.toolName, e.details) })` |
| **DriftAlertInterceptor** | `context` 事件 + 注入 | `pi.on("context", (e) => { if (driftDetected) e.messages.push(warningMessage) })` |
| **VisionInterceptor** | `tool_execution_end` 事件 | `pi.on("tool_execution_end", async (e) => { if (isScreenshot(e.toolName)) { /* vision analysis */ } })` |
| **SnapshotRecorder** | 自定义逻辑 + `tool_execution_end` | 同上，额外做文件归档 |
| **_build_instructions()** | `before_agent_start` 事件 | `pi.on("before_agent_start", (e) => { return { systemPrompt: injectSkills(e) } })` |

---

下一篇 [04-protocol-and-server](04-protocol-and-server.md) 将解析 PiAgent 的通信协议和服务器——它是怎么把 AgentSession 暴露给远程客户端的。
