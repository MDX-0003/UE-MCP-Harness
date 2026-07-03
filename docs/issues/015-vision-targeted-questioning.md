# Issue 015: Vision Session 架构 — 统一的多轮对话与上下文管理

**创建日期**: 2026-07-03
**状态**: ❌ 待开发
**依赖**: 006 Vision Pipeline, 007 验证闭环, 008 State Cache
**关联**: Issue 014（闭环验收场景直接依赖此架构）

## 1. 现状批判

### 1.1 Vision 寄生在 Screenshot 上

```
LLM 调 take_screenshot → capturer.capture() → VisionInterceptor.post_call() → VisionSubAgent.check()
                              ↑                         ↑
                         唯一入口                  隐式副作用，LLM 不可见
```

Vision 分析是 `take_screenshot` 的副作用，不是一等公民。LLM 无法：
- 对**同一张截图**继续追问
- 在不同截图之间保持对话连续性（"比上次亮了吗？"）
- 查看 Vision 会话状态

### 1.2 Session 管理的缺失

当前"会话"是 `VisionSubAgent._history: list[dict]` —— 裸消息列表。没有 ID、没有截图绑定、不支持多截图、reset 靠外部手动调用。

### 1.3 上下文注入完全不存在

Vision Agent 不知道场景里有什么、刚才改了什么、L2 读回确认了什么。State Cache 有这些信息但从未传入 Vision。

## 2. 设计决策

### 决策 1: Session ≠ Screenshot

**Session 绑定到任务/场景，不是绑定到单张截图。**

| 方案 | 行为 | 问题 |
|------|------|------|
| ~~每次截图 = 新 Session~~ | `vision_screenshot` 关闭旧 Session 开新的 | 无法跨截图比较、无法引用前图 |
| **Session 累积截图** ✅ | `vision_screenshot` 追加截图到当前 Session | — |

Session 边界由 **显式 reset** 或 **超时** 触发，不由截图触发。这样：

```
vision_screenshot(question="5个立方体对齐吗?")      → Screenshot A 加入 Session
set_actor_transform(Cube_3, z=0)                   → dirty: Cube_3
vision_screenshot(question="现在对齐了吗?")           → Screenshot B 加入同一 Session
vision_ask(question="对比两次截图，Cube_3 下降了?")    → Vision 可引用 A vs B
vision_reset()                                      → 开启新 Session
```

### 决策 2: 上下文自动注入为主，`vision_tell` 为辅

**系统自动注入**它已知的信息（State Cache 的 dirty actors、最近 write tool 的参数、L2 读回结果）。LLM 不需要记住该注入什么——框架自动做。

**LLM 手动 `vision_tell`** 仅用于系统无法推断的**任务意图**（"我要实现黄昏光照"）。

自动注入的信息来源：

| 来源 | 内容 | 触发时机 |
|------|------|---------|
| `WorldState.dirty_actors` | 哪些 Actor 刚被修改 | 每次 `vision_ask` / `vision_screenshot` 前 |
| 被修改 Actor 的当前属性 | name, class, transform, label | 同上 |
| 最近一次 write tool 调用 | `set_actor_transform(Cube_3, z=150)` | 同上 |
| L2 读回（如果发生） | `get_actor_transform → z=150` | Interceptor 链路中自动捕获 |

### 决策 3: 上下文过滤，不全量灌入

WorldState 可能有 200+ Actor。全量灌入浪费 token 且稀释 Vision 注意力。

**过滤规则**（优先级递减）：
1. **问题提及**：如果 `question` 中包含某 Actor 名称，加入该 Actor 详情
2. **Dirty 优先**：`dirty_actors` 中的 Actor 全部加入（最近被修改）
3. **Recency**：按 `last_updated` 排序，最新的 10 个
4. **Token 硬上限**：组装后的 context 不超过 ~1000 tokens（约 3000 字符），超出则截断并标注

## 3. 目标架构

### 3.1 工具一览

| 工具 | 职责 | 是否调 Vision API |
|------|------|:---:|
| `vision_screenshot` | 截图 + 追加到当前 Session + 可选首次提问 | ✅ |
| `vision_ask` | 在当前 Session 中追问 | ✅ |
| `vision_tell` | LLM 手动注入任务意图（系统不可推断的部分） | ❌ |
| `vision_reset` | 关闭当前 Session，开启新 Session | ❌ |
| `vision_status` | 查看 Session 摘要 | ❌ |

