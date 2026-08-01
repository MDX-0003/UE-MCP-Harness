# 09 — 实战：从零写一个 PiAgent 扩展

**前置阅读**：全部 01-08 文档
**阅读目标**：用最小化代码示例理解"在 PiAgent 中插入自定义逻辑"的标准方式

**重要**：这**不是**迁移计划——这是一个教学例子，展示 PiAgent 扩展的标准模式。真实的迁移将在后续的迁移计划中详细展开。

---

## 场景

写一个扩展 `tool-logger`，功能：
- 每当 LLM 调用任何工具时，记录工具名、参数、耗时到文件 `~/.pi/tool-log.jsonl`
- 提供一个 `/stats` 命令，显示"从启动到现在一共调了多少次工具"

---

## 1. 扩展文件的基本骨架

创建文件 `.pi/extensions/tool-logger.ts`：

```typescript
// .pi/extensions/tool-logger.ts

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// PiAgent 自动调用这个函数，传入 ExtensionAPI
export default function toolLoggerExtension(pi: ExtensionAPI) {
  // 扩展代码写在这里...
}
```

**何时被执行**：PiAgent 启动时，`ResourceLoader` 扫描 `.pi/extensions/` 目录，找到 `tool-logger.ts`，动态 `import()` 它，然后调用 `export default function`。

**传入的 `pi` 对象**：就是 [03-extension-system](03-extension-system.md) 中描述的 `ExtensionAPI`。

---

## 2. 注册事件监听：记录工具调用

```typescript
export default function toolLoggerExtension(pi: ExtensionAPI) {
  // 存储每个 toolCallId 的开始时间
  const startTimes = new Map<string, number>();
  const logFile = join(homedir(), ".pi", "tool-log.jsonl");

  // ── 工具开始执行时：记录开始时间 ──
  pi.on("tool_execution_start", (event) => {
    // event = { type: "tool_execution_start", toolCallId, toolName, args }
    startTimes.set(event.toolCallId, Date.now());
  });

  // ── 工具执行结束时：计算耗时、写入文件 ──
  pi.on("tool_execution_end", (event) => {
    // event = { type: "tool_execution_end", toolCallId, toolName, result, isError }
    const startTime = startTimes.get(event.toolCallId);
    const duration = startTime ? Date.now() - startTime : -1;
    startTimes.delete(event.toolCallId);

    const logEntry = JSON.stringify({
      ts: new Date().toISOString(),
      tool: event.toolName,
      duration_ms: duration,
      isError: event.isError,
    });
    appendFileSync(logFile, logEntry + "\n");
  });
}
```

