# 07 — CLI 入口与模式分发

**对应的源文件**：[packages/coding-agent/src/main.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/main.ts)、[core/sdk.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/sdk.ts)、[modes/interactive/interactive-mode.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/modes/interactive/interactive-mode.ts)、[modes/print-mode.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/modes/print-mode.ts)

**前置阅读**：全部 01-06 文档
**阅读目标**：理解 `main()` 如何组装所有组件、如何选择运行模式、createAgentSession() 的完整链路

---

## 全局地图：main() 在完整链路中的位置

```
                    ┌─────────────────────────────────────────┐
                    │  pi (命令) / pi-test.sh                 │
                    │  Node.js 进程启动                       │
                    └────────────────┬────────────────────────┘
                                     │
                                     ▼
╔══════════════════════════════════════════════════════════════╗
║  07-main-and-modes: main() ←── 本文档范围                   ║
║                                                             ║
║  1. 加载扩展（文件 + 内联）                                  ║
║  2. 解析 CLI 参数                                           ║
║  3. 选择模式：                                              ║
║     ├── RPC 模式 → 启动 PiServer                           ║
║     ├── Print 模式 → 单次运行 → 输出文本                    ║
║     └── Interactive 模式 → TUI（默认）                      ║
║                                                             ║
║  4. 创建 Session：                                          ║
║     createAgentSessionRuntime()                             ║
║       → createAgentSessionServices()                        ║
║       → createAgentSession()                                ║
║         → new Agent({ streamFn, convertToLlm, ... })        ║
║         → new AgentSession({ agent, sessionManager, ... })   ║
║       → new AgentSessionRuntime(session, services, ...)      ║
║                                                             ║
║  5. 进入模式 → 等待用户输入                                 ║
╚══════════════════════════════════════════════════════════════╝
```

**本文档覆盖**：从命令行启动到 AgentSession 就绪的全部流程。

---

## 调用上下文：main() 怎么被调用

```bash
# 终端用户：
$ pi "把 PointLight 改成红色"
$ pi --mode rpc --port 9000
$ pi                # 进入 TUI

# 编程调用：
import { main } from "@earendil-works/pi-coding-agent";
await main(process.argv.slice(2), { extensionFactories: [myExtension] });
```

---

## 1. main() 的完整流程

### 步骤 1：加载扩展 + 离线检测

```typescript
async function main(args: string[], options?: MainOptions): Promise<number> {
  // 合并扩展：内置 + 用户传入的
  const allExtensions = [
    ...BUILTIN_EXTENSIONS,      // llama.cpp 支持（隐藏）
    ...(options?.extensionFactories ?? []),
  ];

  // 检测离线模式
  const isOffline = args.includes("--offline") || process.env.PI_OFFLINE;
  // → 如果离线：跳过版本检查、跳过网络模型刷新
```

### 步骤 2：解析 CLI 参数

```typescript
  const parsed = parseArgs(args);
  // → 输入：命令行参数数组
  // → 返回：{ text, session, fork, resume, model, thinkingLevel, mode, ... }
  // → 如果有错误：exit(1)
```

### 步骤 3：处理特殊子命令（直接返回，不走 Agent）

```typescript
  // "pi update" / "pi install" / "pi list" 等包管理命令
  const handled = await handlePackageCommand(parsed);
  if (handled) return 0;

  // "pi auth" — 认证管理
  const authHandled = await runCredentialPrintCommand(parsed);
  if (authHandled) return 0;
```

### 步骤 4：选择运行模式

```typescript
  // 优先级：显式 --mode > --print / -p 标志 > 管道/stdin 检测 > TTY 检测
  let mode: AppMode;
  if (parsed.mode === "rpc") {
    mode = "rpc";
  } else if (parsed.print || !process.stdout.isTTY || pipedStdin) {
    mode = parsed.mode === "json" ? "json" : "print";
  } else {
    mode = "interactive";  // 默认 TUI
  }
```

### 步骤 5：处理 Session 文件选择

