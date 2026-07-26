# UE Agent Harness — 架构与实施方案

## 1. 现有基础设施分析

### 1.1 UE5.8 已提供的 MCP 能力

**ModelContextProtocol 插件（MCP Server）**
- 路径：`Engine/Plugins/Experimental/ModelContextProtocol/`
- 协议：JSON-RPC 2.0 over HTTP POST，绑定 `http://localhost:{port}/mcp`（默认端口 8000）
- Session 管理：`Mcp-Session-Id` header + `Mcp-Protocol-Version` header 协商
- 支持协议版本：`2025-11-25`、`2025-06-18`、`2024-11-05`
- 工具发现：`tools/list`（分页支持），`tools/call`（异步 SSE event-stream 响应）
- 延迟加载：`list_toolsets` / `describe_toolset` / `load_toolset`（默认启用，CVar `ModelContextProtocol.DeferredToolLoading`）
- 进度通知：`notifications/progress` SSE 帧，`notifications/tools/list_changed` 广播
- 自动启动：Editor Preference → General → Model Context Protocol → Auto Start Server
- 客户端配置生成：`ModelContextProtocol.GenerateClientConfig <Name>` 控制台命令

**ToolsetRegistry（工具注册表）**
- 路径：`Engine/Plugins/Experimental/ToolsetRegistry/`
- 核心类：`FToolsetRegistry` — `TMap<FString, TSharedPtr<FToolset>>`
- 工具执行：`ExecuteTool(FToolDescriptor, JsonInput) → TFuture<TValueOrError<FString, FString>>`
- 自定义 JSON 转换器：`FToolsetJsonConverter`
- 异步结果：`UToolCallAsyncResult` 系列
- 技能系统：`UAgentSkill` / `UAgentSkillBestPractices`（工具级文档，非任务级工作流）

**内置工具集（3 个 C++ + 16 个 Python）：**

C++ 工具集（注册于 `UToolsetRegistrySubsystem::Initialize()`）：
| 工具集 | 工具数 | 关键功能 |
|--------|--------|----------|
| EditorAppToolset | 17 | `GetSelectedActors`, `SelectActors`, `GetVisibleActors`, `CaptureEditorImage`, `CaptureAssetImage`, 相机操作, 内容浏览器操作 |
| LogsToolset | 4 | `GetLogEntries`, `GetLogCategories`, `GetVerbosity`, `SetVerbosity` |
| AgentSkillToolset | 4 | `ListSkills`, `GetSkills`, `CreateSkill`, `UpdateSkill` |

Python 工具集（位于 `ToolsetRegistry/Content/Python/toolset_registry/toolsets/core/`）：
| 工具集 | 工具数 | 关键功能 |
|--------|--------|----------|
| SceneTools | 12 | `find_actors`, `add_to_scene_from_class`, `add_to_scene_from_asset`, `remove_from_scene`, `load_level`, `get_current_level`, 文件夹管理, `trace_world` |
| ActorTools | 16 | `get_actor_transform`, `set_actor_transform`, `get_label`, `set_label`, 标签管理, 组件管理, `get_actor_bounds` |
| ObjectTools | 5 | `get_class`, `list_properties`, `get_properties`, `set_properties`, `search_subclasses` |
| PrimitiveTools | 4 | `add_cube`, `add_sphere`, `add_cylinder`, `add_cone` |
| BlueprintTools | 35+ | 蓝图编辑全流程 |
| AssetTools | ~10 | 资产管理 |
| + 其他 11 个 Python 工具集 | ~60 | 涵盖动画、材质、Niagara 等 |

**18 个 C++ Toolsets 插件（独立于上述内置工具集）：**
| 状态 | 插件 |
|------|------|
| 已完整实现（12 个） | AutomationTestToolset, ConversationToolset, DataflowAgent, GameFeaturesToolset, GameplayTagsToolset, GASToolsets, LiveCodingToolset, NiagaraToolsets, PhysicsToolsets, SlateInspectorToolset, UMGToolSet, WorldConditionsToolset |
| 存根（4 个） | AIModuleToolset, AnimationAssistantToolset, SequencerAnimMixerToolset, StateTreeToolset |

**总计约 157 个 MCP 可调用工具**，通过 ToolsetRegistry 注册，由 MCP Server 统一暴露。

