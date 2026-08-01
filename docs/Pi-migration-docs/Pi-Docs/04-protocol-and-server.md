# 04 — 协议与服务器

**对应的源文件**：[packages/protocol/src/schemas.ts](d:/Programs/2024-2/pi/packages/protocol/src/schemas.ts)、[packages/server/src/server.ts](d:/Programs/2024-2/pi/packages/server/src/server.ts)、[packages/server/src/sessions.ts](d:/Programs/2024-2/pi/packages/server/src/sessions.ts)、[packages/client/src/client.ts](d:/Programs/2024-2/pi/packages/client/src/client.ts)

**前置阅读**：[00-overview](00-overview.md)、[02-agent-session](02-agent-session.md)
**阅读目标**：理解 PiAgent 的通信协议如何使用、PiServer 如何暴露 AgentSession、与 MCP 标准协议的关键区别

---

## 全局地图：协议与服务器在完整链路中的位置

```
┌─────────────────────────────────────────────────────────────────┐
│  进程 A: PiAgent TUI (本地)                                     │
│                                                                 │
│  main() → AgentSession → Agent.prompt() → LLM                   │
│                            ↑                                    │
│                            │ 直接调用（同进程）                  │
│                            │                                    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  进程 B: PiAgent Server 模式 (远程)                              │
│                                                                 │
│  启动: pi --mode rpc → PiServer.start()                        │
│                         │                                       │
│  ┌──────────────────────┼──────────────────────────────┐        │
│  │ PiServer             │ ← 本文档范围                  │        │
│  │                      │                               │        │
│  │ ├─ 连接握手 + 认证   │                               │        │
│  │ ├─ LiveSessionManager│← 管理活跃 session             │        │
│  │ │  ├─ create session  │                               │        │
│  │ │  ├─ prompt/steer    │← 所有操作通过 PiSessionRuntime│       │
│  │ │  └─ abort           │  接口，底层仍是 AgentSession  │       │
│  │ └─ Snapshot 广播      │                               │        │
│  └──────────────────────┼──────────────────────────────┘        │
│                         │                                       │
└─────────────────────────┼───────────────────────────────────────┘
                          │
                  ┌───────┴────────┐
                  │ PiClient       │  ← 远程客户端
                  │ (packages/     │
                  │  client)       │
                  └────────────────┘
```

**关键认知**：PiAgent 有两种使用方式——本地模式（AgentSession 直接运行在同一个 Node.js 进程中）和远程模式（通过 PiServer 暴露，远程客户端通过 PiClient 连接）。**本文档只讨论远程模式中协议和服务器的工作方式**。

---

## 调用上下文：PiServer 怎么被创建、怎么被调用

### 谁启动了 PiServer

```typescript
// 在 RPC 模式中：
const server = new PiServer(backend, {
  // 输入：
  token: "my-secret-token",        // 客户端认证 token（握手时校验 SHA-256）
  listeners: [unixListener],       // 传输层监听器（Unix Socket 或 TCP）
  serverId: "my-server",           // 可选，默认随机 UUID
  onError: (err) => console.error(err),
});

await server.start();
// → 修改：每个 listener 开始 accept 连接
// → 返回：this（Promise<PiServer>）
```

### PiSessionBackend — 谁提供了 PiSessionRuntime

```typescript
// backend 实现了 PiSessionBackend 接口
const backend: PiSessionBackend = {
  // 列出所有 session（用于客户端选择）
  listSessions: () => [...savedSessions],

  // 列出所有模型（用于客户端选择）
  listModels: () => modelRuntime.getAvailable(),

  // 创建新 session → 返回一个 PiSessionRuntime
  createSession: async (options) => {
    // options: { id: UUID (server 分配), cwd?, name?, model?, thinkingLevel? }
    const session = await createAgentSession({ ...options });
    return new RpcSessionRuntime(session);  // 包装 AgentSession
  },

  // 打开已有 session → 返回一个 PiSessionRuntime
  openSession: async (sessionId) => {
    const session = await restoreSession(sessionId);
    return new RpcSessionRuntime(session);
  },
};
```

**输入**：Server 分配的 session ID + 可选参数。
**返回**：`PiSessionRuntime` 实例。
**修改**：无——backend 只负责创建/恢复，不负责管理。

---

## 1. 协议栈：四个层次

```
┌─────────────────────────────────────────┐
│ 应用层: ServerMessage / ClientMessage    │  ← TypeBox 类型校验的消息
│          (session_snapshot, prompt,      │
│           steer, abort, ...)             │
├─────────────────────────────────────────┤
│ 编码层: CBOR                             │  ← 二进制编码（Compact Binary
│         (比 JSON 紧凑 ~40%)              │     Object Representation）
├─────────────────────────────────────────┤
│ 帧层:   4 字节 uint32 BE 长度前缀 +      │  ← 最大帧长 16 MiB (可配置)
│         负载                            │
├─────────────────────────────────────────┤
│ 传输层: Unix Domain Socket / TCP         │  ← Node.js net.Socket
└─────────────────────────────────────────┘
```

