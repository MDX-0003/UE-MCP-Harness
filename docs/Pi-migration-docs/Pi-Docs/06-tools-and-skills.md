# 06 — 工具与技能系统 + streamSimple() 深读

**对应的源文件**：[packages/coding-agent/src/core/tools/](d:/Programs/2024-2/pi/packages/coding-agent/src/core/tools/)、[packages/coding-agent/src/core/skills.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/skills.ts)、[packages/ai/src/](d:/Programs/2024-2/pi/packages/ai/src/)

**前置阅读**：[00-overview](00-overview.md)、[03-extension-system](03-extension-system.md)
**阅读目标**：理解工具如何定义、Skill 如何注入 system prompt、streamSimple() 如何直接调用 LLM

---

## 全局地图：工具与技能在完整链路中的位置

```
┌──────────────────────────────────────────────────────────────┐
│  启动时: ResourceLoader 扫描文件                              │
│                                                              │
│  .pi/skills/*.md        → Skill[]                            │
│  .pi/prompts/*.md       → PromptTemplate[]                   │
│  AGENTS.md / CLAUDE.md  → ContextFile[]                      │
│  .pi/extensions/*.ts    → Extension[]                        │
│                         │                                    │
│                         ▼                                    │
│  ╔══════════════════════════════════════════════════╗        │
│  ║  AgentSession._rebuildSystemPrompt()            ║        │
│  ║                                                 ║        │
│  ║  将 Skill 内容 + Prompt snippets + Guidelines   ║        │
│  ║  编译为 LLM 可理解的 system prompt               ║        │
│  ╚══════════════════════════════════════════════════╝        │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│  运行时: Agent 循环中的工具执行                               │
│                                                              │
│  LLM 返回 toolCall { name: "read", input: { filePath: ... } }│
│    │                                                         │
│    ▼                                                         │
│  Agent 循环 → 查找 AgentTool → execute() → 返回结果          │
│                                                              │
│  ToolDefinition (扩展注册的)                                  │
│    → wrapRegisteredTools() → AgentTool → Agent.state.tools   │
└──────────────────────────────────────────────────────────────┘
```

**本文档覆盖**：工具定义的结构、Skill 系统、streamSimple() —— 从零开始的 LLM 调用方法。

---

## 1. ToolDefinition 的结构

### 1.1 扩展注册工具时使用的接口

```typescript
interface ToolDefinition<TParams extends TSchema, TDetails = unknown, TState = any> {
  // ── 元数据 ──
  name: string;              // 工具名（LLM 在 toolCalls 中使用）
  label: string;             // 人类可读标签（TUI 显示用）
  description: string;       // 工具描述（发给 LLM 的 function description）

  // ── System Prompt 注入 ──
  promptSnippet?: string;    // 一句话摘要，出现在 system prompt 的"可用工具"部分
                             // 如果留空，此工具不会出现在工具的 prompt 列表中
  promptGuidelines?: string[]; // 工具使用指南，追加到 system prompt 的 Guidelines 节

  // ── 参数 Schema ──
  parameters: TParams;       // TypeBox schema（运行时类型校验）

  // ── 可选 ──
  prepareArguments?: (args: unknown) => Static<TParams>;
                             // 兼容垫片：LLM 可能传了旧格式 args → 转为新格式
  executionMode?: "sequential" | "parallel";  // 工具是串行还是并行执行
  constrainedSampling?: false | ConstrainedSamplingConfig;
                             // 可选的结构化输出约束（仅某些 provider 支持）
  renderShell?: "default" | "self";  // TUI 渲染选项（迁移不涉及）

  // ── 核心：执行函数 ──
  execute(
    toolCallId: string,      // 唯一 ID
    params: Static<TParams>, // 已验证的参数
    signal: AbortSignal | undefined,  // 中止信号
    onUpdate: AgentToolUpdateCallback<TDetails> | undefined,  // 部分结果回调
    ctx: ExtensionContext,   // 扩展上下文
  ): Promise<AgentToolResult<TDetails>>;

  // ── TUI 渲染（迁移不涉及）──
  renderCall?: (args, theme, context) => Component;
  renderResult?: (result, options, theme, context) => Component;
}
```