### 1.2 现有 State Cache 数据源确认

State Cache 所需的所有核心数据**均有现有工具支持**（此前遗漏了 Python 工具集导致误判为缺失）：

| 缓存数据 | 现有工具 | 来源 |
|----------|----------|------|
| 当前地图路径 | `SceneTools.get_current_level()` | scene.py |
| 全部 Actor 列表 | `SceneTools.find_actors(glob='*')` | scene.py |
| Actor 变换 | `ActorTools.get_actor_transform(actor)` | actor.py |
| Actor 属性 | `ObjectTools.get_properties(actor, [...])` | object.py |
| 生成/删除/移动 Actor | `SceneTools.add_to_scene_*()` / `remove_from_scene()` / `ActorTools.set_actor_transform()` | scene.py, actor.py |
| 设置属性 | `ObjectTools.set_properties(actor, json)` | object.py |
| 选中 Actor | `EditorAppToolset.GetSelectedActors()` / `SelectActors()` | EditorAppToolset.h |
| 截图 | `SlateInspector.Screenshot()` / `EditorAppToolset.CaptureEditorImage()` | SlateInspectorToolset, EditorAppToolset |
| 日志 | `LogsToolset.GetLogEntries()` | LogsToolset.h |
| 视口相机 | `EditorAppToolset.GetCameraTransform()` / `SetCameraTransform()` | EditorAppToolset.h |
| **PIE 状态** | `SceneTools._is_pie()` 为**私有方法** | ⚠️ 需通过日志推导或新增公开工具 |
| **Build 状态** | **不存在** | ⚠️ 需通过 `LogsToolset` 推导 |

### 1.3 直接连接 Claude Code → UE MCP Server 的局限性

可以做到：单个工具调用、简单一步任务。
无法做到以下 10 项——这些构成了 Harness 的不可替代增量价值：

1. **状态缓存** — 每轮都重新扫描世界，100 步任务 = 100 次全量查询
2. **视觉验证闭环** — Claude Code CLI 不内建 vision analysis + 调整循环
3. **上下文组装** — 157 工具全灌入 context，无过滤、无 skill 注入
4. **长任务记忆压缩** — Chat history 膨胀，自然语言压缩丢失 UE 语义
5. **UE 感知重试** — 不知道 PIE 正在跑导致 spawn 失败需要等待
6. **多模态交叉验证** — 属性值 + 截图 + 渲染统计三方验证
7. **Session 持久化** — 关闭 Claude Code = 失去所有上下文
8. **安全护栏** — 无"不要删 PlayerStart"、"不要改系统关卡"规则
9. **Skill 体系分离** — UE `UAgentSkill`（工具级）和任务流程 skill（任务级）混在 context 里
10. **工具列表热更新** — 运行时新工具集注册后不会自动发现

---

## 2. 架构设计

### 2.1 拓扑

```
┌──────────────────────┐
│  Claude / GPT / etc  │  (MCP Client)
│  (LLM + Reasoning)   │
└──────────┬───────────┘
           │ MCP (JSON-RPC 2.0 over HTTP)
           ▼
┌──────────────────────────────────────────┐
│          UE Agent Harness                │  ← 外部 Python 进程
│                                          │
│  ┌────────────────────────────────────┐  │
│  │  MCP Server（面向 LLM）             │  │  ← mcp Python SDK
│  │  系统唯一入口，LLM 不知 UE Server 存在│  │
│  └───────────────┬────────────────────┘  │
│                  │                       │
│  ┌───────────────▼────────────────────┐  │
│  │  Context Assembler（上下文组装器）   │  │  ← 三层 prompt 构建
│  │  ├─ System Context（身份+状态快照）  │  │
│  │  ├─ Task Context（Skill 指令+记忆）  │  │
│  │  └─ Tool Reference（按需加载文档）    │  │
│  └───────────────┬────────────────────┘  │
│                  │                       │
│  ┌───────────────▼────────────────────┐  │
│  │  Agent Loop Controller             │  │  ← observe→think→act→verify
│  │  ├─ Tool Call Interceptor          │  │     观察→思考→执行→验证
│  │  ├─ State Cache (Write-Through)    │  │
│  │  └─ Task Memory (结构化压缩)        │  │
│  └───────────────┬────────────────────┘  │
│                  │                       │
│  ┌───────────────▼────────────────────┐  │
│  │  Verification Engine               │  │  ← 独立 Vision Sub-Agent
│  │  ├─ Capturer（截图获取）            │  │
│  │  └─ Vision Sub-Agent               │  │     独立上下文+结构化返回+可追问
│  └───────────────┬────────────────────┘  │
│                  │                       │
│  ┌───────────────▼────────────────────┐  │
│  │  MCP Client（面向 UE）              │  │  ← httpx + 手写 JSON-RPC 2.0
│  │  ├─ Session 管理（initialize 握手） │  │
│  │  ├─ SSE 两阶段解析                  │  │
│  │  └─ Tool List 生命周期管理          │  │
│  └───────────────┬────────────────────┘  │
│                  │                       │
│  ┌───────────────▼────────────────────┐  │
│  │  Observability（可观测性）          │  │
│  │  ├─ 全量 Tool Call 日志             │  │
│  │  ├─ Replay 回放引擎                 │  │
│  │  └─ 统计面板                        │  │
│  └────────────────────────────────────┘  │
└──────────┬───────────────────────────────┘
           │ MCP (JSON-RPC 2.0 over HTTP POST)
           ▼
┌──────────────────────┐
│  UE MCP Server       │  ← ModelContextProtocol 插件
│  localhost:8000/mcp  │
│                      │
│  ┌────────────────┐  │
│  │ ToolsetRegistry│  │
│  │ + ~157 tools   │  │
│  └────────────────┘  │
└──────────────────────┘
```

