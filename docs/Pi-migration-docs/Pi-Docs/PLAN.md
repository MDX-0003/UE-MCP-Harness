# PiAgent 架构文档群 — 编写计划

**目标读者**：理解 UEMCPHarness 但初学 PiAgent 的开发者
**状态**：已确认，待开工

---

## 阅读地图

```
                     ┌─────────────────────┐
                     │  00-overview        │  ← 先读：全景 + 包依赖关系
                     └────────┬────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ 01-agent-core   │  │ 02-agent-session │  │ 03-extension-    │
│ (Agent + Loop)  │  │ (编排层)         │  │ system           │
└────────┬────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                    │                     │
         ▼                    ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ 04-protocol-    │  │ 05-session-      │  │ 06-tools-and-    │
│ and-server      │  │ manager          │  │ skills           │
└────────┬────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                    │                     │
         └────────────────────┼─────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │ 07-main-and-modes   │  ← CLI 入口 + 模式分发
                    └────────┬────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │ 08-full-lifecycle   │  ← 完整事件序列
                    └────────┬────────────┘
                             │
                             ▼
                    ┌─────────────────────┐
                    │ 09-extension-       │  ← 最后读：从零写扩展
                    │ tutorial            │
                    └─────────────────────┘
```

---

## 文档清单

### 00 — 全景概览 (overview)

**内容**：
- PiAgent 是什么、解决什么问题
- 8 个 package 的职责一句话总结
- 包之间的依赖关系图
- 一个请求从头到尾经过哪些代码层（宏观时序图）
- 关键术语表（AgentMessage / AgentTool / AgentContext / AgentEvent / SessionManager / ExtensionRunner …）

**深度**：概念级。不涉及任何函数签名。

**与其他文档的关系**：所有后续文档的索引 + 术语锚点。

---

### 01 — Agent 核心：Agent 类 + Agent 循环 (agent-core)

**内容**：

1. **Agent 类的结构**（接口级）
   - 构造参数 AgentOptions 各字段的含义
   - state（AgentState）的读写语义
   - 三个队列：steering / followUp / pendingToolCalls
   - subscribe() 监听器机制
   - prompt() / continue() / abort() / reset() 生命周期

2. **Agent 循环的完整流程**（深度级——核心路径逐行解读）
   - `runAgentLoop()` vs `runAgentLoopContinue()` 的区别
   - 外层循环（follow-up）和内层循环（turn）的嵌套关系
   - 每一轮 turn 的 11 个步骤：turn_start → transformContext → convertToLlm → streamFn → tool call extraction → 执行 → toolResults → turn_end → prepareNextTurn → shouldStopAfterTurn → 检查队列
   - **关键副作用**：哪些步骤修改了 agent.state.messages？哪些步骤触发了事件？哪些步骤会"卡住"等待 LLM？

3. **工具执行的三种模式**（深度级）
   - sequential vs parallel 的执行顺序差异
   - prepare → execute → finalize 三阶段
   - beforeToolCall / afterToolCall 钩子的调用时机和参数
   - terminate 提前终止机制

4. **流式响应处理**
   - streamAssistantResponse() 的内部逻辑
   - StreamingMessage 的累积过程
   - stopReason 的各种取值（stop / length / toolUse / error / aborted）的含义

**关键问题回答**：
- 如果我有一个外部工具（比如 UE screenshot），我应该在哪一步拦截？
- 如果我想在工具执行后追加额外信息到 LLM 上下文，应该用什么钩子？

---

### 02 — AgentSession：编排层 (agent-session)

**核心地位**：这是整个 coding-agent 最复杂的类（~3333 行），连接 Agent、SessionManager、ExtensionRunner、ResourceLoader、ModelRuntime。**值得自己的文档**。

**内容**：

1. **AgentSession 的定位**（概念级）
   - 它不跑 Agent 循环，它编排"谁在什么时候做啥"
   - 持有哪些依赖（Agent / SessionManager / ExtensionRunner / ResourceLoader / ModelRuntime / SettingsManager）
   - 它和 AgentSessionRuntime 的分工（SessionRuntime 管"哪个 session"，AgentSession 管"session 里发生了什么"）