```typescript
  // --session <path> : 使用指定 session 文件
  // --resume : 恢复上次 session
  // --continue : 继续上次 session 并传入 stdin 内容
  // --fork <id> : 从特定 entry 分叉
  // --session-id <id> : 使用 session id 查找
  // --no-session : 纯内存模式（不写入文件）

  const sessionManager = resolveSession(parsed, cwd);
  // → 可能触发交互式 session 选择器（如果多个匹配）
```

### 步骤 6：Project Trust 检查

```typescript
  const trustStore = new ProjectTrustStore(agentDir);
  const trusted = await trustStore.resolve(cwd, { mode, hasUI, ui });
  // → 如果不是 interactive 模式且项目未信任：可能退出
  // → 如果是首次运行：弹出交互式确认
```

### 步骤 7：创建 AgentSessionRuntime

```typescript
  // 这是最关键的组装步骤（见第 2 节深度分析）
  const runtime = await createAgentSessionRuntime(factory, {
    cwd,
    agentDir,
    sessionManager,
    sessionStartEvent: { type: "session_start", reason: "startup" },
  });

  // runtime.session — AgentSession 实例
  // runtime.services — AgentSessionServices（modelRuntime, resourceLoader, ...）
  // runtime.diagnostics — 启动诊断信息（warnings / errors）
```

### 步骤 8：注入初始消息（如果有）

```typescript
  // --print "hello" → 直接作为第一条 prompt
  // stdin 内容 → 合并到第一条 prompt
  // 文件参数（@file.txt）→ 作为附加上下文
  const initialMessage = buildInitialMessage(parsed, pipedStdin);
```

### 步骤 9：进入模式

```typescript
  if (mode === "rpc") {
    await runRpcMode(runtime);  // 启动 PiServer
  } else if (mode === "interactive") {
    await new InteractiveMode(runtime, options).run();  // TUI
  } else {
    await runPrintMode(runtime, { initialMessage, mode });  // 单次运行
  }
```

---

## 2. createAgentSession() 的完整链路（深度级）

> **📍 当前位置**：步骤 7 的内部。这是所有组件被创建并连接在一起的时刻。理解这个链路才能理解"迁移的扩展应该在哪一步注入"。

### 2.1 createAgentSessionServices() — 创建基础设施

```typescript
const services = await createAgentSessionServices({
  cwd: "/project",
  agentDir: "/home/user/.pi/agent",
  modelRuntime: undefined,         // 不传入 → 自动创建
  settingsManager: undefined,      // 不传入 → 自动创建
  extensionFlagValues: parsed.extFlags,  // 扩展 "--flag" 的值
});
```

内部做的事情：
```
1. 创建 ModelRuntime
   → new ModelRuntime(authPath, modelsPath)
   → 读取 auth.json（API key / OAuth token）
   → 读取 models.json（模型目录缓存）

2. 创建 SettingsManager
   → SettingsManager.create(cwd, agentDir)
   → 读取 .pi/settings.json + 全局 settings.json

3. 创建 ResourceLoader
   → new DefaultResourceLoader({ cwd, agentDir, settingsManager })
   → 扫描 .pi/skills/*.md → Skill[]
   → 扫描 .pi/prompts/*.md → PromptTemplate[]
   → 扫描 AGENTS.md / CLAUDE.md → ContextFile[]
   → 扫描 .pi/extensions/*.ts → Extension[]

4. 注册扩展注册的 LLM provider
   → modelRuntime.registerProvider(name, config)
   → modelRuntime.registerNativeProvider(provider)

5. 刷新模型列表
   → modelRuntime.refresh({ allowNetwork: false })
```

**返回**：`{ cwd, agentDir, modelRuntime, settingsManager, resourceLoader, diagnostics }`。

### 2.2 createAgentSession() — 创建 AgentSession