### 3.2 组件分层

```
┌──────────────────────────────────────────────────────────┐
│                    Harness MCP Tools                      │
│                                                          │
│  vision_screenshot  ─── 截图 +追加到 Session + 首次提问    │
│  vision_ask         ─── 追问（复用 Session 内所有截图）     │
│  vision_tell        ─── 注入 LLM 意图（仅在系统无法推断时） │
│  vision_reset       ─── 显式关闭旧 Session，开启新 Session  │
│  vision_status      ─── 查看 Session 状态                 │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│              VisionSessionManager                         │
│                                                          │
│  职责：会话生命周期、截图管理、自动上下文组装、归档          │
│                                                          │
│  ┌────────────────────────────────────────────┐         │
│  │ VisionSession                              │         │
│  │  - id: str                                 │         │
│  │  - created_at / last_active_at: datetime   │         │
│  │  - screenshots: list[ScreenshotRef]  # 多张 │         │
│  │    └─ ScreenshotRef: {b64, meta, timestamp}│         │
│  │  - context_blocks: list[ContextBlock]      │         │
│  │    └─ ContextBlock: {source, content, ts}  │         │
│  │       source: "auto:dirty" | "auto:write"  │         │
│  │             | "auto:l2" | "manual:tell"   │         │
│  │  - question_log: list[(question, verdict)] │         │
│  │  - _agent: VisionSubAgent                  │         │
│  └────────────────────────────────────────────┘         │
│                                                          │
│  start(question?) → Session                              │
│  add_screenshot(session, b64, meta, question?) → Verdict │
│  ask(session, question) → VisionVerdict                  │
│  tell(session, info) → None                              │
│  reset() → 归档旧 Session，返回新 Session                 │
│  get_active() → Session | None                           │
│                                                          │
│  _build_auto_context(session, world_state) → str         │
│    组装来源：dirty actors + last write + L2 read-back      │
│    过滤规则：question 提及 > dirty > recency > token cap   │
│  _build_full_prompt(session, question) → str              │
│    自动上下文 + manual tell 上下文 + question              │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│              VisionSubAgent (API Client)                  │
│  职责：Anthropic API 调用、消息格式、token 统计            │
│  不做：会话管理、上下文组装                                │
└──────────────────────────────────────────────────────────┘
```

### 3.3 新增文件

```
harness/verification/
├── vision_agent.py     # VisionSubAgent（小幅修改）
├── session.py          # [新] VisionSession + VisionSessionManager + context builder
├── interceptor.py      # VisionInterceptor（对接 SessionManager）
├── capturer.py         # 截图（不变）
└── config.py           # Vision 配置（不变）
```

## 4. 工具 API 详细设计

### 4.1 `vision_screenshot`

```json
{
  "name": "vision_screenshot",
  "description": "获取 UE 编辑器截图，追加到当前 Vision Session，可选附带针对性提问。如无活跃 Session 则自动创建。",
  "inputSchema": {
    "properties": {
      "mode": { "enum": ["viewport", "editor", "asset"], "default": "viewport" },
      "asset_path": { "type": "string" },
      "hide_ui": { "type": "boolean", "default": false },
      "question": {
        "type": "string",
        "description": "可选。针对本次截图的首次提问。Vision 会自动获得场景上下文（dirty actors、最近修改等）。"
      }
    }
  }
}
```

**返回值格式**：
```
Screenshot 已获取: 1920x1080 image/png (mode=viewport)
Session: a1b2c3d4 (截图 #2，累计 3 次提问)

[Vision 分析] ✅ PASS
所有 5 个立方体底面在同一水平面，Cube_3 已从悬浮状态修正。阴影投影一致。
```

### 4.2 `vision_ask`

```json
{
  "name": "vision_ask",
  "description": "在当前 Vision Session 中追问（不截新图）。自动附带最新场景上下文。",
  "inputSchema": {
    "properties": {
      "question": {
        "type": "string",
        "description": "追问。Vision 可引用 Session 内的历史截图和对话。"
      }
    },
    "required": ["question"]
  }
}
```

