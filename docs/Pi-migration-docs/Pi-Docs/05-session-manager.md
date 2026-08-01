# 05 — SessionManager：会话持久化

**对应的源文件**：[packages/coding-agent/src/core/session-manager.ts](d:/Programs/2024-2/pi/packages/coding-agent/src/core/session-manager.ts)

**前置阅读**：[00-overview](00-overview.md)、[02-agent-session](02-agent-session.md)
**阅读目标**：理解会话如何持久化为 JSONL 文件、如何恢复、分支树如何工作、与 UEMCPHarness 的 ToolCallLogger 的对应关系

---

## 全局地图：SessionManager 在完整链路中的位置

```
┌─────────────────────────────────────────────────────────────┐
│  AgentSession._handleAgentEvent()                           │
│                                                             │
│  Agent 循环发射 message_end 事件                             │
│    │                                                        │
│    ▼                                                        │
│  ╔══════════════════════════════════════════════════╗       │
│  ║  SessionManager ←── 本文档范围                   ║       │
│  ║                                                 ║       │
│  ║  appendMessage(event.message)                    ║       │
│  ║    → 写入 JSONL 文件（一行一个 JSON）             ║       │
│  ║                                                 ║       │
│  ║  appendCustomMessageEntry(...)                   ║       │
│  ║    → 写入自定义消息（扩展发送的）                 ║       │
│  ║                                                 ║       │
│  ║  appendCompaction(summary, ...)                  ║       │
│  ║    → 写入压缩摘要 + 标记压缩边界                 ║       │
│  ║                                                 ║       │
│  ║  appendModelChange(...)                          ║       │
│  ║  appendThinkingLevelChange(...)                  ║       │
│  ╚══════════════════════════════════════════════════╝       │
│                                                             │
│  Compaction / Session 恢复时：                              │
│                                                             │
│  ╔══════════════════════════════════════════════════╗       │
│  ║  buildSessionContext()                           ║       │
│  ║    → 读取 JSONL → 解码 entries → 重建 messages  ║       │
│  ║    → 赋值给 agent.state.messages                 ║       │
│  ╚══════════════════════════════════════════════════╝       │
└─────────────────────────────────────────────────────────────┘
```

**本文档覆盖**：SessionManager 的全部内容——存储格式、分支模型、消息恢复、与 UEMCPHarness 的对应。

---

## 调用上下文：SessionManager 怎么被创建、怎么被调用

### 谁创建了 SessionManager

```typescript
// 在 main() 启动流程中：
const sessionManager = SessionManager.create(cwd, sessionDir);
// → 输入：工作目录 + session 文件的存储目录
// → 返回：SessionManager 实例
// → 修改：如果没有 session 文件，创建一个新的空 JSONL 文件

// 或者恢复已有 session：
const sessionManager = SessionManager.open(sessionPath, sessionDir);
// → 输入：session 文件的完整路径
// → 返回：SessionManager 实例
// → 修改：无（只读已有文件）

// 或者纯内存模式（--no-session）：
const sessionManager = SessionManager.inMemory(cwd);
// → 返回：SessionManager 实例（所有 entries 只在内存中，不写磁盘）
```

**SessionManager 被注入到 AgentSession 的构造参数中**。

### SessionManager 何时被写入

在 `AgentSession._handleAgentEvent()` 中，当事件类型为 `message_end` 时：

```typescript
// message_end → 写入磁盘
if (event.message.role === "user" ||
    event.message.role === "assistant" ||
    event.message.role === "toolResult") {
  this.sessionManager.appendMessage(event.message);
  // → 修改：JSONL 文件追加一行
  // → 返回：新 entry 的 ID
}

// 如果是 custom message（扩展发送的）：
if (event.message.role === "custom") {
  this.sessionManager.appendCustomMessageEntry(
    event.message.customType, event.message.content,
    event.message.display, event.message.details,
  );
}
```

---

## 1. Session 存储模型

### 1.1 JSONL 文件格式

Session 文件是一个 **JSONL** 文件（JSON Lines）——每一行是一个独立的 JSON 对象，代表一个 `SessionEntry`。