### 2.2 三层 Session 解耦

这是 Harness 架构的核心约束——三种 Session 生命周期不同，禁止相互绑定：

| Session 类型 | 职责 | 生命周期 | 管理者 |
|---|---|---|---|
| **MCP Connection Session** | 传输层连接状态：协议版本协商、HTTP 连接、SSE 流 | 连接 → 断开 | `mcp` SDK（面向 LLM 侧）/ `harness/client.py`（面向 UE 侧） |
| **Agent Session** | 任务执行状态：State Cache、Task Memory、当前步骤 | 任务启动 → 任务完成/失败。**可跨 MCP 连接存活** | Harness Agent Loop Controller |
| **Conversation Session** | 对话状态：用户与 LLM 的多轮对话历史 | 用户消息 → 多轮交互 → 任务达成 | Harness + LLM 共同管理 |

**核心原则：MCP 断开不丢失 Agent Session。** UE 崩溃 → MCP Session 断开 → Harness 保持运行 → 重连 UE → 恢复 Agent Session → 继续执行任务。

### 2.3 技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | MCP SDK 生态、async 原生、LLM SDK 可用 |
| MCP Server（面向 LLM） | `mcp` Python 包 | MCP 协议握手、SSE framing、session 管理全部内置 |
| MCP Client（面向 UE） | `httpx` + 手写 JSON-RPC 2.0 | UE MCP Server 使用原始 HTTP POST + SSE，不需要完整 MCP 客户端 |
| LLM 集成 | `openai` / `anthropic` SDK | 多供应商路由 |
| Vision | `anthropic.Vision` 或 `openai.chat.completions`（图片） | 截图验证 |
| 状态存储 | `pydantic` 模型 + JSON 序列化 | 结构化状态缓存 |
| 日志 | `structlog` | 结构化机器可解析日志，支持 replay |
| 配置 | `pyyaml` + `.env` | Harness 配置 |

### 2.4 关键接口

**MCP Client → UE（`harness/client.py`）：**
```
POST http://localhost:{port}/mcp
  body: {"jsonrpc": "2.0", "id": N, "method": "tools/call", "params": {...}}
  headers: {
    "Content-Type": "application/json",
    "Mcp-Session-Id": "...",
    "Mcp-Protocol-Version": "2025-11-25"  // initialize 后必须携带
  }
  response: Content-Type: text/event-stream
    → 空 SSE 流头 (MultipleWriteStream | HasAdditionalWrites)
    → 可选 progress: event:message\ndata:{notifications/progress}\n\n
    → 最终: event:message\ndata:{jsonrpc result}\n\n
```