2. **prompt() 的完整链路**（深度级——这是最核心的路径）
   - 扩展命令检查（/command → 同步执行，不排队）
   - input 事件（扩展可拦截/转换/消费）
   - Skill 展开（/skill:name → 读文件 → 包装 XML）
   - Prompt 模板展开（变量替换）
   - 流式/非流式的分支（steer/followUp 队列 vs 直接发送）
   - before_agent_start 事件（system prompt 注入点）
   - _runAgentPrompt() 的内部：
     - agent.prompt(messages) → 内部 while 循环：每次 agent 停下后检查 auto-retry → auto-compaction → queued messages → agent.continue()
   - **关键副作用**：每一步对 state.messages / session 文件 / extension events 的影响

3. **Agent 事件 → Extension 事件 → Session 事件的映射**（接口级）
   - `_handleAgentEvent()` 如何将 Agent 的原始事件转换为 AgentSession 的富事件
   - `_emitExtensionEvent()` 如何将 Agent 事件映射到 ~30 种 Extension 事件
   - **什么时候事件被写入磁盘**（message_end → sessionManager.appendMessage）

4. **Compaction（上下文压缩）流程**（深度级）
   - 触发条件：threshold（超过配置百分比）vs overflow（LLM 返回溢出错误）
   - 手动 vs 自动两种路径
   - prepareCompaction → session_before_compact 事件 → compact() LLM 总结 → 替换 agent.state.messages → 持久化
   - **副作用**：哪些消息被保留？哪些被替换为摘要？

5. **Auto-Retry 机制**（接口级）
   - 什么错误会触发重试（overloaded / rate limit / server error，但不包括 context overflow）
   - 重试前的状态清理

6. **Model / Thinking Level 管理**（接口级）
   - setModel / cycleModel / setThinkingLevel / cycleThinkingLevel

7. **ModelRuntime 深读**（深度级——从零概念出发）
   - ModelRuntime 是什么、由谁创建、被谁持有
   - 它和 Model（单个模型描述）的区别
   - 核心能力：获取可用模型列表、解析模型认证、发起 LLM 请求
   - `getAuth(provider)`：认证流程（API key / OAuth / 环境变量 三种路径）
   - `checkAuth(provider)` vs `hasConfiguredAuth(provider)` 的区别
   - `getAvailable()` / `getModel(provider, id)`：模型查找
   - `streamSimple()`：这是如何被传给 Agent 的 `streamFn` 的
   - `registerProvider()` / `registerNativeProvider()`：扩展如何注册自定义 LLM provider
   - 关键问题：如果我想让 Vision 验证用不同的模型（比如 MiMo API），ModelRuntime 如何支持？

8. **AgentSessionRuntime 深读**（深度级——从零概念出发）
   - AgentSessionRuntime 是什么：它不是"跑 session 的东西"，它是"session 的容器 + 生命周期管理器"
   - 持有三样东西：当前 AgentSession + 当前 AgentSessionServices + 创建 runtime 的工厂函数
   - 和 AgentSession 的分工：
     - AgentSession = 一个 session 内部发生了什么（消息、工具、prompt）
     - AgentSessionRuntime = session 之间的切换（new / fork / switch / import）
   - 核心方法：
     - `newSession()`：触发 session_before_switch 事件 → 销毁旧 session → 创建新 SessionManager → 调用工厂创建新 AgentSession → 触发 session_start
     - `fork(entryId)`：从某个历史 entry 分叉出新的 session 分支
     - `switchSession(path)`：切换到另一个 session 文件
     - `importFromJsonl(path)`：从 JSONL 文件导入 session
   - 关键副作用：每次切换都触发 `session_shutdown` → `dispose()` → 清理扩展上下文
   - 为什么这关系到 UE 迁移：UE 连接是跟着 session 走还是跟着 runtime 走？session 切换时 UE 连接要不要断？

### 03 — 扩展系统 (extension-system)

### 03 — 扩展系统 (extension-system)