### 1.2 AgentTool（Agent 循环使用的工具格式）

`ToolDefinition` 通过 `wrapRegisteredTools()` 转换为 `AgentTool`：

```typescript
interface AgentTool<TParameters extends TSchema, TDetails = any> {
  name: string;
  label: string;
  description: string;
  parameters: TParameters;
  prepareArguments?: (args: unknown) => Static<TParameters>;
  execute: (
    toolCallId: string,
    params: Static<TParameters>,
    signal?: AbortSignal,
    onUpdate?: AgentToolUpdateCallback<TDetails>,
  ) => Promise<AgentToolResult<TDetails>>;
  executionMode?: "sequential" | "parallel";
}
```

**ToolDefinition 有而 AgentTool 没有的字段**（这些是扩展系统特有的）：
- `promptSnippet` / `promptGuidelines` — 只在 `_rebuildSystemPrompt()` 时用到
- `renderCall` / `renderResult` — TUI 渲染用
- `constrainedSampling` — Provider 配置用
- `execute` 多一个 `ctx: ExtensionContext` 参数（wrap 函数把这个参数去掉，因为 Agent 循环不提供 ExtensionContext）

### 1.3 内置工具清单

| 工具名 | 输入参数 | 功能 | Details 类型 |
|--------|---------|------|-------------|
| `read` | `{ filePath: string, offset?: number, limit?: number }` | 读取文件内容 | `ReadToolDetails` — path, lineCount, bytes |
| `write` | `{ filePath: string, content: string }` | 写入文件 | `undefined` |
| `edit` | `{ filePath, oldString, newString, replaceAll? }` | 文件中替换字符串 | 差异统计 |
| `bash` | `{ command: string, workdir?: string, timeout?: number }` | 执行 shell 命令（带超时 + 目录限制） | stdout, stderr, exitCode, killed |
| `grep` | `{ pattern: string, path?: string, glob?: string }` | 正则搜索文件 | 匹配行列表 |
| `find` | `{ pattern: string, path?: string }` | 按通配符找文件 | 匹配文件路径列表 |
| `ls` | `{ path?: string }` | 列出目录内容 | 文件/目录列表 |

---

## 2. Skill 系统（深度级）

### 2.1 Skill 文件格式

Skill 是 `.md` 文件，存储在 `.pi/skills/` 或 `{agentDir}/skills/` 中：

```markdown
---
name: match-atmosphere
description: 根据参考截图调整灯光氛围
triggers:
  - 氛围匹配
  - atmosphere
  - 参考图
---
# 灯光调整 SOP

## 步骤 1：基本参数对齐

1. 使用 get_all_light_actors 列出场景中所有灯光
2. 对每个灯光，使用 get_properties 获取 LightColor 和 Intensity

## 步骤 2：截图对比

...
```

- **Frontmatter**（`---` 之间）：YAML 格式，包含 `name`（必需）、`description`（必需）、`triggers`（匹配关键词）
- **Body**（`---` 之后）：Markdown 格式，完整的技能指南

### 2.2 Skill 如何被加载

```typescript
// ResourceLoader 在启动时扫描
const skillsDir = join(cwd, ".pi", "skills");
const userSkillsDir = join(agentDir, "skills");
const skillFiles = [...findMdFiles(skillsDir), ...findMdFiles(userSkillsDir)];

const skills = skillFiles.map(file => {
  const content = readFileSync(file, "utf-8");
  const { frontmatter, body } = parseFrontmatter(content);
  return {
    name: frontmatter.name,
    description: frontmatter.description,
    triggers: frontmatter.triggers ?? [],
    content: body,
    filePath: file,
    baseDir: dirname(file),       // Skill 文件中相对路径的基准目录
    sourceInfo: { filePath: file, source: "skill" },
  };
});
```

### 2.3 Skill 如何注入 System Prompt

`AgentSession._rebuildSystemPrompt()` 在构建 system prompt 时包含所有 skills：

```typescript
const skills = this._resourceLoader.getSkills().skills;
const skillList = skills.map(s => `- ${s.name}: ${s.description}`).join("\n");

const systemPrompt = `
You are a coding agent...