```typescript
const { session, extensionsResult, modelFallbackMessage } = await createAgentSession({
  cwd: services.cwd,
  agentDir: services.agentDir,
  modelRuntime: services.modelRuntime,
  settingsManager: services.settingsManager,
  resourceLoader: services.resourceLoader,
  sessionManager,             // 上面的步骤 5 中确定
  model: parsed.model,        // --model 参数
  thinkingLevel: parsed.thinkingLevel,
  tools: ["read", "bash", "edit", "write"],  // --tools 参数
  scopedModels: parsed.models,               // --models 参数
  sessionStartEvent: { type: "session_start", reason: "startup" },
});
```

内部做的事情：
```
1. 确定初始模型：
   → 优先：parsed.model（--model 参数）
   → 其次：session 恢复的模型
   → 再次：settings 中的默认模型
   → 最后：第一个可用的模型

2. 确定初始 thinking level（同理，层层回落）

3. 确定初始活跃工具：
   → 优先：parsed.tools（--tools 参数）
   → 其次：["read", "bash", "edit", "write"]（默认）

4. 创建 Agent 实例：
   new Agent({
     streamFn: (model, ctx, opts) => {
       // 包装 modelRuntime.streamSimple：
       // - 加超时控制
       // - 加 retry 配置
       // - 加 extension header 转换（before_provider_headers 事件）
       return modelRuntime.streamSimple(model, ctx, processedOpts);
     },
     convertToLlm: (msgs) => {
       // 过滤 custom/UI 消息 + 可选的 image blocking
       return convertToLlm(msgs, blockImages);
     },
     transformContext: async (msgs, sig) => {
       // 发射 "context" 扩展事件（扩展可裁剪/注入消息）
       const ctxEvent = await extensionRunner.emit({ type: "context", messages: msgs });
       return ctxEvent?.messages ?? msgs;
     },
     onPayload: (payload) => {
       // 发射 "before_provider_request" 扩展事件
       extensionRunner.emit({ type: "before_provider_request", payload });
     },
     onResponse: (resp) => {
       // 发射 "after_provider_response" 扩展事件
       extensionRunner.emit({ type: "after_provider_response", ... });
     },
     sessionId: sessionManager.getSessionId(),
     steeringMode: settings.getSteeringMode(),
     followUpMode: settings.getFollowUpMode(),
     initialState: {
       systemPrompt: "...",  // 此时是空字符串，AgentSession 会覆盖
       model: resolvedModel,
       thinkingLevel: resolvedThinkingLevel,
       tools: [],
       messages: [],         // 空，AgentSession 会恢复
     },
   });

5. 恢复 session 消息（如果 sessionManager 有历史）：
   → agent.state.messages = sessionManager.buildSessionContext().messages

6. 创建 AgentSession 实例：
   new AgentSession({
     agent,
     sessionManager,
     settingsManager,
     resourceLoader,
     modelRuntime,
     cwd,
     initialActiveToolNames: ["read", "bash", "edit", "write"],
     customTools: parsed.customTools,
     baseToolsOverride: parsed.baseToolsOverride,
     sessionStartEvent: { type: "session_start", reason: "startup" },
   });
```

**AgentSession 构造函数内部做的**（回顾 02）：
1. `agent.subscribe(_handleAgentEvent)` → 事件→持久化→扩展事件映射
2. `_installAgentToolHooks()` → 覆盖 `agent.beforeToolCall` / `agent.afterToolCall`
3. `_installAgentNextTurnRefresh()` → 覆盖 `agent.prepareNextTurnWithContext`
4. `_buildRuntime()` → 创建 ExtensionRunner + 刷新工具注册表 + 重建 system prompt

**返回**：`{ session, extensionsResult, modelFallbackMessage }`。

### 2.3 createAgentSessionRuntime() — 包装为 Runtime

```typescript
const runtime = await createAgentSessionRuntime(factory, {
  cwd, agentDir, sessionManager,
  sessionStartEvent: { type: "session_start", reason: "startup" },
});
```