### 4.3 `vision_tell`

```json
{
  "name": "vision_tell",
  "description": "向当前 Session 注入 LLM 的意图或预期（系统无法自动推断的信息）。不触发 API 调用。如需注入系统已知的数据（Actor 状态、修改记录），系统会自动完成，无需手动调用。",
  "inputSchema": {
    "properties": {
      "info": {
        "type": "string",
        "description": "任务级意图或预期。如：'目标是傍晚暖色光照，DirectionalLight 色温应为 4000K，阴影长度应为 Actor 高度的 3 倍'。"
      }
    },
    "required": ["info"]
  }
}
```

### 4.4 `vision_reset`

```json
{
  "name": "vision_reset",
  "description": "关闭当前 Vision Session（归档到日志），开启新 Session。在新任务开始或场景发生根本变化时调用。",
  "inputSchema": { "properties": {} }
}
```

### 4.5 `vision_status`

```json
{
  "name": "vision_status",
  "description": "查看当前 Vision Session 摘要：时长、截图数、提问数、token 消耗、上次结论。",
  "inputSchema": { "properties": {} }
}
```

**返回值示例**：
```
Vision Session: a1b2c3d4 (活跃 8 分钟)
  截图: 2 张 (最近: 1920×1080 viewport, 30秒前)
  提问: 3 次 | 手动上下文: 1 条
  Token 消耗: 4,210
  自动注入上下文: dirty=[Blueprint_Cube_3] write=set_actor_transform
  上次结论: ✅ 所有立方体对齐
```

## 5. 自动上下文注入机制

### 5.1 注入来源

每次 `vision_screenshot` 或 `vision_ask` 前，`VisionSessionManager._build_auto_context()` 自动组装：

```python
def _build_auto_context(session, world_state, question="") -> str:
    blocks = []

    # 来源 1: Dirty actors（谁刚被改了）
    if world_state.dirty_actors:
        dirty_details = []
        for name in sorted(world_state.dirty_actors):
            snap = world_state.actors.get(name)
            if snap and not snap.deleted:
                loc = snap.transform.get("location", {}) if snap.transform else {}
                dirty_details.append(
                    f"  - {name} ({snap.class_name or 'Unknown'})"
                    f" | label=\"{snap.label or ''}\""
                    f" | loc=({loc.get('x',0):.0f},{loc.get('y',0):.0f},{loc.get('z',0):.0f})"
                )
        if dirty_details:
            blocks.append("最近修改的 Actor（可能影响截图内容）：\n" + "\n".join(dirty_details))

    # 来源 2: 最近 write tool 调用
    recent = _get_recent_writes(limit=3)  # 从 ToolCallLogger 或内存 buffer
    if recent:
        blocks.append("最近执行的操作：\n" + "\n".join(f"  - {r}" for r in recent))

    # 来源 3: L2 读回（如果 StateCache 检测到 write 后触发了读回）
    l2 = _get_last_l2_readback()
    if l2:
        blocks.append(f"L2 读回验证：{l2}")

    # 组装 + token cap
    raw = "\n\n".join(blocks)
    return _cap_tokens(raw, max_tokens=1000)
```

### 5.2 过滤规则

| 优先级 | 规则 | 目的 |
|--------|------|------|
| P0 | `question` 中提及的 Actor 名 → 全文提取 | 问题针对性 |
| P1 | `dirty_actors` 全部提取 | 最近被修改，最可能相关 |
| P2 | 最近 10 个 `last_updated` Actor | 补充上下文 |
| P3 | Token cap ~1000（约 3000 字符） | 防止爆炸 |

### 5.3 `vision_tell` 的使用场景

仅在以下情况手动调用：
- 任务级意图："我要实现傍晚光照"
- 验收标准："阴影长度应为 Actor 高度的 3 倍"
- 跨轮次记忆："上一轮你判断灯光太冷，我已经把色温从 6500K 降到 4000K"

这些系统无法推断，必须 LLM 提供。

### 5.4 Recent Writes Buffer — 操作记录的内存滚动缓冲