## Available Skills
${skillList}

When appropriate, use /skill:<name> to activate a skill.
`;
```

**Skill 的内容本身不直接注入 system prompt**——只有名称和描述出现在 prompt 中。完整内容在用户输入 `/skill:name` 时由 `_expandSkillCommand()` 展开。

### 2.4 /skill:name 的展开流程（回顾）

如 [02-agent-session](02-agent-session.md) 步骤 3 所述：

```
用户输入 "/skill:match-atmosphere 把 PointLight 改成红色"
  │
  ▼
AgentSession._expandSkillCommand()
  → 从 resourceLoader 中查找 name == "match-atmosphere" 的 skill
  → readFileSync(skill.filePath)
  → stripFrontmatter(content) → 去掉 --- 标记（但保留 YAML 字段值？不保留）
  → 包装:
    <skill name="match-atmosphere" location="/path/to/skill.md">
    内容基于 /path/to/skill.md 所在目录相对路径

    {skill.body}
    </skill>

    把 PointLight 改成红色
  → 返回包装后的文本
```

### 2.5 与 UEMCPHarness SkillRegistry 的对应

| UEMCPHarness SkillRegistry | PiAgent 等价 |
|---|---|
| 文件格式 | YAML（`name/description/triggers/steps/tools_allowlist`）→ YAML frontmatter + Markdown body |
| 加载 | `SkillRegistry.load_skills()` → 扫描 `~/.ue-harness/skills/*.yaml` → `ResourceLoader` 扫描 `.pi/skills/*.md` |
| 激活方式 | `activate_skill("match-atmosphere")` — HarnessTool handler → `/skill:match-atmosphere` — 扩展命令格式 |
| 注入 LLM 上下文 | `assemble_system_prompt()` → Skill steps 作为 Tier 2 Context → `_rebuildSystemPrompt()` → Skill 名和描述出现在 system prompt |
| 工具限制 | `ctx_filter_tools()` 按 `tools_allowlist` 过滤 → `_refreshToolRegistry()` 按 allow/deny list 过滤 |
| 内置 Skill | evening-lighting + scene-verification → 无内置（靠文件扩展） |

**关键区别**：UEMCPHarness 的 skill 激活后会通过 `ctx_filter_tools()` **限制可用工具列表**（只暴露 skill 需要的工具）。PiAgent 的 skill 没有这个机制——`/skill:name` 只是展开文本，不改变工具列表。如果需要类似行为，可以在扩展中监听 `/skill:name` 并调用 `ctx.setActiveTools(...)`。

---

## 3. Prompt Template 系统

Prompt 模板类似于 Skill，但用于更简单的文本展开。存储在 `.pi/prompts/*.md`。

```markdown
---
name: report
description: 生成项目报告
---
请分析项目 {{directory}} 并生成一份报告，包含：
1. 文件结构
2. 依赖分析
3. 代码质量评估
```

用户输入 `/report directory=src` → 展开：`请分析项目 src 并生成一份报告...`

变量替换：`{{variableName}}` → 命令参数中的值。

---

## 4. Context Files（AGENTS.md / CLAUDE.md）

这些文件在 `ResourceLoader` 加载时被自动读取，它们的内容直接嵌入 system prompt 的专门部分：

```
## Project Instructions (from AGENTS.md)

{AGENTS.md 内容}
```

**加载顺序**：项目目录 → 父目录逐级向上 → agentDir（~/.pi/）。nearer files 覆盖 farther ones。这允许项目级 AGENTS.md 覆盖全局设置。

---

## 5. streamSimple() 深读（深度级——从零概念出发）

> **📍 当前位置**：streamSimple() 不在 prompt() 链路中——它是 LLM 调用的**底层原语**。Agent 循环通过它发请求，Vision 验证也可以通过它独立发请求。理解它才能理解"如何在 PiAgent 中做 Vision 分析"。

### 5.1 它是什么、在哪里定义

`streamSimple` 是 `@earendil-works/pi-ai` 包的导出函数。在 `ModelRuntime` 中有一个对应的 `modelRuntime.streamSimple()`。