**Context Assembler（`harness/context/`）：**
```
输入：原始 UE tool list + 当前 Task + Session State
输出：过滤后的 tool list + 注入的 system prompt + 注入的 skill 指令

三层上下文：
  Tier 1 — System Context（始终存在，~500 tokens）
    身份 + State Cache 快照 + 安全护栏
  Tier 2 — Task Context（Skill 匹配时注入，~200-800 tokens）
    步骤列表 + 完成/待完成 + 工具白名单
  Tier 3 — Tool Reference（按需加载，~200-500 tokens/toolset）
    describe_toolset 结果，仅 LLM 首次使用该工具集时加载
```

**Write-Through State Cache（`harness/state/`）：**
```
L1 写穿透（即时，零开销）：
  Harness 解析 write tool call 参数 → 直接更新缓存中被修改的 Actor
  示例：
    set_actor_transform(A, xform) → cache.actors[A].transform = xform
    add_to_scene_from_class(...) → 从返回值提取新 Actor → 加入缓存
    remove_from_scene(A) → 标记 cache.actors[A] = deleted

L2 读验证（按需，选择性）：
  仅校验被修改的 Actor：
    set_actor_transform(Light_0, ...) 成功
    → 可选: get_actor_transform(Light_0) 验证生效

L3 全量刷新（仅在 Hard Boundary 事件）：
  触发条件：
    - Harness 首次连接 UE（初始快照）
    - load_level() 调用（地图切换）
    - Harness 与 UE 重连（连接中断恢复）
    - LLM 显式请求 cache_refresh
  注意：未覆盖的 write tool 仅标记缓存为 dirty，不自动触发全量刷新
```

**Verification Engine（`harness/verification/`）：**
```
Vision Sub-Agent 设计：
  - 独立于主 Agent 的上下文窗口
  - 接收：截图（base64 PNG）+ 预期描述（来自 Skill YAML verification.expected）
  - 返回：结构化结果 {"pass": bool, "reason": str, "adjustment": str}
  - 可追问：Sub-Agent 可请求主 Agent 提供额外信息（如特定 Widget 的放大截图）
  - 状态保持：Sub-Agent 在整个任务期间保持同一 Agent Session
```

**Observability（`harness/observability/`）：**
```
日志格式（每个 tool call 一条 JSON 行）：
{
  "timestamp": "2026-06-13T10:00:00.000Z",
  "session_id": "abc123",
  "task_id": "coffee-shop-001",
  "tool_name": "SceneTools.find_actors",
  "tool_input": {"glob": "DirectionalLight*"},
  "tool_output": "[Actor_0, Actor_1]",
  "error": null,
  "duration_ms": 45,
  "screenshot_path": null,
  "verification": null
}
```

### 2.5 工具列表生命周期

```
UE Editor 启动
   ↓
ToolsetRegistry 注册工具集 → MCP Server 启动 → AdapterManager.RegisterTools()
   ↓（deferred 模式：仅注册 list_toolsets/describe_toolset/load_toolset 三个发现工具）
   ↓
Harness 连接 → initialize 握手 → tools/list → 获取 3 个发现工具
   ↓
Harness 遍历 list_toolsets 结果 → 串行 load_toolset 预加载所有工具集
  （每次 load_toolset → UE BroadcastToolsListChanged → SSE notification → Harness 拦截）
   ↓
Harness 重取 tools/list → 全部 ~157 工具就绪 → 存入内部缓存
   ↓
Context Assembler 根据当前 Skill 过滤 → 展示给 LLM
   ↓
... 运行时新工具集注册（如用户装了新插件）...
   ↓
OnToolsetRegistered → RegisterTools() → BroadcastToolsListChanged（SSE notification）
   ↓
Harness 自动拦截 → 重取 tools/list → 更新缓存 → 重新应用 Context Assembler 过滤
   ↓
下次 LLM 轮次使用更新后的工具列表
```

---

## 3. 实施阶段

### P0 — MCP Proxy + 全量透传（3 天）
**目标：** 端到端链路打通。LLM → Harness → UE → 工具执行 → 返回结果。

**交付物：**
- `harness/server.py` — MCP Server（mcp SDK），LLM 连接入口
- `harness/client.py` — JSON-RPC 2.0 + SSE 解析客户端，连接 UE MCP Server
  - 核心复杂度：SSE 两阶段解析（空流头 → progress 帧 → 最终 result 帧）
  - `Mcp-Protocol-Version` header 在所有 post-initialize 请求中携带
  - `notifications/tools/list_changed` 拦截（Harness 内部消费，不转发给 LLM）