**问题**：`_build_auto_context()` 需要"最近 N 次 write 操作记录"来告知 Vision "刚才改了什么"。`StateCacheInterceptor` 已处理每次 write 调用并更新 `dirty_actors`，但只记录"谁被改了"，不记录"怎么改的"。

**方案**：在 `session.py` 中维护一个模块级 `deque(maxlen=10)`，由 `StateCacheInterceptor` 在 post_call 成功后追加一条**人可读的操作描述**。

**数据流**：

```
LLM 调 set_actor_transform(Cube_3, z=150)
  → StateCacheInterceptor.post_call()
    → 更新 WorldState.dirty_actors（现有逻辑，不变）
    → record_write("set_actor_transform", event.args)  ← 新增
      → _recent_writes.append(
           "set_actor_transform(Blueprint_Cube_3, location=(0,0,150))")

LLM 调 set_properties(DirectionalLight_0, {LightColor: (1,0.5,0.3), Intensity: 8})
  → record_write("set_properties", event.args)
    → _recent_writes.append(
         "set_properties(DirectionalLight_0, [LightColor, Intensity])")

LLM 调 vision_screenshot(question="对齐了吗?")
  → VisionSessionManager._build_auto_context()
    → get_recent_writes(limit=3)
      → [
          "set_actor_transform(Blueprint_Cube_3, location=(0,0,150))",
          "set_properties(DirectionalLight_0, [LightColor, Intensity])",
        ]
    → 注入 prompt: "最近执行的操作：\n  - set_actor_transform(...)\n  - set_properties(...)"
```

**`_format_write_description()` 映射表**：

`record_write()` 内部根据 handler 类型从 `event.args` 中提取关键字段，生成简洁描述：

| Handler | args 关键字段 | 格式化结果示例 |
|---------|-------------|-------------|
| `set_actor_transform` | `actor.name`, `xform.location` | `set_actor_transform(Cube_3, location=(0,0,150))` |
| `set_properties` | `actor.name`, `json` keys | `set_properties(DirectionalLight_0, [LightColor, Intensity])` |
| `set_label` | `actor.name`, `label` | `set_label(Cube_3, "悬浮立方体")` |
| `add_to_scene_from_class` | `actor_type`, `label` | `add_to_scene(PointLight, label="新光源")` |
| `add_to_scene_from_asset` | `asset_path` | `add_to_scene_from_asset(/Game/Meshes/SM_Chair)` |
| `remove_from_scene` | `actor.name` | `remove_from_scene(Cube_5)` |
| `add_tag` / `remove_tag` | `actor.name`, `tag` | `add_tag(Cube_3, "modified")` |
| 未覆盖的 write tool | 全部 args | `UnknownTool.ToolName({args摘要})` |

**改动范围**：

- `harness/verification/session.py`：新增 `_recent_writes: deque` + `record_write(name, args)` + `get_recent_writes(limit)` 
- `harness/state/interceptor.py`：在每个 handler 成功后调用 `record_write(short_name, event.args)`（一行追加，~12 处）

**设计约束**：
- `deque(maxlen=10)` 保证内存固定
- buffer 是模块级的（不绑定到特定 Session），因为操作发生在 SessionManager 创建之前
- VisionSessionManager 只读不写——write 记录由 StateCacheInterceptor 写入，SessionManager 在 `_build_auto_context()` 时读取

### 5.5 Token Cap — 上下文截断策略

**问题**：Vision API 按 token 收费。WorldState 可能有 200+ Actor，如果全部灌入 context，导致：（1）每次 `vision_ask` 成本增加 （2）注意力稀释——Vision 模型读 200 个 Actor 列表才能找到被改的 3 个 （3）推理延迟增加。

**概念澄清**：此处的 token cap 仅控制**文本上下文**（user message 中注入的 Actor 列表、操作记录等）。截图的 base64 不受此限制——Vision API 按图片尺寸单独计算 image tokens，不在讨论范围内。

**方案**：**过滤优先，截断兜底**。

```
P0: question 中提及的 Actor 名 → 全文提取该 Actor（不管是否 dirty）
P1: dirty_actors 全部提取（最近被修改，最相关，通常 < 10 个）
P2: 最近 10 个 last_updated Actor（补充上下文）
P3: 硬截断 1000 tokens ≈ 3000 字符 → 截掉的部分标注省略量
```

