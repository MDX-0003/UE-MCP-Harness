# 08 — 一次完整对话的生命周期

**前置阅读**：全部 01-07 文档
**阅读目标**：用 UE 场景串联所有知识，验证每个概念在真实流程中的位置

---

## 全局地图：两条视角

本文从一个具体 UE 场景出发，用**两条视角**走通相同的内容：

```
视角 A：代码调用层（哪个函数调了哪个函数）
视角 B：事件序列层（什么时间发射了什么事件、谁监听了它）
```

| 时间 | 视角 A (调用链) | 视角 B (事件序列) |
|------|----------------|------------------|
| T+0 | 用户按回车 | — |
| T+1 | AgentSession.prompt() | input 事件 |
| T+2 | _expandSkillCommand() | — |
| T+3 | emitBeforeAgentStart() | before_agent_start 事件 |
| T+4 | Agent.prompt() | agent_start → turn_start |
| T+5 | streamAssistantResponse() | message_start(assistant) |
| T+6 | (流式 token) | message_update × N |
| T+7 | — | message_end(assistant) |


---

## 场景：把 PointLight 的颜色改成参考图的暖色

**假设**：
- UE 编辑器已启动，MCP Server 在 :8000 运行
- UE Harness 扩展已注册（假设已迁移到 PiAgent，作为扩展运行）
- `MatchAtmosphereSkill.md` 存放在 `.pi/skills/` 中
- 用户已通过 `pi` 命令启动了 PiAgent TUI

**用户输入**：
```
/skill:match-atmosphere 把场景里的 PointLight 改成和参考图一致的暖色调
```

---

## 视角 A：代码调用链

### A.1 T+0 — 用户按回车

```typescript
// [07-main-and-modes] InteractiveMode.run()
// 终端读取用户输入
const text = "/skill:match-atmosphere 把场景里的 PointLight 改成和参考图一致的暖色调";

// [07] → 调用 AgentSession.prompt()
await runtime.session.prompt(text);
```

### A.2 T+1 — AgentSession.prompt() 开始处理

```typescript
// [02-agent-session §2] prompt() 的 8 个步骤开始

// 步骤 1：扩展命令检查
// "/skill:match-atmosphere" → 先查 ExtensionRunner.getCommand("skill:match-atmosphere")
// → 没有注册过此命令 → 继续
// → 但 "/skill:" 前缀匹配 `_expandSkillCommand()`

// 步骤 2：input 事件
// [03-extension-system §3.1] → ExtensionRunner.emitInput()
// → 如果有扩展注册了 pi.on("input", ...)，此时被调用
// → 这里没有扩展拦截 → action: "continue" → 原样继续

// 步骤 3：Skill 展开
// [06-tools-and-skills §2.4] → _expandSkillCommand()
let expandedText = "/skill:match-atmosphere 把 PointLight 改成暖色调";
const skill = resourceLoader.getSkills().skills.find(s => s.name === "match-atmosphere");
const content = readFileSync(skill.filePath, "utf-8");
const body = stripFrontmatter(content).trim();

expandedText = `<skill name="match-atmosphere" location="/path/to/skill.md">
References are relative to /path/to/skill.md parent directory.

${body}
</skill>

把 PointLight 改成暖色调`;
```

### A.3 T+2 — before_agent_start

```typescript
// 步骤 4：Prompt 模板展开 — 跳过（没有匹配模板）

// 步骤 5：流式/非流式分支
// Agent 空闲 → isStreaming = false → 继续（不走排队路径）

// 步骤 6：before_agent_start 事件
// [03-extension-system §3.1] → ExtensionRunner.emitBeforeAgentStart()
const result = await runner.emit({
  type: "before_agent_start",
  prompt: expandedText,
  systemPrompt: baseSystemPrompt,
  systemPromptOptions: { cwd, skills, contextFiles, selectedTools, ... },
});

// UE Harness 扩展在这里注入了：
// 1. State Cache 快照（当前场景中所有 Actor 的列表）
// 2. Vision 验证流程 instructions
// 3. 最近操作记录
if (result?.systemPrompt !== undefined) {
  agent.state.systemPrompt = result.systemPrompt;
}
```

### A.4 T+3 — 进入 Agent 循环

```typescript
// 步骤 7：_runAgentPrompt()
// [02-agent-session §2 步骤 8]
await this.agent.prompt([
  { role: "user", content: [{ type: "text", text: expandedText }], timestamp: Date.now() },
]);
```

### A.5 T+4~T+12 — Agent 循环内部（多轮 turn）

#### Turn 1：LLM 分析 skill 内容并确定第一步