```jsonl
{"type":"header","version":5,"cwd":"/project","created":"2026-07-01T00:00:00Z","updated":"2026-07-01T00:05:00Z","leafId":"entry-005"}
{"type":"message","id":"entry-001","parentId":null,"message":{"role":"user","content":[{"type":"text","text":"Hello"}],"timestamp":1719792000000}}
{"type":"message","id":"entry-002","parentId":"entry-001","message":{"role":"assistant","content":[{"type":"text","text":"Hi!"}],"provider":"anthropic","model":"claude-sonnet-5","usage":{"input":100,"output":50},"stopReason":"stop","timestamp":1719792010000}}
{"type":"message","id":"entry-003","parentId":"entry-002","message":{"role":"user","content":[{"type":"text","text":"Read package.json"}],"timestamp":1719792030000}}
{"type":"message","id":"entry-004","parentId":"entry-003","message":{"role":"assistant","content":[{"type":"toolCall","id":"tc1","name":"read","input":{"filePath":"package.json"}}],"stopReason":"toolUse","timestamp":1719792040000}}
{"type":"message","id":"entry-005","parentId":"entry-004","message":{"role":"toolResult","toolCallId":"tc1","content":[{"type":"text","text":"{\"name\":\"my-app\"}"}],"timestamp":1719792050000}}
```

**关键字段**：
- `header` — 文件的第一行，包含 session 元数据和 `leafId`（指向当前分支的最新 entry ID）
- `parentId` — 每个 entry 的"从哪里来"，形成**链表**（多条消息共享同一个 parent 则形成**分支**）
- `id` — 每个 entry 的唯一标识，UUID 格式
- `leafId` — 在 header 中，指向当前正在活跃的分支末端 entry

### 1.2 SessionEntry 类型

| 类型 | 内容 | 何时写入 | 是否进入 LLM 上下文 |
|------|------|---------|-------------------|
| `message` | 一条完整的 user / assistant / toolResult 消息 | 每次 `message_end` 事件 | **是** |
| `compaction` | 压缩摘要 + `firstKeptEntryId`（从哪个 entry 开始保留原文） | 每次 compaction 完成 | **摘要部分是，原文不再进入** |
| `branchSummary` | 分支摘要（从 session 树的某个节点导航过来时生成） | 每次 `navigateTree()` | **是** |
| `custom` | 扩展定义的自定义消息（如系统通知） | 扩展调 `pi.sendMessage()` | **类别特定** |
| `modelChange` | 模型切换记录（provider + model id） | 每次 `setModel()` / `cycleModel()` | 否 |
| `thinkingLevelChange` | 思考级别切换记录 | 每次 `setThinkingLevel()` | 否 |
| `labelChange` | entry 标签变更 | 扩展调 `pi.setLabel()` | 否 |

---

## 2. 分支 / 树结构（深度级）

### 2.1 parentId 链表 → parentId 树

```
entry-001 (user: "Hello")
  └── entry-002 (assistant: "Hi!")
        ├── entry-003 (user: "Read package.json")  ← 分支 A
        │     └── entry-004 (assistant: toolCall)
        │           └── entry-005 (toolResult: ...)
        │
        └── entry-006 (user: "Write a test")      ← 分支 B（从 entry-002 分叉）
              └── entry-007 (assistant: ...)
```

如果 `entry-003` 已存在，用户对 `entry-002`（assistant "Hi!"）做了 fork，创建了 `entry-006`（parentId = `entry-002`），这就形成了一个分支。

### 2.2 leafId 和活跃分支

`header.leafId` 指向"当前活跃的分支末端 entry"。`buildSessionContext()` 只读取从根到 `leafId` 路径上的消息。

### 2.3 createBranchedSession()

```typescript
// 用户输入 /fork entry-002，创建一个新分支
sessionManager.createBranchedSession("entry-002");
// → 修改：更改 header.leafId 指向 entry-002
// → 效果：下次用户发消息时，新的 entry 的 parentId = entry-002
//         entry-003/004/005 不再在当前活跃分支上
```

不会删除旧分支——`entry-003/004/005` 仍然在文件中。如果用户切回去（`navigateTree("entry-005")`），leafId 又指回 `entry-005`。

### 2.4 navigateTree()

```typescript
navigateTree(targetId, options);
// → 输入：目标 entry ID + 可选选项
// → 修改：header.leafId = targetId
// → 可选：生成 branchSummary（总结从旧分支到新分支之间丢弃的消息）
// → 返回：{ cancelled: boolean }
```