**token 估算方式**：`max_chars = max_tokens × 3`。中文约 1.5 char/token，英文约 4 char/token，混合取 3 是保守估算（不会严重低估导致 token 超限）。

**实现**：

```python
def _cap_context(blocks: list[tuple[int, str]], max_tokens: int = 1000) -> str:
    """按优先级组装 context blocks，超出 token cap 则截断低优先级 blocks 并标注。

    blocks: [(priority, text), ...]，已按 priority 排序（0 最高）。
    P0/P1（critical）必须保留；P2+（optional）超出 cap 时截断。
    """
    max_chars = max_tokens * 3

    # 分离 critical（P0, P1）和 optional（P2+）
    critical = "\n\n".join(t for pri, t in blocks if pri <= 1)
    optional_parts = [(pri, t) for pri, t in blocks if pri > 1]

    if len(critical) + sum(len(t) for _, t in optional_parts) <= max_chars:
        # 全部放得下
        return critical + "\n\n" + "\n\n".join(t for _, t in optional_parts)

    # 按优先级依次填充 optional
    remaining = max_chars - len(critical) - 80  # 80 字符留给省略说明
    included = []
    omitted_chars = 0
    for pri, text in optional_parts:
        if remaining > len(text):
            included.append(text)
            remaining -= len(text)
        else:
            if remaining > 80:
                included.append(text[:remaining])
            omitted_chars += len(text) - max(0, remaining)
            remaining = 0

    result = critical
    if included:
        result += "\n\n" + "\n\n".join(included)
    if omitted_chars > 0:
        result += f"\n\n... (省略约 {omitted_chars // 3} tokens：补充 Actor 列表等低优先级上下文)"

    return result
```

**示例**：
- 场景有 200 个 Actor，dirty 3 个，question 未提及 Actor 名
- P0: 无（question 未提及）
- P1: 3 个 dirty actor 详情 ~500 字符 — 保留
- P2: 10 个 recency actor ~2000 字符
- 上限 3000 字符：P1 (500) + P2 (2000) = 2500 < 3000 → 全部保留
- 上限 1000 字符：P1 (500) 保留，P2 截断到 420 字符 + 省略说明

---

## 6. Session 生命周期管理 — 过期与警告升级

### 6.1 设计原则

**始终由 LLM 主动调 `vision_reset()` 关闭 Session。** Harness 不自动关闭——自动关闭会打断 LLM 正在进行的分析链。

**但当 Session 超过健康阈值时**（时间过长 / 提问次数过多），Harness 在每次 tool 返回结果的**顶部**注入警告。警告持续出现并升级，直到 LLM 调 `vision_reset()`。

### 6.2 阈值与警告三级升级

```python
def _check_warning(session: VisionSession) -> str | None:
    """检查 Session 是否触发警告条件。返回警告文本或 None。"""
    age_min = (datetime.now(timezone.utc) - session.created_at).total_seconds() / 60
    count = session.question_count  # vision_screenshot + vision_ask 累计

    if age_min > 30 or count > 15:
        # L3: 严重超时 — 成本显著，Vision 上下文可能膨胀到影响判断质量
        return (
            f"🚨 [Vision Session 严重超时]\n"
            f"Session 已活跃 {age_min:.0f} 分钟，累计 {count} 次提问，"
            f"{session.screenshot_count} 张截图。\n"
            f"长时间 Session 累积高额 token 成本，且 Vision 上下文膨胀可能影响判断质量。\n"
            f"请立即调 vision_reset() 关闭此 Session。"
        )
    elif age_min > 15 or count > 8:
        # L2: 超时警告 — 成本累计中
        return (
            f"⚠ [Vision Session 超时警告]\n"
            f"Session 已活跃 {age_min:.0f} 分钟，累计 {count} 次提问。\n"
            f"长时间 Session 累积 token 成本。建议调 vision_reset() 关闭。"
        )
    elif age_min > 8 or count > 5:
        # L1: 温和提醒 — 成本开始累积
        return (
            f"💡 [Vision Session 提醒]\n"
            f"Session 已活跃 {age_min:.0f} 分钟。\n"
            f"如验证已完成，可调 vision_reset() 关闭以节省 token。"
        )
    return None
```