内部做的事情：
```
1. 调用 factory（即 createAgentSessionRuntimeFactory）：
   → createAgentSessionServices() + createAgentSession()

2. 包装为 AgentSessionRuntime：
   new AgentSessionRuntime(session, services, factory, diagnostics, fallbackMsg)
   → runtime.session → session
   → runtime.services → services
   → runtime.createRuntime → factory（供后续 new/fork 重用）
```

---

## 3. 三种运行模式

### 3.1 Interactive 模式（TUI）

```typescript
class InteractiveMode {
  constructor(private runtime: AgentSessionRuntime) {}

  async run(): Promise<void> {
    // 1. 初始化终端（alt screen）
    const tui = new TuiAltScreen({ ... });

    // 2. 构建组件树：
    //    Header → Chat → Footer
    //    Chat = TranscriptItems + Editor
    //    TranscriptItems 订阅 runtime.session.subscribe()
    //    每当新 event 到达 → 更新渲染

    // 3. 绑定扩展（设置 UI context）
    await runtime.session.bindExtensions({
      uiContext: tui,     // 扩展可以通过 ctx.ui.showDialog() 等
      mode: "tui",
      shutdownHandler: () => process.exit(0),
      abortHandler: () => runtime.session.abort(),
    });

    // 4. 启动输入循环
    while (true) {
      const input = await tui.readInput();
      if (input.startsWith("/command")) {
        await runtime.session.prompt(input);  // 扩展命令在此处理
      } else {
        await runtime.session.prompt(input);
      }
    }
  }
}
```

### 3.2 Print 模式（单次运行）

```typescript
async function runPrintMode(runtime, options): Promise<void> {
  // 1. 绑定扩展（在 "print" 模式下）
  await runtime.session.bindExtensions({ mode: "print" });

  // 2. 订阅事件
  let output = "";
  runtime.session.subscribe((event) => {
    if (event.type === "message_update") {
      // 流式输出到 stdout
    }
  });

  // 3. 发 prompt + 等待完成
  await runtime.session.prompt(options.initialMessage);
  await runtime.session.waitForIdle();

  // 4. 输出最终结果
  const lastAssistant = runtime.session.state.messages.findLast(m => m.role === "assistant");
  process.stdout.write(contentText(lastAssistant.content));

  // 5. 退出
  await runtime.dispose();
  process.exit(0);
}
```

### 3.3 RPC 模式

```typescript
async function runRpcMode(runtime): Promise<void> {
  // 创建 PiSessionBackend（把 AgentSession 包装为 PiSessionRuntime）
  const backend: PiSessionBackend = {
    listSessions: () => [...],
    listModels: () => runtime.services.modelRuntime.getAvailable(),
    createSession: async (opts) => {
      const session = await createAgentSession({ ...opts, modelRuntime, ... });
      return new RpcSessionRuntime(session);
    },
    openSession: async (id) => { /* restore */ },
  };

  // 启动 PiServer
  const server = new PiServer(backend, { token, listeners });
  await server.start();

  // RPC 客户端通过 stdin/stdout 的 JSON 流通信
  const rpc = new JsonRpcTransport(process.stdin, process.stdout);
  // ...
}
```

---

## 4. 对迁移的意义

**在哪一步注入 UE Harness 扩展？**

```typescript
// 方式 A：作为 extensionFactory 传入
await main(args, {
  extensionFactories: [ueHarnessExtension],  // ← 在这里
});

// 方式 B：放在 .pi/extensions/ue-harness.ts
// PiAgent 自动加载

// 方式 C：在 createAgentSession() 之后、进入模式之前
const runtime = await createAgentSessionRuntime(factory, options);
// 手动做额外配置：
runtime.services.modelRuntime.registerProvider("mimo", { ... });
await runtime.session.bindExtensions({ /* UE-specific UI */ });
```

**推荐方式 A/B** — 完全作为标准扩展。这样迁移后的代码与 PiAgent 的其他扩展没有区别，可以热重载（/reload），可以被其他扩展组合使用。

---

下一篇 [08-full-lifecycle](08-full-lifecycle.md) 将以一个具体的 UE 场景串联全部 01-07 的知识。