- `harness/config.py` — 配置管理
- `harness/cli.py` — `harness start --ue-port 8000 --listen-port 9000`

**关键技术细节：**
- Session 初始化流程：`initialize`（协议版本协商）→ 获取 `Mcp-Session-Id` → `notifications/initialized` → `tools/list`
- 工具预加载：Harness 连接后自动遍历 `list_toolsets` → 逐个 `load_toolset` → 等 `list_changed` → 重取 `tools/list`（串行 12 次，约 2-3 秒启动时间）
- `load_toolset` 从 LLM 可见工具列表中移除——Harness 内部管理，LLM 不可见
- Harness 等待 UE SSE 完整结束后，一次性返回最终结果给 LLM（不转发 SSE 流）

### P1 — Observability 可观测性（2 天）
**目标：** 每个 tool call 全量记录，失败可回放复现。

**交付物：**
- `harness/observability/logger.py` — 拦截所有 tool call，记录结构化 JSON 到 `~/.ue-harness/logs/{session_id}.jsonl`
- `harness/observability/replay.py` — 读取日志文件，对运行中的 UE 实例回放 tool call 序列
- `harness/observability/stats.py` — `harness stats` 命令：工具调用次数、错误率、平均耗时

### P2 — Context Assembly 上下文组装（3 天）
**目标：** 与直接 MCP 连接的第一个质变。Harness 控制 LLM 上下文。

**交付物：**
- `harness/context/filter.py` — 工具过滤：按类别/任务过滤，自由探索模式默认暴露 ~20 个高频工具
- `harness/context/prompt.py` — Prompt 构建器：组装三层上下文
- `harness/context/skill_registry.py` — Skill CRUD：`harness skill create/update/delete/list`

**Skill 文件格式：**
```yaml
# ~/.ue-harness/skills/evening-lighting.yaml
name: evening-lighting
description: "将场景光照调整为黄昏/傍晚氛围"
triggers:
  - "make it evening"
  - "adjust to dusk"
  - "sunset lighting"
  - "黄昏"
  - "傍晚"
tools_allowlist:  # 仅允许 LLM 使用这些工具
  - "ObjectTools.list_properties"
  - "ObjectTools.get_properties"
  - "ObjectTools.set_properties"
  - "SceneTools.find_actors"
  - "ActorTools.set_actor_transform"
  - "SlateInspector.Screenshot"
steps: |
  1. 找到场景中所有的 DirectionalLight Actor
  2. 将主 DirectionalLight 旋转调整为低角度（地平线上 10-20 度）
  3. 设置光源色温为暖色（3000-4000K）
  4. 将强度降低到默认值的 30-50%
  5. 降低 SkyLight 强度
  6. 考虑添加带暖色分级的 PostProcessVolume
  7. 截图验证场景是否看起来像黄昏
  8. 如果不满意，根据 Vision Sub-Agent 反馈继续调整

verification:
  type: screenshot
  expected: "场景具有温暖的低角度光照和长阴影。天空为暗色或暮色。整体亮度降低。"
  tolerance: 0.7  # Vision model 置信度阈值
```

### P3 — Visual Verification 视觉验证闭环（4 天）
**目标：** Harness 与 Coding Agent 的本质差异。基于视觉的质量保证。

**交付物：**
- `harness/verification/capturer.py` — 通过 MCP 调用 `SlateInspector.Screenshot()` 或 `EditorAppToolset.CaptureEditorImage()`
- `harness/verification/vision_agent.py` — 独立 Vision Sub-Agent
  - 独立于主 Agent 的上下文窗口
  - 接收：base64 PNG（直接从 `FToolsetImage.Data` 解码） + 预期描述
  - 返回：`{"pass": bool, "reason": str, "adjustment": str}`
  - 可追问主 Agent 获取额外信息
  - 同一任务内保持 Agent Session 状态
- `harness/verification/loop.py` — 验证循环控制器：每次 tool call 后按 Skill 定义的 verification 策略执行

**关键技术细节：**
- `FToolsetImage` 返回 `MimeType: "image/png"` + `Data: base64`——Harness 零转码直传 vision API
- 截图分辨率：发送前 resize 到 1024x768 最大尺寸（token 成本优化）
- 遥测：记录 vision model 延迟、成本、通过/失败率