### 6.3 注入位置

在 `vision_screenshot` 和 `vision_ask` 的 handler 中，获取 Vision 分析结果后：

```python
# server.py — vision_screenshot / vision_ask handler 伪代码：
result_text = session_manager.add_screenshot(session, screenshot, question)  # or .ask()
warning = session_manager._check_warning(session)
if warning:
    result_text = warning + "\n\n" + result_text  # 警告置于结果顶部
return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

### 6.4 行为轨迹示例

```
第 1 次 vision_ask → 无警告（Session 活跃 2 分钟，1 次提问）

第 5 次 vision_ask → 无警告（Session 活跃 7 分钟，5 次提问）

第 6 次 vision_ask → 💡 [Vision Session 提醒]
  Session 已活跃 9 分钟。如验证已完成，可调 vision_reset() 关闭以节省 token。

第 7-8 次 vision_ask → 💡 持续出现（阈值未升级）

第 9 次 vision_ask → ⚠ [Vision Session 超时警告]
  Session 已活跃 16 分钟，累计 9 次提问。
  长时间 Session 累积 token 成本。建议调 vision_reset() 关闭。

第 10-15 次 vision_ask → ⚠ 持续出现

第 16 次 vision_ask → 🚨 [Vision Session 严重超时]
  Session 已活跃 32 分钟，累计 16 次提问，3 张截图。
  请立即调 vision_reset() 关闭此 Session。

LLM 调 vision_reset() → 警告消失，新 Session 干净开始，旧 Session 归档
```

### 6.5 附加字段

`VisionSession` 新增用于警告判断的字段：

```python
@dataclass
class VisionSession:
    # ... existing ...
    question_count: int = 0       # vision_screenshot + vision_ask 总次数
    screenshot_count: int = 0     # vision_screenshot 次数
    created_at: datetime          # Session 创建时间（用于计算年龄）
    last_active_at: datetime      # 最后一次操作时间（用于判断闲置）
```

---

## 7. Issue 014 闭环完整演练

```
┌─ 新任务开始 ────────────────────────────────────────┐
│ vision_reset()                                       │
│ → 新 Session: s1                                     │
└──────────────────────────────────────────────────────┘

┌─ 初始观测 ──────────────────────────────────────────┐
│ vision_screenshot(question="场景中有哪些Actor?")       │
│ → Auto-context: (空，无 dirty)                       │
│ → Vision: "5个立方体和一个地面平面，DirectionalLight"  │
└──────────────────────────────────────────────────────┘

┌─ 执行修改 ──────────────────────────────────────────┐
│ set_actor_transform("Blueprint_Cube_3", z=150)       │
│ → dirty_actors = {"Blueprint_Cube_3"}               │
│ → 系统记录: last_write = "set_actor_transform(        │
│     Blueprint_Cube_3, location.z=150)"               │
└──────────────────────────────────────────────────────┘

┌─ L2 读回 ───────────────────────────────────────────┐
│ get_actor_transform("Blueprint_Cube_3")              │
│ → {location: {z: 150}}  ✓ 写入确认                   │
│ → 系统记录: last_l2 = "get_actor_transform →          │
│     Blueprint_Cube_3 当前 z=150"                     │
└──────────────────────────────────────────────────────┘