```
[01-agent-core §3] 内层循环第 1 轮

3. streamAssistantResponse() → LLM 返回:
   "好的，我按照 SOP 的步骤 1 进行。先找到场景中的所有灯光。"
   toolCalls: [
     { name: "get_all_light_actors", args: {} }
   ]
   stopReason: "toolUse"

5-6. 执行工具:
   [01-agent-core §4] prepare → execute → finalize

   prepare:
     → 在 agent.state.tools 中查找 "get_all_light_actors"
     → toolCall 被 wrapRegisteredTools 包装过，自动发射 tool_execution_start

   execute:
     → get_all_light_actors.execute() → 返回 [{ name: "PointLight_0", ... }]

   finalize:
     → afterToolCall: 发射 "tool_result" 扩展事件
     → [03-extension-system §3.4] 扩展收到 → 更新 State Cache

   return: toolResult { content: [{ type: "text", text: "找到 PointLight_0" }] }

8. turn_end: 附带 toolResults
```

#### Turn 2：LLM 获取灯光属性

```
[01-agent-core §3] 内层循环第 2 轮

11. 检查 steering 队列 → 空
    → 继续下一轮

3. streamAssistantResponse() → LLM 返回:
   "现在获取 PointLight_0 的当前属性。"
   toolCalls: [
     { name: "get_properties", args: { actor: "PointLight_0", properties: ["LightColor", "Intensity"] } }
   ]

5-6. 执行工具 → 返回 { LightColor: "(1,1,1)", Intensity: "50" }
   → afterToolCall: State Cache 更新 PointLight_0 的属性缓存
```

#### Turn 3：LLM 修改灯光颜色

```
[01-agent-core §3] 内层循环第 3 轮

3. streamAssistantResponse() → LLM 返回:
   "参考图是暖色调，大概对应 R:B ratio 约 1.8。我把 LightColor 改成 (1, 0.7, 0.4)。"
   toolCalls: [
     { name: "set_properties", args: { actor: "PointLight_0", properties: { LightColor: "(1,0.7,0.4)" } } }
   ]

5-6. 执行工具 → 返回成功
   → afterToolCall: [03-extension-system §6] ReadbackInterceptor 等价逻辑:
     L2 读回验证：用 get_properties 读回刚写的值
     → 确认 LightColor 确实是 (1,0.7,0.4) → 验证通过
     → 注入 badge: "✓ L2 读回确认：LightColor = (1,0.7,0.4)"
```

#### Turn 4：LLM 截图验证

```
[01-agent-core §3] 内层循环第 4 轮

3. streamAssistantResponse() → LLM 返回:
   "现在截图验证效果。"
   toolCalls: [
     { name: "vision_screenshot", args: { question: "PointLight_0 照射到的表面是否呈现暖色调？" } }
   ]

5-6. 执行 vision_screenshot:
   → [06-tools-and-skills §5.7] 扩展内部调 streamSimple() 做独立 Vision 请求
   → 不经过 Agent 循环、不污染对话历史
   → Vision 模型返回: "被照射表面呈现暖黄色，R:B ratio 约 1.6。与参考图相比偏黄了一点，建议稍微降低 Green 分量。"

   → [03-extension-system §6] afterToolCall: VisionInterceptor 等价逻辑
     → 将 vision_verdict 写入自定义 entry（SessionManager.appendCustomEntry）
     → 结果作为 toolResult content 返回给 LLM
```

#### Turn 5：LLM 微调 + 再验证

```
[01-agent-core §3] 内层循环第 5 轮

3. streamAssistantResponse() → LLM 返回:
   "Vision 建议降低 Green。我把 Green 从 0.7 降到 0.6。"
   toolCalls: [
     { name: "set_properties", args: { actor: "PointLight_0", properties: { LightColor: "(1,0.6,0.4)" } } }
   ]

5-6. 执行 → L2 读回确认 → 截图 → Vision 确认通过

11. 检查 steering 队列 → 空
    检查 followUp 队列 → 空
    → 退出内层循环
```

### A.6 T+13 — 循环后处理

```typescript
// [02-agent-session §2 步骤 8]
// 内层循环结束后，_handlePostAgentRun() 被调用

// 检查 auto-retry: 最后一轮 assistant 的 stopReason 是 "stop" → 不重试
// 检查 auto-compaction: context tokens 低于阈值 → 不压缩
// 检查 queued messages: agent.hasQueuedMessages() → false

// AgentSession._emitAgentSettled()
// → agent_settled 事件 → UI 更新（TUI 显示"空闲"状态）
this._isAgentRunActive = false;
```

---

## 视角 B：事件序列

### B.1 AgentSession 层事件（T+1 ~ T+3）

```
事件类型                      触发者                         监听者
────────────────────────────────────────────────────────────────
input                         AgentSession.prompt() 步骤 2    扩展 (可选拦截)
  → action: "continue"

before_agent_start            AgentSession.prompt() 步骤 6    扩展 (注入 system prompt)
  → { systemPrompt: "..." }
```