### P4 — State Cache 状态缓存（3 天）
**目标：** LLM 不再每轮重新扫描 UE 世界。

**交付物：**
- `harness/state/cache.py` — pydantic 世界状态模型 + write-through 更新方法
- `harness/state/interceptor.py` — 解析 tool call → 识别写操作 → 更新缓存。硬编码 ~15 个高频 write tool 的 handler
- `harness/state/refresher.py` — Hard Boundary 事件驱动的全量刷新

**Write-Through Cache 三层策略：**
1. **L1 写穿透**：拦截 write tool call（`set_actor_transform`、`set_properties`、`add_to_scene_*`、`remove_from_scene` 等），从参数中提取变更，即时更新缓存
2. **L2 读验证**：写操作后对修改的 Actor 做选择性重查（可选，由 Skill verification 策略决定）
3. **L3 全量刷新**：仅 Hard Boundary 事件触发。未覆盖的 write tool 仅标记缓存 dirty，不自动刷新

**缓存结构：**
```python
class WorldState(BaseModel):
    map_path: str
    actors: dict[str, ActorSnapshot]  # actor_name → snapshot
    selected_actors: list[str]
    dirty_actors: set[str]  # 被未覆盖 tool 修改的 actor，需要 LLM 显式刷新
    last_full_refresh: datetime
```

### P6 — Structured Task Memory 结构化任务记忆（2 天）
**目标：** 100+ 步任务不爆上下文。

**交付物：**
- `harness/memory/compressor.py` — 将原始 tool call 历史压缩为结构化进度
- `harness/memory/injector.py` — 注入压缩后的记忆到 LLM context，替代原始历史

**任务记忆模型：**
```python
class TaskMemory(BaseModel):
    task_id: str
    description: str          # "构建一个咖啡馆场景"
    completed: list[str]      # ["地板放置", "墙体建造", "桌子布置"]
    pending: list[str]        # ["灯光设置", "装饰摆放"]
    current_step: str         # "正在调整 DirectionalLight 角度"
    tool_call_count: int      # 42
    errors: list[str]         # ["生成椅子资产失败: 资产未找到"]
    key_assets: dict[str, str]  # {"floor_material": "/Game/Materials/M_WoodFloor"}
```

### P7 — Retry & Recovery 重试与恢复（2 天）
**目标：** UE 感知的错误分类和智能重试。

**交付物：**
- `harness/recovery/classifier.py` — 错误分类：PIE_RUNNING、MAP_LOADING、ASSET_LOCKED、TIMEOUT、NETWORK、UNKNOWN
- `harness/recovery/retry.py` — 每类错误的策略：PIE_RUNNING → 等待重试（最多 3 次）；TIMEOUT → 重试一次；UNKNOWN → 立即失败交 LLM 决策
- `harness/recovery/handler.py` — 集成到 Agent Loop：错误 → 分类 → 应用策略 → 重试或上报 LLM

### P8 — Safety Guardrails 安全护栏（2 天）
**目标：** 防止关键上下文中的破坏性操作。

**交付物：**
- `harness/safety/rules.py` — 规则引擎：(condition, action) 对。条件：工具名模式、资产路径模式、Actor 类模式、PIE 状态。动作：ALLOW、ASK_USER、DENY
- `harness/safety/preflight.py` — 预检钩子，每次 tool call 前执行
- `harness/safety/defaults.py` — 默认规则：禁止删除 PlayerStart、Engine 内容操作警告、批量删除（>10 Actor）警告、PIE 期间禁止写操作

---

## 4. 项目目录结构

> 最后更新：2026-07-26（重构后）