┌─ 视觉验证（系统自动注入上下文）───────────────────────┐
│ vision_screenshot(question="Cube_3 底面是否与其他      │
│     4 个立方体对齐在同一水平面？")                      │
│                                                      │
│ → Auto-context（系统自动组装，LLM 无需手动 tell）:      │
│   "最近修改的 Actor：                                 │
│     - Blueprint_Cube_3 (StaticMeshActor)              │
│       | loc=(0,0,150)                                │
│    最近执行的操作：                                    │
│     - set_actor_transform(Blueprint_Cube_3, z=150)    │
│    L2 读回验证：                                       │
│     - get_actor_transform → Blueprint_Cube_3 z=150"   │
│                                                      │
│ → Vision: "Cube_3 明显悬浮在 150 单位高处，             │
│    其他 4 个立方体均在 z=0。❌ 不对齐。"                │
└──────────────────────────────────────────────────────┘

┌─ 追问细节 ──────────────────────────────────────────┐
│ vision_ask(question="Cube_3 的正下方地面是否有阴影？   │
│    地面材质是否连续？")                                │
│ → Auto-context: 同上（dirty 未变）                    │
│ → Vision: "地面材质连续，Cube_3 下方有正确的           │
│    阴影投影，因高度较大阴影比其他立方体更长"            │
└──────────────────────────────────────────────────────┘

┌─ LLM 修正 ──────────────────────────────────────────┐
│ set_actor_transform("Blueprint_Cube_3", z=0)         │
│ → dirty_actors = {"Blueprint_Cube_3"}               │
│ → last_write = "set_actor_transform(Cube_3, z=0)"    │
└──────────────────────────────────────────────────────┘

┌─ 再次验证（同一 Session，第二张截图）──────────────────┐
│ vision_screenshot(question="现在所有立方体对齐了吗？")  │
│ → Auto-context:                                      │
│   "最近修改的 Actor：                                 │
│     - Blueprint_Cube_3 | loc=(0,0,0)                 │
│    最近执行的操作：                                    │
│     - set_actor_transform(Blueprint_Cube_3, z=0)"     │
│                                                      │
│ → Vision: "所有 5 个立方体底面在同一水平面 (z=0)。     │
│    ✅ 对齐正确。与上次截图相比，Cube_3 明显下降了。"     │
└──────────────────────────────────────────────────────┘

┌─ 任务完成 ──────────────────────────────────────────┐
│ vision_status()                                      │
│ → "Session s1: 截图2张, 提问3次, tokens=6,200,        │
│    最后结论: ✅ 所有立方体对齐"                         │
│ vision_reset()  # 归档 Session s1                    │
└──────────────────────────────────────────────────────┘
```

注意整个流程中 LLM **从未手动调用 `vision_tell`**——所有 Actor 状态、操作记录、L2 结果都是系统自动注入的。LLM 只需关注"问什么问题"和"怎么修正"。

## 8. 实现计划

### Phase 1: VisionSession + VisionSessionManager（50 min）

新文件 `harness/verification/session.py`：

- [ ] `VisionSession` dataclass（id, created_at, last_active_at, question_count, screenshot_count, screenshots list, context_blocks, question_log）
- [ ] `ScreenshotRef` dataclass（b64, meta, timestamp）
- [ ] `ContextBlock` dataclass（source enum: auto:dirty | auto:write | auto:l2 | manual:tell，content, timestamp）
- [ ] 模块级 `_recent_writes: deque[maxlen=10]` + `record_write(short_name, args)` + `get_recent_writes(limit)`
- [ ] `_format_write_description(short_name, args)` — args → 人可读描述（见 5.4 映射表）
- [ ] `VisionSessionManager` 类：
  - `start()` / `reset()` / `get_active()`
  - `add_screenshot(screenshot_b64, meta, question?, scene_context?)` → 调 Vision API
  - `ask(question)` → 调 Vision API（复用 session 历史 + 全部截图）
  - `tell(info)` → 追加 ContextBlock（source=manual:tell）
  - `_check_warning(session)` → 三级警告文本（见 6.2）
- [ ] Session 归档到 `{log_dir}/{session_id}/vision_sessions/{vs_id}.json`
- [ ] 单元测试：session CRUD、多截图追加、reset 归档

### Phase 2: 自动上下文构建器（40 min）

在 `session.py` 中实现：

- [ ] `_build_auto_context(session, world_state, question)` 
  - 来源 1: `world_state.dirty_actors` → 提取 Actor 详情（name, class, transform, label, tags）
  - 来源 2: `get_recent_writes(limit=3)` → 最近 write 操作
  - 来源 3: L2 读回结果（从拦截器链路捕获，格式：`get_actor_transform → {actor} {result}`）
- [ ] `_extract_mentioned_actors(question, world_state)` — P0：从 question 中匹配 Actor 名
- [ ] `_cap_context(blocks, max_tokens=1000)` — P0~P3 优先截断（见 5.5）、3 chars/token 估算
- [ ] 单元测试：
  - 空 dirty → 返回空或仅 recent writes
  - 3 个 dirty → 全部提取
  - 200 Actor / 50 dirty → P1 50 个提取，P2 截断，省略说明出现
  - question 含 "Cube_3" → P0 提取 Cube_3 详情（即使它不在 dirty 中）
  - 中文文本 token 估算不会严重低估

### Phase 3: Session 过期警告（25 min）

- [ ] `VisionSession.question_count` / `screenshot_count` 字段 + 每次操作递增
- [ ] `VisionSessionManager._check_warning(session)` — 三级阈值 + 警告文本（见 6.2）
- [ ] `vision_screenshot` / `vision_ask` handler 中：获取结果后检查警告，注入结果顶部
- [ ] 单元测试：
  - 第 1-5 次提问无警告
  - 第 6 次触发 L1
  - 第 9 次触发 L2
  - 第 16 次触发 L3
  - vision_reset 后警告消失

### Phase 4: 工具注册（30 min）

在 `server.py` 中：

- [ ] `vision_screenshot` tool schema + handler（重命名自 `take_screenshot`，调用 SessionManager）
- [ ] `vision_ask` tool schema + handler（委托 SessionManager.ask，含警告注入）
- [ ] `vision_tell` tool schema + handler（委托 SessionManager.tell）
- [ ] `vision_reset` tool schema + handler（委托 SessionManager.reset）
- [ ] `vision_status` tool schema + handler（查询活跃 Session）
- [ ] 保留 `take_screenshot` 作为 `vision_screenshot` 的别名（兼容过渡期，deprecation warning）

### Phase 5: cli.py 集成（20 min）

- [ ] 创建 `VisionSessionManager` 实例（传入 config + world_state）
- [ ] `VisionInterceptor` 改为使用 `SessionManager`（而非直接调 `VisionSubAgent`）
- [ ] `StateCacheInterceptor` 的每个 handler 末尾加 `record_write(short_name, event.args)`
- [ ] `build_server()` 新增 `vision_session_manager` 参数

### Phase 6: VisionSubAgent 适配（15 min）

- [ ] `check()` 改为接受 `context_blocks: list[str]` 参数
- [ ] 新增 `VISION_SYSTEM_PROMPT_QUESTION`（针对性提问模式，自由文本回答）
- [ ] 三层 system prompt 选择逻辑：question → expected → describe
- [ ] `_call_vision_api()` 根据 question 模式选择对应的 system prompt

### Phase 7: 集成测试（30 min）

- [ ] `vision_screenshot(question="...")` → 返回针对性回答 + 自动 dirty actor 上下文
- [ ] `vision_screenshot` → `vision_ask` 追问链
- [ ] dirty actor 自动注入验证（set_actor_transform → vision_screenshot，检查返回文本包含 dirty info）
- [ ] Recent writes buffer 正确记录和注入
- [ ] Token cap：200 Actor 场景下 context ≤ 3000 字符
- [ ] `vision_reset` → 旧 Session 归档 → 新 Session 创建
- [ ] `vision_status` 摘要正确（时长、截图数、提问数、token 消耗）
- [ ] 无 Session 时 `vision_ask` 返回友好错误
- [ ] 多截图 Session（3 张截图），`vision_ask` 可引用历史截图
- [ ] 三级警告正确触发和升级

## 9. 验收标准

1. `vision_screenshot(question="有哪些类型的光源？")` → 返回针对性回答，自动附带 dirty actor 上下文
2. 随后 `vision_ask(question="暖色光的具体方向？")` → Vision 引用前一轮对话
3. 执行 `set_actor_transform` 后，下一次 `vision_screenshot` **自动**附带"最近修改：Cube_3, set_actor_transform(z=150)"信息
4. `vision_reset` → 旧 Session 归档到日志目录 → 下次 `vision_screenshot` 创建新 Session
5. 同一 Session 内 3 张截图，`vision_ask` 可引用"与第一张截图相比..."
6. WorldState 有 200 个 Actor 时，自动注入 ≤ 1000 tokens
7. Issue 014 的 L2+Vision 闭环完整走通（见第 6 节演练），LLM 无需手动 `vision_tell`