**注意**：`streamSimple` 是 `ModelRuntime` 的**静态方法**，不是实例方法。实际调用中，Agent 使用的是传入的 `streamFn` 回调：

```typescript
// 在 createAgentSession() 中
const agent = new Agent({
  streamFn: (model, context, options) => {
    // 这里包装了 modelRuntime.streamSimple，加了超时、headers 等
    return modelRuntime.streamSimple(model, context, {
      ...options,
      timeout: settings.timeout,
    });
  },
});
```

### 5.2 完整签名

```typescript
function streamSimple<TApi extends Api>(
  model: Model<TApi>,           // 见 5.3
  context: Context,             // 见 5.4
  options?: SimpleStreamOptions // 见 5.5
): Promise<AssistantMessageEventStream>;  // 见 5.6
```

### 5.3 参数 1：Model\<Api\>

`Model` 不是字符串——它是一个完整的模型描述对象。关键字段：

```typescript
interface Model<TApi extends Api = Api> {
  id: string;              // 模型 ID，如 "claude-sonnet-5-20251001"
  name: string;            // 显示名，如 "Claude 4 Sonnet"
  provider: string;        // provider 名，如 "anthropic"
  api: TApi;               // API 类型，如 "anthropic-messages" | "openai-responses" | ...
  baseUrl: string;         // API 端点 URL
  reasoning: boolean;      // 是否支持 thinking/reasoning
  input: ("text" | "image")[];  // 支持的输入模态
  cost: { input: number; output: number; cacheRead: number; cacheWrite: number };
  contextWindow: number;   // 最大上下文 window（tokens）
  maxTokens: number;       // 最大输出 tokens
  thinkingLevelMap?: Record<string, string | null>;  // thinking level 映射
}
```

对于 Vision 验证：需要确保 model.input 包含 `"image"`，且使用支持图像理解的 API（如 `"anthropic-messages"`）。

### 5.4 参数 2：Context

```typescript
interface Context {
  systemPrompt: string;     // 系统提示词
  messages: Message[];      // 对话历史。注意这里是 Message[]（LLM 原生格式），
                            // 不是 AgentMessage[]
  tools: Tool[];            // 可用工具列表（TypeBox schema 格式）
}
```

**关键**：`Context` 中的 `tools` 不是 `AgentTool[]`——它是基础的 `Tool[]` 格式（只有 name / description / inputSchema）。Agent 循环通过 `convertToLlm` 把 `AgentMessage[]` 转为 `Message[]`，tools 也从 `AgentTool[]` 转为 `Tool[]`。

### 5.5 参数 3：SimpleStreamOptions

```typescript
interface SimpleStreamOptions {
  apiKey?: string;                          // API key（覆盖环境变量）
  headers?: Record<string, string | null>;  // 额外 HTTP headers
  signal?: AbortSignal;                     // 中止信号
  thinkingLevel?: string;                   // 思考级别
  thinkingBudgets?: {                       // Token 预算
    low?: number;
    medium?: number;
    high?: number;
  };
  maxTokens?: number;                       // 最大输出 tokens
  transport?: "auto" | "http" | ...;        // 传输方式
  onPayload?: (payload, model) => void;     // 观察发出去的原始 payload
  onResponse?: (response, model) => void;   // 观察收到的响应头
  sessionId?: string;                       // 转发给 provider 的 session ID
  maxRetryDelayMs?: number;                 // 最大重试延迟
}
```

### 5.6 返回值：AssistantMessageEventStream

```typescript
type AssistantMessageEventStream = AsyncIterable<AssistantMessageEvent>;
```

`AssistantMessageEvent` 是一个 discriminated union。消费方式：