**发生了什么**：
- `pi.on("tool_execution_start", ...)` — 注册 handler。参考 [03-extension-system §3.4](03-extension-system.md#34-工具执行事件) 的事件表，此事件在 execute 阶段开始时触发。
- `pi.on("tool_execution_end", ...)` — 同一表中，此事件在 execute 完成时触发。
- 这些 handler **不返回任何值**——它们只做观察（副作用），不影响工具执行流程。

---

## 3. 注册一个 `/stats` 命令

```typescript
export default function toolLoggerExtension(pi: ExtensionAPI) {
  // ... (上面的事件监听代码)

  let totalCalls = 0;

  // 更新计数器
  pi.on("tool_execution_end", () => {
    totalCalls++;
  });

  // ── 注册命令 ──
  pi.registerCommand("stats", {
    description: "显示工具调用统计",

    handler: async (args: string, ctx) => {
      // args = "/stats" 后面的文本（如果有）
      // ctx = ExtensionCommandContext（见 [03-extension-system §5.1]）

      if (args.includes("--reset")) {
        totalCalls = 0;
        ctx.ui.notify("统计已重置");
      } else {
        ctx.ui.notify(`自启动以来共调用 ${totalCalls} 次工具`);
      }
    },
  });
}
```

**调用方式**：用户在 TUI 中输入 `/stats` 或 `/stats --reset`。

**Command vs input 事件**：
- `/stats` → `AgentSession._tryExecuteExtensionCommand("stats")` → 你的 handler 被调用。**不发往 Agent 循环**。
- 如果用户不带 `/` 前缀输入 `stats` → 通过 `input` 事件 → 可能被拦截或跳过。

---

## 4. 注册一个自定义工具：`ping`

扩展还可以注册 LLM 可调用的工具：

```typescript
import { Type } from "typebox";  // TypeBox 用于运行时参数校验

export default function toolLoggerExtension(pi: ExtensionAPI) {
  // ... (上面的代码)

  // ── 注册工具 ──
  pi.registerTool({
    name: "ping",
    label: "Ping",
    description: "一个简单的 ping 工具，返回 'pong'。没有参数。",
    parameters: Type.Object({}),  // 无参数 → 空对象 schema

    async execute(toolCallId, params, signal, onUpdate, ctx) {
      // params = {}（已被 TypeBox 校验）
      // signal = 当前 run 的 AbortSignal（如果用户按 Ctrl+C 则 abort）
      // onUpdate = 如果不为 undefined，调用它发送部分结果
      // ctx = ExtensionContext（见 [03-extension-system §3]）

      // 模拟长时间执行→发送部分结果
      if (onUpdate) {
        onUpdate({ content: [{ type: "text", text: "ping...\n" }], details: {} });
        await new Promise(resolve => setTimeout(resolve, 500));
      }

      // 返回最终结果
      return {
        content: [{ type: "text", text: "pong!" }],
        details: { latency: "500ms" },
      };
    },
  });
}
```

**这是什么效果**：
- 工具出现在 system prompt 的"可用工具"列表中
- LLM 可以在 `toolCalls` 中调用 `{ name: "ping", args: {} }`
- wrapRegisteredTools 自动包装 execute → 发射 tool_execution_start/update/end 事件
- 返回的 `{ content, details }` 被拼装为 toolResult message，发回 LLM

---

## 5. 修改 System Prompt

```typescript
export default function toolLoggerExtension(pi: ExtensionAPI) {
  // ... (上面的代码)

  // ── 在每次 prompt 发送给 Agent 前，注入额外指引 ──
  pi.on("before_agent_start", (event) => {
    // event = { type: "before_agent_start", prompt, images, systemPrompt, systemPromptOptions }

    const customInstructions = `
## 工具日志记录已启用

当前会话中所有工具调用都会记录到 ~/.pi/tool-log.jsonl。
使用 /stats 查看调用统计。
`;

    return {
      systemPrompt: event.systemPrompt + "\n\n" + customInstructions,
      // → 修改：本轮 LLM 看到的 system prompt 被扩展追加了内容
      // → 下一轮 agent.prepareNextTurnWithContext 会重置为 _baseSystemPrompt
      // → 所以这个 modification 只在本轮有效
    };
  });
}
```

---

## 6. 完整文件

```typescript
// .pi/extensions/tool-logger.ts
// 功能：记录所有工具调用 + /stats 命令 + ping 工具

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { appendFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export default function toolLoggerExtension(pi: ExtensionAPI) {
  const startTimes = new Map<string, number>();
  const logFile = join(homedir(), ".pi", "tool-log.jsonl");
  let totalCalls = 0;

  // ── 事件监听 ──
  pi.on("tool_execution_start", (event) => {
    startTimes.set(event.toolCallId, Date.now());
  });

  pi.on("tool_execution_end", (event) => {
    const startTime = startTimes.get(event.toolCallId);
    const duration = startTime ? Date.now() - startTime : -1;
    startTimes.delete(event.toolCallId);
    totalCalls++;

    appendFileSync(logFile, JSON.stringify({
      ts: new Date().toISOString(),
      tool: event.toolName,
      duration_ms: duration,
      isError: event.isError,
    }) + "\n");
  });

  // ── 命令 ──
  pi.registerCommand("stats", {
    description: "显示工具调用统计",
    handler: async (args, ctx) => {
      if (args.includes("--reset")) {
        totalCalls = 0;
        ctx.ui.notify("统计已重置");
      } else {
        ctx.ui.notify(`共调用 ${totalCalls} 次工具`);
      }
    },
  });

  // ── 工具 ──
  pi.registerTool({
    name: "ping",
    label: "Ping",
    description: "返回 'pong'。",
    parameters: Type.Object({}),
    async execute(toolCallId, params, signal, onUpdate) {
      return {
        content: [{ type: "text", text: "pong!" }],
        details: {},
      };
    },
  });

  // ── System Prompt ──
  pi.on("before_agent_start", (event) => {
    return {
      systemPrompt: event.systemPrompt + "\n\n## 工具日志记录已启用\n使用 /stats 查看。",
    };
  });
}
```

---

## 7. 这个模式与 UEMCPHarness 的对应

| 这个例子中的代码 | 对应 UEMCPHarness 概念 | 对应文档节 |
|---|---|---|
| `pi.on("tool_execution_start", ...)` | `ToolCallLogger.post_call()` 的开始时间记录部分 | [03 §3.4](03-extension-system.md#34-工具执行事件) |
| `pi.on("tool_execution_end", ...)` | `ToolCallLogger.post_call()` 的耗时计算 + 写入部分 | [03 §3.4](03-extension-system.md#34-工具执行事件) |
| `pi.registerTool(...)` | `HarnessTool(name, description, inputSchema, handler)` | [06 §1](06-tools-and-skills.md#1-tooldefinition-的结构) |
| `pi.registerCommand("stats", ...)` | 无直接等价 — Harness 没有 slash command 系统 | [03 §5](03-extension-system.md#5-command-系统) |
| `pi.on("before_agent_start", ...)` | `_build_instructions()` → `server.instructions` | [03 §3.1](03-extension-system.md#31-输入阶段事件) |
| `appendFileSync(logFile, ...)` | `ToolCallLogger` 的 JSONL 后台写入器 | [05 §4](05-session-manager.md#4-与-uemcpharness-的对应) |

---

## 8. 下一步

这个例子展示了扩展的四种核心模式：
1. **事件监听**（`pi.on`）— 观察 Agent 生命周期、做副作用
2. **工具注册**（`pi.registerTool`）— 给 LLM 提供新能力
3. **命令注册**（`pi.registerCommand`）— 给用户提供快捷操作
4. **System Prompt 注入**（`before_agent_start`）— 引导 LLM 行为

真实 UE Harness 迁移扩展将使用**这四种模式的全部**——但规模更大、逻辑更复杂。

返回 [00-overview](00-overview.md) 查看全局索引，或回到 [迁移进度存档](../plans/pi-migration-progress-2026-08-01.md) 继续迁移讨论。