---

## 3. buildSessionContext()（深度级）

这是"从 JSONL 文件重建 agent.state.messages"的核心方法。

```typescript
buildSessionContext(): { messages: AgentMessage[] } {
  const entries = this.getEntries();  // 从文件读取所有 entries
  const path = this.getPathToLeaf();  // 沿着 parentId 链走到 leafId

  // 找到最近的 compaction entry
  const latestCompaction = findLatestCompaction(path);

  const messages: AgentMessage[] = [];

  if (latestCompaction) {
    // 压缩边界之前 → 用摘要文本代替全部旧消息
    messages.push({
      role: "user",  // 摘要作为一条 user message 注入
      content: [{ type: "text", text: latestCompaction.summary }],
      timestamp: Date.now(),
    });

    // 压缩边界之后（firstKeptEntryId 之后）→ 保留原始消息
    const postCompactionPath = path.slice(
      path.findIndex(e => e.id === latestCompaction.firstKeptEntryId)
    );
    for (const entry of postCompactionPath) {
      if (entry.type === "message") {
        messages.push(entry.message);
      } else if (entry.type === "branchSummary") {
        // 分支摘要也作为一条消息注入
        messages.push(branchSummaryToMessage(entry));
      }
    }
  } else {
    // 没有压缩 → 所有 message entries 直接作为消息
    for (const entry of path) {
      if (entry.type === "message") {
        messages.push(entry.message);
      }
    }
  }

  return { messages };
}
```

**返回**：`{ messages: AgentMessage[] }`。这个数组被赋给 `agent.state.messages`。

**副作用**：旧消息被替换为摘要。LLM 将看到摘要而不是完整历史——这就是压缩的核心效果。

---

## 4. 与 UEMCPHarness 的对应

| UEMCPHarness | PiAgent SessionManager | 覆盖情况 |
|---|---|---|
| **ToolCallLogger** (JSONL 写入) | `appendMessage()` 在每次 `message_end` 时写入同格式的 JSONL | **已覆盖** — PiAgent 的 JSONL 格式更结构化（有 parentId 树） |
| **session_id 管理** | `SessionHeader.id` + `SessionManager.getSessionId()` | **已覆盖** |
| **回放引擎** (`harness replay <session_id>`) | `buildSessionContext()` + `getEntries()` 提供所有历史消息 | **已覆盖** — 不需要单独的回放工具，直接从 JSONL 读取 |
| **统计面板** (`harness stats`) | `getSessionStats()` 聚合输入/输出 token、花费、消息数量 | **已覆盖** |
| **SnapshotRecorder** (截图归档) | **不覆盖** — SessionManager 不知道截图文件的存在 | **需额外实现** — 扩展可写自定义 entry 或额外文件 |
| **Skill 激活记录** | **不覆盖** — SessionManager 没有 "skill 激活" entry 类型 | **需额外实现** — 可用 custom entry |
| **vision_verdict 存储** | **不覆盖** | **需额外实现** — 扩展可写自定义 entry 或外部文件 |

---

## 5. 对迁移的意义

1. **UEMCPHarness 的 ToolCallLogger 是冗余的**：PiAgent 已经通过 SessionManager 完整记录了所有消息历史。迁移中不需要重写 JSONL 日志系统。

2. **Snapshot / Vision 相关数据**：需要扩展自定义 entry 类型或外部文件来存储截图路径、vision verdict 等非消息数据。可以通过 `pi.appendEntry("vision_verdict", { ... })` 或直接写文件。

3. **State Cache 持久化**：UEMCPHarness 的 State Cache（WorldState）需要独立持久化——它不能作为 SessionManager 的一部分（因为 State Cache 是跨 session 的，不与特定 session 绑定）。

4. **Session 分支功能的可能性**：PiAgent 的 `createBranchedSession` / `navigateTree` 天然支持"尝试一个方案 → 不满意 → 切回分叉点重新试"。这对 UE 场景非常有价值——"试试这个灯光配置 → 不对 → 回到调整前重新来"。

---

下一篇 [06-tools-and-skills](06-tools-and-skills.md) 将解析工具定义、Skill 系统、内置工具、和 streamSimple() 深读。