**与 MCP 标准协议的关键区别**：

| 维度 | MCP 标准 (JSON-RPC 2.0) | PiAgent 协议 |
|------|------------------------|-------------|
| **消息编码** | JSON 文本 | CBOR 二进制 |
| **帧定界** | 无（依赖 HTTP/SSE） | 4 字节 uint32 BE 长度前缀 |
| **传输层** | HTTP POST + SSE 长连接 | 原始 TCP / Unix Socket |
| **握手** | `initialize` RPC + `Mcp-Session-Id` header | 第一条消息必须是 `ClientHello`（含版本 + token） |
| **异步通知** | SSE event-stream | `EventEnvelope`（server 推送 session_snapshot / session_progress） |
| **请求-响应** | JSON-RPC 2.0 `{ jsonrpc, id, method, params }` | `RequestEnvelope`（type, id, request）→ `ResponseEnvelope`（type, id, ok, result/error） |
| **Session 管理** | 无（MCP 不定义 session） | 完整 session CRUD（create/attach/detach/list）+ 多连接附加到同一 session |

---

## 2. PiServer 的架构

### 2.1 连接状态机

```
客户端连接
  │
  ▼
awaitingHello ──► 接收第一条消息（必须是 hello）
  │
  ▼
handshaking ──► 认证 token → 检查版本 → 发送 ServerHello
  │
  ▼
ready ──► 正常通信（prompt/steer/abort 等操作）
  │
  ├── closing (协议错误或关闭)
  │     └── 发送 hello_error 或 close → 断开
  │
  └── closed (连接断开或超时)
```

### 2.2 消息路由

```
PiServer.receive(state, chunk)
  │
  ├── state.decoder.push(chunk)     ← 从字节流提取完整帧 + CBOR 解码
  │
  └── dispatchMessage(state, message)
        │
        ├── message.type == "hello" && stage == "awaitingHello"
        │   → finishHandshake(state, hello)
        │     → authenticate(hello)          ← SHA-256 + timingSafeEqual
        │     → isSupportedProtocolVersion()  ← 必须是 2
        │     → send ServerHello              ← 回复连接 ID + server snapshot
        │
        └── message.type == "request" && stage == "ready"
            → handleRequest(state, envelope)
              → sessions.executeCommand(state, envelope.request)
                → 成功 → send ResponseEnvelope { ok: true, result }
                → 失败 → send ResponseEnvelope { ok: false, error }
```

### 2.3 LiveSessionManager

```
executeCommand(connection, command)
  │
  ├── "list"    → return listSessions()
  ├── "create"  → backend.createSession({ id: UUID }) → 附加 connection → broadcast
  ├── "attach"  → backend.openSession(sessionId) → 附加 connection → broadcast
  ├── "detach"  → 移除 connection from session → maybeDispose
  ├── "prompt"  → requireAttached → session.runtime.prompt(text)
  ├── "steer"   → requireAttached → session.runtime.steer(text)
  ├── "abort"   → requireAttached → session.runtime.abort()
  ├── "set_model"    → requireAttached → session.runtime.setModel(model)
  └── "set_thinking" → requireAttached → session.runtime.setThinking(level)
```

**`requireAttached`**：验证发起请求的 connection 确实 attached 到目标 session。一个 client 可以 attached 到多个 session（比如用 `SessionLease` 同时打开多个 session handle）。一个 session 也可以被多个 client attached。

### 2.4 Snapshot 广播

```typescript
// 每次 session 状态变化 → broadcastSnapshot
// 每次 server 级变化（新 session 创建/销毁）→ snapshots.broadcast

broadcast():
  → revision++  (单调递增的版本号)
  → 对每个 attached connection:
    → send EventEnvelope { type: "event", event: { type: "session_snapshot", snapshot } }
  → 对每个 connected client:
    → send EventEnvelope { type: "event", event: { type: "server_snapshot", snapshot } }
```

**Snapshot 的内容**：session 的完整状态——ID / name / cwd / model / thinkingLevel / phase / transcript（消息历史）/ queuedSteer（排队中的消息）。客户端收到后更新本地 UI。

---

## 3. PiClient 的架构

### 3.1 连接 → 握手 → 操作

```typescript
// 客户端代码
const client = await PiClient.connect({
  transportFactory: () => createUnixTransport({ path: "/tmp/pi.sock" }),
  token: "my-secret-token",
});

// 创建 session
const handle = await client.createSession({ cwd: "/project" });
// → 返回 SessionLease（session 的操作句柄）

// 发送 prompt
const snapshot = await handle.prompt("Hello");
// → 返回 SessionSnapshot（含更新后的 transcript）

// 监听 session 状态变化
handle.onEvent((event) => {
  if (event.type === "session_progress") {
    // 流式 token 到达 → 更新 UI
  }
});

// 断开
await handle.dispose();
await client.dispose();
```

### 3.2 Session Lease 模型