```
ue-agent-harness/
├── harness/
│   ├── cli.py              # CLI 入口 + 拦截器链注册 + _build_instructions
│   ├── server.py           # MCP Server（面向 LLM, 407 行, 注册表分发, build_server）
│   ├── client.py           # MCP Client（面向 UE, JSON-RPC 2.0 + SSE 解析, McpClientSession）
│   ├── config.py           # Config dataclass + from_env + merge_cli_overrides
│   ├── transport.py        # MCP SSE transport (create_app + serve)
│   ├── interceptor.py      # ToolCallInterceptor 基类 + ToolCallCompleted
│   ├── tools.py            # HarnessTool + ToolContext + tool_ok/tool_fail
│   ├── context/
│   │   ├── filter.py       # 工具过滤 (ctx_filter_tools, ctx_is_escape_hatch)
│   │   ├── prompt.py       # 三层 Prompt 组装 (assemble_system_prompt + ContextProvider)
│   │   ├── provider.py     # Context Provider 接口
│   │   ├── skill_registry.py # Skill CRUD（YAML 文件管理）
│   │   └── skill_tools.py  # activate_skill/save_skill/deactivate_skill/get_context handler
│   ├── verification/
│   │   ├── atmosphere.py   # build_atmosphere_mapping (MiMo 氛围组件扫描)
│   │   ├── capturer.py     # 截图获取 (capture_screenshot + Screenshot)
│   │   ├── config.py       # Vision 配置 (.vision.env 加载, load_vision_env)
│   │   ├── debug.py        # Vision 调试开关
│   │   ├── drift_alert.py  # DriftAlertInterceptor (漂移时注入警告)
│   │   ├── interceptor.py  # VisionInterceptor + ReadbackInterceptor + is_screenshot_tool
│   │   ├── metrics.py      # compute_match_metrics (直方图/SSIM/R-B ratio)
│   │   ├── reference.py    # match_reference handler + ReferenceImageSession
│   │   ├── session.py      # VisionSessionManager + record_write + build_full_prompt_context
│   │   ├── vision_agent.py # VisionSubAgent (独立 LLM API) + VisionVerdict
│   │   └── vision_tools.py # vision_screenshot/ask/tell/reset/status handler
│   ├── state/
│   │   ├── models.py       # WorldState + ActorSnapshot pydantic 模型
│   │   ├── interceptor.py  # StateCacheInterceptor (on_write 回调注入, L1 写穿透)
│   │   ├── refresher.py    # state_full_refresh (L3 全量刷新)
│   │   ├── hard_boundary.py # execute_hard_boundary (指纹校验 + dirty-diff)
│   │   └── normalize.py    # normalize_tool_args + state_parse_ref_path + mcp_tool_short_name
│   ├── observability/
│   │   ├── logger.py       # JSONL ToolCallLogger
│   │   ├── replay.py       # 回放引擎
│   │   ├── snapshotter.py  # SnapshotRecorder (截图+上下文归档)
│   │   └── stats.py        # 统计面板
│   ├── safety/             # 011 安全护栏（待实现）
│   └── memory/             # 空目录（009 任务记忆已作废，见 ADR 0008）
├── skills/                 # 内置 Skill 示例
├── tests/                  # 384 passed + 4 skipped (2026-07-26)
├── pyproject.toml
├── README.md
└── docs/
    ├── architecture.md     # 本文档
    ├── CONTEXT.md          # 领域语言词汇表
    ├── contracts.md        # 接口契约
    ├── adr/                # 架构决策记录 (0001-0008)
    ├── issues/             # 开发 Issue
    ├── plans/              # 实施计划
    └── handoff/            # 交接文档
```

---

## 5. MVP 范围与成功标准

**MVP = P0 + P1 + P2 + P3 + P4（约 2 周）**

P5 Transaction & Undo **从 MVP 移除**——Write-Through Cache 的状态记录提供了足够的安全网，LLM 可自行管理回退。

MVP 完成后 Harness 可以：

1. `harness start` 启动，连接运行中的 UE 编辑器（MCP Server 已启用）
2. Claude Code 等 MCP 兼容 LLM 连接 Harness，发现工具
3. Skill 文件（`evening-lighting.yaml`）定义带工具白名单和验证标准的多步任务
4. LLM 执行 Skill 时，Harness：
   - 仅暴露白名单工具
   - 注入步骤指令和当前 State Cache 快照
   - 记录每个 tool call
   - 按 verification 策略截图 → Vision Sub-Agent 判断 → 反馈调整
5. Session 在 UE 编辑器重启后存活（Harness 保持运行、重连、恢复 State Cache）

## 6. MVP 不做的事

- 多 UE 实例编排（单 Harness → 单 UE）
- UE Transaction 管理器
- 实时协作编辑
- 蓝图可视化图谱生成
- 自定义 LLM 训练/微调
- GUI 前端（仅 CLI）
- 代码生成或 C++ 修改（Claude Code 负责这些——Harness 是 Editor Agent，不是 Code Agent）