```typescript
const stream = await streamSimple(model, context, options);

for await (const event of stream) {
  switch (event.type) {
    case "message_start":
      // 第一个事件。event.message 是初始的空 assistant message
      console.log("LLM 开始回复");
      break;

    case "content_block_start":
      // 一个新的内容块开始（text block / toolCall block / thinking block）
      // event.contentBlock.type: "text" | "toolCall" | "thinking"
      break;

    case "content_block_delta":
      // 流式增量（每次收到一个 token）
      if (event.delta.type === "text_delta") {
        console.log(event.delta.text);  // 追加一个 token
      } else if (event.delta.type === "input_json_delta") {
        // toolCall 参数增量
      }
      break;

    case "content_block_stop":
      // 一个内容块结束
      break;

    case "message_delta":
      // 消息级增量（usage 更新等）
      console.log(event.delta.usage);  // 累计用量
      break;

    case "message_stop":
      // 最终事件。event.message 是完整的 assistant message
      // 包含 stopReason / usage / content / model / provider
      return event.message;  // 这就是最终结果
  }
}
```

### 5.7 使用示例：独立发一次 Vision 请求

```typescript
import { streamSimple } from "@earendil-works/pi-ai";

async function visionCheck(screenshotBase64: string, question: string) {
  const model = await selectVisionModel();  // 获取支持 image 的模型

  const messages: Message[] = [{
    role: "user",
    content: [
      {
        type: "image",
        source: {
          type: "base64",
          media_type: "image/png",
          data: screenshotBase64,
        },
      },
      { type: "text", text: question },
    ],
  }];

  const context: Context = {
    systemPrompt: "你是一个游戏截图分析助手。分析截图并回答用户问题。",
    messages,
    tools: [],  // Vision 验证不需要工具
  };

  const options: SimpleStreamOptions = {
    apiKey: process.env.VISION_API_KEY,  // Vision 专用的 API key
    maxTokens: 1024,
  };

  // 不需要 Agent 循环，直接调 LLM
  const stream = await streamSimple(model, context, options);
  let fullText = "";

  for await (const event of stream) {
    if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
      fullText += event.delta.text;
    }
  }

  return fullText;
}
```

### 5.8 streamSimple vs Agent 使用的区别

| 维度 | Agent 循环中使用 | Vision 独立调用 |
|------|-----------------|----------------|
| **谁调用** | `streamAssistantResponse()` → `streamFn()` | 扩展内部直接调 `streamSimple()` |
| **messages 来源** | `agent.state.messages` + `convertToLlm` 过滤 | 手动构造 `Message[]` |
| **tools** | `agent.state.tools`（全部可用工具） | 空数组或自定义工具 |
| **结果流向** | 进入 Agent 循环 → 提取 toolCalls → 继续循环 | 由调用方自行处理文本 |
| **systemPrompt** | 从 AgentState 继承 | 自行指定 |
| **是否影响对话历史** | 是（assistant message 写入 agent.state.messages） | 否（完全不涉及 AgentSession） |

### 5.9 对迁移的意义

**Vision 验证**：直接使用 `streamSimple()` 做独立 LLM 调用。不经过 Agent 循环（不会污染对话历史），可以指定不同的模型和 API key。

**多 Provider 支持**：如果 Vision 验证要用 MiMo API，通过 `modelRuntime.registerProvider("mimo", {...})` 注册后，用 MiMo 的 model 调 `streamSimple()` 即可。

---

## 6. 与 UEMCPHarness Skill 系统的最终对照

| 功能 | UEMCPHarness | PiAgent | 迁移策略 |
|------|-------------|---------|---------|
| Skill 存储 | YAML 文件 | Markdown + YAML frontmatter | 转换格式或用扩展加载 YAML |
| Skill 激活 | HarnessTool `activate_skill()` | 用户输入 `/skill:name` | 注册一个 `activate_skill` 命令或用 input 事件拦截 |
| 工具限制 | `ctx_filter_tools` + `tools_allowlist` | `_refreshToolRegistry` + allow/deny list | 在扩展中调用 `setActiveTools()` |
| System Prompt 注入 | `assemble_system_prompt()` | `before_agent_start` 事件 | `pi.on("before_agent_start", ...)` |
| Skill Steps 注入 | Tier 2 ContextProvider | Skill body 通过 XML 块 inline 注入 | 同上，在 `before_agent_start` 中展开 |

---

下一篇 [07-main-and-modes](07-main-and-modes.md) 将展示 CLI 入口——所有组件怎么在 `main()` 中被组装起来。