**核心地位**：这是 PiAgent 的"插件/中间件"系统，是 UEMCPHarness 的 Interceptor 链的等价物。

**内容**：

1. **扩展的三种加载方式**（接口级）
   - 文件扩展（.pi/extensions/*.ts）
   - 内联扩展（ExtensionFactory 函数）
   - 内置扩展（llama.cpp 例子）

2. **ExtensionRunner 的结构**（接口级）
   - 事件总线：handlers Map<事件类型, 处理函数[]>
   - 工具注册表：tools Map<名称, ToolDefinition>
   - 命令/快捷键/Flag 注册表
   - initialize() → bindCore() 的两阶段初始化

3. **关键事件类型及其作用**（深度级——每类一个表格行，含"UEMCPHarness 等价物"列）

   | 事件 | 触发时机 | 可返回值 | UEMCPHarness 等价 |
   |------|---------|---------|------------------|
   | `input` | 用户输入到达时 | handled / transform | — |
   | `before_agent_start` | prompt 发送给 Agent 前 | 改 systemPrompt / 注入 custom message | `_build_instructions()` |
   | `tool_call` | 工具执行前（beforeToolCall 钩子） | block / 改参数 | `Interceptor.pre_call()` |
   | `tool_result` | 工具执行后（afterToolCall 钩子） | 改结果内容 | `Interceptor.post_call()` |
   | `tool_execution_start/end` | 工具开始/结束执行时 | — | VisionInterceptor / ReadbackInterceptor |
   | `message_end` | 每条消息结束时（含 user/assistant/toolResult） | 替换消息 | — |
   | `turn_end` | 每轮 turn 结束时 | — | — |
   | `agent_end` | Agent 循环结束时 | — | — |
   | `context` | context 发送给 LLM 前 | 改 messages | — |
   | `session_before_compact` | compaction 前 | cancel / 自定义摘要 | — |

4. **工具注册流程**（深度级）
   - 扩展调用 `pi.registerTool(toolDefinition)` → Runner 存入 tools Map
   - AgentSession._refreshToolRegistry() 何时被调用
   - wrapRegisteredTools() 做了什么（在工具外套一层 event emit）
   - 最终如何变成 Agent.state.tools

5. **Command 系统**（接口级）
   - /command → AgentSession._tryExecuteExtensionCommand()
   - Command 为何不能排队（必须在空闲时执行）

---

### 04 — 协议与服务器 (protocol-and-server)

**核心地位**：PiAgent 自有的二进制协议 + Server。理解它才能判断"UEMCPHarness 的 MCP Server :9000 能否替代/合并"。

**内容**：

1. **协议栈**（接口级）
   - 传输层：Unix Socket / TCP
   - 帧层：4 字节 uint32 BE 长度前缀 + CBOR 负载
   - 消息层：TypeBox 类型校验
   - 握手：ClientHello → ServerHello（token 认证 + 版本协商）

2. **PiServer 的架构**（接口级）
   - 连接状态机：awaitingHello → handshaking → ready → closing → closed
   - LiveSessionManager 如何管理活跃 session
   - Snapshot 广播机制（revision 计数器 + 每连接 attached 状态）

3. **PiClient 的架构**（接口级）
   - 连接 → 握手 → session lease 管理
   - 排他锁 vs 共享锁
   - request/response 配对

4. **PiSessionRuntime 接口**（深度级）
   - 这是"如何把 AgentSession 暴露给远程客户端"的抽象
   - snapshot / prompt / steer / abort / setModel / setThinking / dispose
   - 与 AgentSession 的关系

---

### 05 — SessionManager：会话持久化 (session-manager)

**内容**：

1. **Session 存储模型**（接口级）
   - SessionEntry 类型：message / compaction / branchSummary / custom / modelChange / thinkingLevelChange / labelChange
   - SessionHeader 结构（version, cwd, model, created, updated）
   - JSONL 文件格式（每行一个 JSON 对象）

2. **分支/树结构**（深度级）
   - parentId 指针形成树
   - leafId 指向当前活跃分支
   - createBranchedSession() 的分叉逻辑
   - navigateTree() 的导航逻辑

3. **buildSessionContext()**（深度级）
   - 从 JSONL entries 重建 agent.state.messages
   - Compaction 边界处理：只纳入最新 compaction 之后的条目
   - Branch summary 的处理

4. **与 UEMCPHarness 的对应**
   - UEMCPHarness ToolCallLogger（JSONL 写入）→ PiAgent SessionManager 已覆盖
   - UEMCPHarness SnapshotRecorder（截图归档）→ 需额外实现

---

### 06 — 工具与技能系统 (tools-and-skills)

**内容**：

1. **ToolDefinition 的结构**（接口级）
   - name / label / description / parameters（TypeBox schema）
   - promptSnippet / promptGuidelines（系统提示词中的工具描述）
   - execute() 签名
   - renderCall / renderResult（TUI 渲染，迁移中不需要）

2. **内置工具清单**（接口级）
   - read / write / edit / bash / grep / find / ls
   - 每个工具的 input 类型、details 类型

3. **Skill 系统**（深度级）
   - .pi/skills/*.md 的文件结构（YAML frontmatter + Markdown body）
   - ResourceLoader 如何发现 skills
   - Skill 如何嵌入 system prompt
   - /skill:name 命令的展开流程
   - 与 UEMCPHarness SkillRegistry 的对应关系

4. **Prompt Template 系统**（接口级）
   - 文件位置、变量替换语法

5. **Context Files（AGENTS.md / CLAUDE.md）**（接口级）
   - ResourceLoader 如何加载
   - 如何嵌入 system prompt

6. **streamSimple() 深读**（深度级——从零概念出发）
   - 它在哪定义、被谁调用、调用时机
   - **完整签名**：`streamSimple(model, context, options?) → AssistantMessageEventStream`
   - **参数拆解**：
     - `Model<Api>`：不是一个简单的 "model name"，而是包含 id / provider / api / baseUrl / contextWindow / cost / reasoning / input 等完整元数据的对象
     - `Context`：`{ systemPrompt: string, messages: Message[], tools: Tool[] }` —— 这是发送给 LLM 的完整 payload
     - `SimpleStreamOptions`：apiKey / headers / signal / onPayload / onResponse / thinkingLevel / thinkingBudgets 等选项
   - **返回值**：`AssistantMessageEventStream` —— 一个 AsyncIterable，每次迭代产出一个 `AssistantMessageEvent`。事件类型包括：`message_start` / `content_block_start` / `content_block_delta`（流式文本增量）/ `content_block_stop` / `message_delta`（usage 更新）/ `message_stop`（最终消息）
   - **使用示例**：如何用 `streamSimple()` 发一次独立的 LLM 请求（不经过 Agent 循环）
   - **对比**：Agent 用 `streamSimple` 做什么 vs Vision 验证用 `streamSimple` 做什么 —— 同一个函数，不同调用方式
   - 关键问题：如果 Vision 验证要用 MiMo API 而不是主 Agent 的模型，怎么做？

---

### 07 — CLI 入口与模式分发 (main-and-modes)

**内容**：

1. **main() 函数的完整流程**（接口级）
   - 参数解析 → 扩展加载 → session 创建/恢复 → 模式分发
   - 12 个主要步骤

2. **三种运行模式**（接口级）
   - InteractiveMode：TUI 全功能
   - PrintMode：单次运行，输出文本
   - RpcMode：JSON-RPC over stdin/stdout

3. **createAgentSession() 的完整链路**（深度级——与迁移直接相关）
   - 这是 SDK 入口，理解它才能理解"如果迁移为扩展，我从哪切入"
   - ModelRuntime / SettingsManager / ResourceLoader 的创建
   - Agent 的构造
   - AgentSession 的构造
   - 初始 tools 的确定
   - Session restore 逻辑

---

### 08 — 一次完整对话的生命周期 (full-lifecycle)

**内容**：

以具体例子串联前 7 个文档：

```
用户在 TUI 中输入 "把场景里的 PointLight 颜色改成红色"
  → 事件序列（精确到每个事件的 type + 参数 + 副作用）
  → 每个文档对应的处理段
  → 两次同样的流程但不同视角：
     视角 A: Agent 循环角度看（turn 级）
     视角 B: 事件角度看（event 序列）
```

**深度**：概念级 + 关键步骤的深度注解。



### 09 — 实战：从零写一个 PiAgent 扩展 (extension-tutorial)

**定位**：不是迁移计划的一部分，而是一个**教学例子**——展示"在 PiAgent 中插入自定义逻辑"的标准方式。

**内容**：

以一个最小化例子贯穿：**写一个 `tool_logger` 扩展**，每当 LLM 调用任何工具时，记录工具名和耗时到文件。

1. **扩展文件的结构**
   - 一个 `.ts` 文件的基本骨架：`export default function(pi: ExtensionAPI) { ... }`
   - 放到 `.pi/extensions/` 下，PiAgent 自动加载

2. **注册事件监听**
   - `pi.on("tool_execution_start", ...)` —— 记录开始时间
   - `pi.on("tool_execution_end", ...)` —— 计算耗时、写入文件
   - 解释 handler 函数的参数类型和返回值

3. **注册自定义工具**
   - `pi.registerTool({ name, description, parameters, execute })`
   - 展示 TypeBox schema 的写法（最小例子：一个没有任何参数的 `ping` 工具）
   - 工具如何返回 `AgentToolResult`（content / details / isError）

4. **注册 slash 命令**
   - `pi.registerCommand("my-command", { handler })`
   - Command handler 收到的参数：args 字符串 + ExtensionCommandContext

5. **修改 system prompt**
   - `pi.on("before_agent_start", ...)` → 注入自定义 instructions

6. **完整例子**：把上面的代码片段拼成完整的扩展文件

**不涉及**：TUI 组件、Provider 注册、Session 生命周期钩子（这些留在迁移阶段处理）

---

## 不覆盖的内容

以下内容**有意不纳入**（与迁移无关或可后续补充）：
- TUI 组件树渲染（migration 不涉及 UI）
- pi-ai 包内部 provider 实现（HTTP/SSE 细节）
- CBOR 编解码算法（协议层使用即可）
- vitest 测试框架内部
- Bun 编译打包流程

---

## 交叉引用设计

多个文档会从不同角度覆盖同一条路径：

| 路径 | 主文档 | 补充角度 |
|------|--------|---------|
| prompt → Agent → LLM → tool → result | 01-agent-core (Agent 循环角度) | 02-agent-session (编排角度), 03-extension-system (事件角度) |
| 工具执行 | 01-agent-core (执行阶段) | 03-extension-system (事件拦截), 06-tools-and-skills (工具定义) |
| System prompt 构建 | 02-agent-session (prompt() 链路) | 03-extension-system (before_agent_start), 06-tools-and-skills (skill 注入) |
| Session 持久化 | 05-session-manager | 02-agent-session (message_end 触发写入) |
| 启动流程 | 07-main-and-modes | 02-agent-session (AgentSession 构造), 03-extension-system (扩展加载) |
| LLM 调用 | 06-tools-and-skills (streamSimple 深读) | 01-agent-core (Agent 循环中调用), 02-agent-session (ModelRuntime 提供) |
| Session 生命周期 | 02-agent-session (AgentSessionRuntime 深读) | 05-session-manager (文件层), 07-main-and-modes (启动时选择 session) |

---

## 审阅检查清单

请在 00-overview 交付后给出反馈：

- [x] 文档数量：9 篇（00-08 核心 + 09 扩展教程）— 已确认
- [x] 每篇文档的深度标注（接口级 vs 深度级）— 已确认，新增三个深度级小节
- [x] 08-full-lifecycle 的例子：UE 场景（PointLight 改红色）— 已确认
- [x] 00-overview 先交付单独审阅 — 已确认
- [x] 文档输出到 `docs/Pi-migration-docs/Pi-Docs/` — 已确认
- [x] 新增内容：ModelRuntime 深读（02）、AgentSessionRuntime 深读（02）、streamSimple() 深读（06）— 已确认，全部深度级