### B.2 Agent 循环事件（T+4 ~ T+12）

```
事件类型                      触发者                         副作用
────────────────────────────────────────────────────────────────
agent_start                   runAgentLoop()                 无

turn_start (第 1 轮)          runAgentLoop()                 无

message_start (user)          注入用户消息                   AgentSession 清理队列 UI
message_end (user)            消息完成                       SessionManager.appendMessage() ← 写入磁盘

message_start (assistant)     streamAssistantResponse()      UI 显示"LLM 正在回复"
message_update × N            每个 token                    UI 更新显示
message_end (assistant)       完成                           SessionManager.appendMessage() ← 写入磁盘

tool_execution_start          wrap 函数 (tool 1)             UI 显示"正在执行 get_all_light_actors"
tool_execution_end            执行完成                       扩展: State Cache 更新

message_start (toolResult)    拼装 toolResult message       无
message_end (toolResult)      完成                           SessionManager.appendMessage()

turn_end (第 1 轮)            本轮的 toolResults 收集完成    无

turn_start (第 2 轮)          新 cycle 开始                  无
  ... message_start/update/end ...
  ... tool_execution_start/end ...
turn_end (第 2 轮)            本轮的 toolResults 收集完成    无

turn_start (第 3 轮)          新 cycle 开始                  无
  ... (set_properties + L2 readback) ...
turn_end (第 3 轮)            本轮的 toolResults 收集完成    无

turn_start (第 4 轮)          新 cycle 开始                  无
  ... (vision_screenshot + Vision LLM) ...
turn_end (第 4 轮)            本轮的 toolResults 收集完成    无

turn_start (第 5 轮)          新 cycle 开始                  无
  ... (微调 + 再验证) ...
turn_end (第 5 轮)            本轮的 toolResults 收集完成    无

agent_end                     循环退出                       扩展通知 → 检查 retry/compact
```

### B.3 后处理事件（T+13）

```
agent_end (willRetry: false)  _handleAgentEvent              UI 显示"完成"
agent_settled                  所有后处理完成                 UI 显示"空闲"，编辑器可用
queue_update (steer: [],       队列被清空                     UI 清除排队提示
  followUp: [])
```

---

## 视角 C：每条消息何时写入磁盘

```
消息                           写入时机                          写入内容
────────────────────────────────────────────────────────────────
user message (skill 展开后)    message_end (user)               完整 user message
assistant (Turn 1)             message_end (assistant)          完整 assistant message
toolResult (get_all_light)   message_end (toolResult)          工具结果
assistant (Turn 2)             message_end (assistant)          完整 assistant message
toolResult (get_properties)  message_end (toolResult)          工具结果
assistant (Turn 3)             message_end (assistant)          完整 assistant message
toolResult (set_properties)  message_end (toolResult)          工具结果
assistant (Turn 4)             message_end (assistant)          完整 assistant message
toolResult (vision_screenshot) message_end (toolResult)         工具结果 + vision verdict
assistant (Turn 5)             message_end (assistant)          完整 assistant message
toolResult (set_properties)  message_end (toolResult)          工具结果
assistant (最终)               message_end (assistant)          最终 assistant message
```

**全量：5 轮 turn × (1 user + 1 assistant + 1 toolResult) = 15 条 message entry 被写入 JSONL。**

---

## 迁移后 vs 迁移前

| 维度 | UEMCPHarness 当前 | PiAgent 扩展 (迁移后) |
|------|------------------|---------------------|
| **LLM 连接** | Harness MCP Server → LLM 的 tool call → Harness 拦截 → UE | PiAgent Agent 循环 → tool call → 扩展拦截 → UE |
| **Vision 验证** | VisionInterceptor.post_call → `VisionSubAgent._call_vision_api()` | `pi.on("tool_execution_end", ...)` → `streamSimple()` 独立调用 |
| **L2 读回** | ReadbackInterceptor → 在 post_call 中追加 badge | `pi.on("tool_result", ...)` → 读回验证 → 修改 result content |
| **State Cache** | StateCacheInterceptor → `WorldState.actors` 更新 | 扩展内部 Map 或 custom entry 持久化 |
| **Skill 激活** | `activate_skill()` HarnessTool → 修改 system prompt | `/skill:name` 自动展开 + `before_agent_start` 事件注入 |
| **JSONL 日志** | ToolCallLogger 独立写入 | SessionManager 自带（15 条 message entry 自动写入） |
| **Session 恢复** | 无（每次新 session） | 支持（JSONL → `buildSessionContext()` → 恢复完整对话） |

---

下一篇 [09-extension-tutorial](09-extension-tutorial.md) 将以一个最小化例子展示"从零写一个 PiAgent 扩展"。