```typescript
// 排他 lease：只有一个句柄可以操作此 session
const exclusiveHandle = await client.createSession({ cwd: "/project" });

// 共享 lease：多个句柄可以同时操作（R/O 或 按顺序操作）
const sharedHandle = await client.attachSession(sessionId, { mode: "shared" });

// lease 冲突：
// - 排他 lease 存在时 → 不能创建任何其他 lease
// - 存在任何 lease 时 → 不能创建排他 lease
```

### 3.3 请求-响应配对

```typescript
// PiClient.#request(command):
const id = nextId++;
const promise = createPromiseResolvers();
pendingRequests.set(id, promise);    // 保存 promise 等待响应
await connection.send({ type: "request", id, request: command });

// 当 handleMessage 收到 ResponseEnvelope:
const pending = pendingRequests.get(envelope.id);
if (envelope.ok) {
  pending.resolve(envelope.result);  // 解析 promise
} else {
  pending.reject(new PiServerError(envelope.error));
}
pendingRequests.delete(envelope.id);
```

---

## 4. PiSessionRuntime 接口

这是"把 AgentSession 暴露给远程客户端"的抽象接口：

```typescript
interface PiSessionRuntime {
  // 获取当前状态快照（包含完整 transcript + phase + model 等）
  snapshot(): SessionSnapshot;

  // 获取当前 phase（idle / turn / compaction / branch_summary / retry）
  getPhase(): SessionPhase;

  // 操作
  prompt(input: { text: string }): Promise<void>;   // 发送 prompt
  steer(input: { text: string }): Promise<void>;    // 发送 steering 指令
  abort(): Promise<void>;                           // 中止当前 run
  setModel(model: ModelRef): Promise<void>;         // 切换模型
  setThinking(level: ThinkingLevel): Promise<void>; // 切换思考级别

  // 事件订阅
  subscribe(listener: (event: PiSessionRuntimeEvent) => void): () => void;
  // event 类型：
  //   { type: "snapshot", snapshot }   — 状态变化
  //   { type: "progress", progress }   — 流式增量
  //   { type: "error", error }         — 错误

  // 销毁
  dispose(): Promise<void>;
}
```

**PiSessionRuntime 的实现**：在 RPC 模式中，它是一个包装了 `AgentSession` 的类：

```typescript
class RpcSessionRuntime implements PiSessionRuntime {
  constructor(private session: AgentSession) {
    // 订阅 AgentSession 的事件 → 转为 PiSessionRuntimeEvent 三种类型
    this.session.subscribe((event) => {
      if (event.type === "agent_end") {
        this.emit({ type: "snapshot", snapshot: this.buildSnapshot() });
      } else if (event.type === "message_update") {
        this.emit({ type: "progress", progress: this.buildProgress(event) });
      }
    });
  }

  async prompt(input: { text: string }): Promise<void> {
    await this.session.prompt(input.text, { expandPromptTemplates: false });
    //                                     ← RPC 模式下不展开 /command /skill
  }
}
```

**关键点**：底层的 Agent 循环、AgentSession 编排、扩展系统全部保持不变。PiSessionRuntime 只是在外面加了一层"远程协议适配"。

---

## 5. 对迁移的意义

### PiServer 能替代 UEMCPHarness 的 MCP Server (:9000) 吗？

**不能直接替代**。原因：

1. **协议不兼容**：PiServer 使用 CBOR + 长度帧，而 LLM (Claude Code) 期望的是 MCP JSON-RPC 2.0 over HTTP + SSE。如果要在 PiServer 上接收 Claude Code 的 `tools/list` / `tools/call` 请求，需要一个**协议转换层**。

2. **PiServer 的角色不同**：PiServer 是为"远程客户端控制 Agent"设计的，不是为"LLM 调用 MCP 工具"设计的。它的协议中的 `prompt` 是"请 Agent 处理这条消息"，不是"请执行这个工具"。

### 迁移策略

三个选项：

- **选项 A**：不碰 PiServer。UEMCPHarness 作为 PiAgent 扩展运行在本地模式（同进程），通过 `before_agent_start` 注入 system prompt、通过 `tool_call`/`tool_result` 事件做拦截。LLM 仍然通过 MCP JSON-RPC 连接 Harness Server。**PiServer 只用于可选的远程控制**。

- **选项 B**：在 PiServer 上追加一个 MCP 协议适配层——接收 JSON-RPC 请求，翻译为 PiSessionRuntime 操作。这需要一个双向协议转换器。

- **选项 C**：用 PiServer 完全替代 MCP Server。LLM 不通过 MCP 连接，而是通过 PiClient 连接 PiServer。这需要 LLM 端支持 PiAgent 协议。

选项 A 是最务实的方案——保持 MCP 协议层不变，PiAgent 的扩展系统作为"中间件"插入。

---

下一篇 [05-session-manager](05-session-manager.md) 将解析 SessionManager —— 消息如何持久化到 JSONL 文件、如何恢复、如何实现分支和时间旅行。
