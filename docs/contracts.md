# UE Agent Harness — 并行开发契约

本文档定义 003（可观测性）/ 004（Context Assembly）/ 006（Vision Pipeline）/ 008（State Cache）
四个模块之间的共享接口和数据类型约束。各模块对着契约开发，彼此不依赖对方的实现。

**修改规则**：已锁定的 Contract 只做追加，不改前文。新增 Contract 追加在末尾。

---

## Contract 1: ToolCallInterceptor — 工具调用拦截器接口

**涉及模块**：003（日志）、008（State Cache）、011（安全护栏，未来）

**契约状态**：✅ 已锁定

### 接口定义

```python
# harness/interceptor.py

from dataclasses import dataclass, field
from typing import Any


class ToolCallInterceptor:
    """工具调用拦截器基类。

    pre_call 和 post_call 都是可选的 —— 子类只覆盖需要的方法。
    当前 003 和 008 都只需 post_call；pre_call 保留给未来 #011 Safety Guardrails 使用。
    """

    async def pre_call(self, name: str, args: dict) -> dict:
        """在转发到 UE 之前调用。可修改 args（返回修改后的）。默认透传。"""
        return args

    async def post_call(self, event: "ToolCallCompleted") -> None:
        """在 UE 返回结果之后调用。默认空操作。"""
        pass


@dataclass
class ToolCallCompleted:
    """一次完整的工具调用所携带的全部信息。

    raw_result / parsed_text 的分工：
      - raw_result:   完整 JSON-RPC result dict/list/str，需要原始结构时用（如图片 base64）
      - parsed_text:  已剥离 MCP content array 外层的纯文本结果，handler 可直读
                      等价于 raw_result["content"][0]["text"] 的提取值。
                      如果 result 不是 MCP content array 格式，则 parsed_text = str(raw_result)
    """

    name: str
    args: dict
    raw_result: Any = None          # JsonRpcResponse.result — 完整原始结果
    parsed_text: str | None = None  # 已提取的 content[0].text，handler 可直读
    error: Exception | None = None
    duration_ms: float = 0.0
```

### 调用规范（server.py 侧）

所有拦截器实现放在 `harness/<module>/` 中，在 `harness/server.py` 的 `build_server()` 中按顺序注册。

**注册点**：

```python
# harness/server.py — build_server() 内

interceptors: list[ToolCallInterceptor] = [
    ToolCallLogger(config.log_dir, session_id),      # 003: 日志
    StateCacheInterceptor(cache),                     # 008: 缓存更新（error is None 时才更新）
]

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    t0 = time.monotonic()
    error = None

    # === pre 阶段 ===
    for ic in interceptors:
        try:
            arguments = await ic.pre_call(name, arguments)
        except Exception as e:
            logger.error("预拦截 %s 失败: %s", type(ic).__name__, e)

    # === 实际调用 ===
    try:
        result_text = await ue_client.call_tool(name, arguments)
    except Exception as e:
        error = e
        result_text = None

    duration_ms = (time.monotonic() - t0) * 1000

    # === 解析（只做一次，各 interceptor 共享） ===
    parsed_raw = None
    parsed_text = None
    if result_text is not None:
        try:
            parsed_raw = json.loads(result_text) if isinstance(result_text, str) else result_text
        except json.JSONDecodeError:
            parsed_raw = result_text

    if isinstance(parsed_raw, dict):
        content = parsed_raw.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parsed_text = item.get("text", "")
                break
    if parsed_text is None:
        parsed_text = result_text if isinstance(result_text, str) else json.dumps(parsed_raw or {})

    # === post 阶段 ===
    event = ToolCallCompleted(
        name=name, args=arguments,
        raw_result=parsed_raw, parsed_text=parsed_text,
        error=error, duration_ms=duration_ms,
    )
    for ic in interceptors:
        try:
            await ic.post_call(event)
        except Exception as e:
            logger.error("后拦截 %s 失败: %s", type(ic).__name__, e)

    if error:
        raise error
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

### 各模块使用方式

| 模块 | pre_call | post_call | 使用字段 | 注意事项 |
|------|:---:|:---:|---|---|
| 003 Logger | 默认透传 | 整条 event 序列化后异步写 JSONL | `name`, `args`, `parsed_text`, `error`, `duration_ms` | 写文件用 `asyncio.create_task`，不阻塞主链路 |
| 008 State Cache | 默认透传 | **仅 `error is None` 时**更新缓存 | `name` (路由 handler), `args` (参数语义), `parsed_text` (结果语义) | 失败不更新，标记对应 toolset dirty |
| 011 Safety (未来) | 检查规则 → DENY 抛异常 | 空 | `name`, `args` | — |

### 修订段（2026-07-26，重构后）

> 以下内容为 Contracts 1-2 的修订注记，记录重构后实际语义与原始示例代码的差异。
> 原始示例代码保留不删，供历史对照。

**pre_call 实际语义。** 原始示例代码中 `pre_call` 异常仅记录日志、继续执行；实际实现中异常会 `break` 出拦截器链并**跳过实际 UE 调用**。这意味着 `pre_call` 具有阻断能力——抛出异常的拦截器可以阻止下游 tool call。

**结果解析已收敛。** 原始 `调用规范` 示例中的手动 JSON 解析和 `content[0].text` 提取已于 Issue 017 收敛为 `mcp_parse_result()` + `mcp_extract_text()` 两个公共函数。`ToolCallCompleted.parsed_text` 现在由 server.py 在 post 阶段之前统一解析一次（而非各 interceptor 自行解析）。

**拦截器链已外移。** 原始示例中拦截器列表在 `build_server()` 内硬编码；实际实现在 `cli.py:cmd_start()` 中组装后传入 `build_server(interceptors=...)`。当前拦截器链为：

```
DebugPreCallInterceptor → ReadbackInterceptor → ToolCallLogger
  → StateCacheInterceptor → DriftAlertInterceptor
  → VisionInterceptor → SnapshotRecorder
```

**ToolContext 字段变更。** 重构后（Issues 017-019）`ToolContext` 移除了 `stop_limit`、`ref_is_first_load`、`ref_mapping_generated` 字段，新增 `ref_session: ReferenceImageSession` 替代原 `_session_reference` 裸字典。

### 当前 pre_call 验证方式

003 和 008 暂时不需要 pre_call。为验证 pre_call 链路通畅，在 server.py 中注册一个简单的 debug interceptor：

```python
class DebugPreCallInterceptor(ToolCallInterceptor):
    """验证 pre_call 链路的临时拦截器。后续有真正需要 pre 的模块时替换掉。"""
    async def pre_call(self, name: str, args: dict) -> dict:
        logger.debug("[pre_call] 工具: %s, args keys: %s", name, list(args.keys()))
        return args
```

### 设计约束

1. **拦截器之间独立**。每个拦截器只依赖 `name` / `args` / `event`，不读取其他拦截器的状态。
2. **拦截器不修改返回结果**。`post_call` 返回 `None`，不能改变 `CallToolResult`。
3. **pre_call 可以拒绝调用**。通过抛异常来阻止后续执行（#011 使用此机制）。
4. **pre_call 的 args 修改向下传递**。`for` 循环中每个 `pre_call` 收到的是上一个修改后的 args。当前无模块需要修改 args，此路径作为预留。
5. **post_call 中抛异常不阻断主链路**。post 阶段的异常被 try/except 吞掉并打印日志——不应影响 LLM 收到结果。

---

## Contract 2: WorldState — 世界状态缓存模型

**涉及模块**：004（Tier 1 System Context 渲染）、008（L1 写穿透填充）

**契约状态**：✅ 已锁定

### 字段定义

```python
# harness/state/models.py

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Vector3(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class ActorSnapshot(BaseModel):
    """单个 Actor 的缓存快照。"""
    name: str
    class_name: str | None = None        # 如 "PointLight", "DirectionalLight"
    transform: dict | None = None        # {"location": \{...}, "rotation": \{...}, "scale": \{...}}
    properties: dict = Field(default_factory=dict)  # {"LightColor": "(1,0.5,0.3)", ...}
    deleted: bool = False
    last_updated: datetime | None = None


class WorldState(BaseModel):
    """UE 编辑器世界状态的完整缓存快照。

    刷新策略（ADR 0004）：
      L1 写穿透 — 覆盖的 write tool 调用成功后即时更新
      L2 读验证 — 按需（当前阶段暂不强制）
      L3 全量刷新 — 仅在 Hard Boundary 事件触发
    """

    map_path: str = ""
    actors: dict[str, ActorSnapshot] = Field(default_factory=dict)
    selected_actors: list[str] = Field(default_factory=list)

    # dirty 区分两个粒度：
    # - 覆盖 tool 写入但未做 L2 读验证的 Actor
    # - 完全未被覆盖的 toolset（我们连"改了哪个 Actor"都不知道）
    dirty_actors: set[str] = Field(default_factory=set)
    dirty_toolsets: set[str] = Field(default_factory=set)

    pie_running: bool | None = None       # None = 未知（无直接数据源，先占位）
    last_full_refresh: datetime | None = None
```

### 008 的 handler 如何更新缓存

所有更新发生在 `StateCacheInterceptor.post_call()` 中，且仅当 `event.error is None`。

每个 handler 接收 `(cache: WorldState, event: ToolCallCompleted)`，更新 `cache` 中的对应字段：

| Handler | 触发工具 | 更新缓存的方式 |
|---------|---------|--------------|
| `handle_set_transform` | `ActorTools.set_actor_transform` | `cache.actors[name].transform = args["xform"]` |
| `handle_set_properties` | `ObjectTools.set_properties` | merge `args` 中的 json 到 `cache.actors[name].properties` |
| `handle_add_to_scene` | `SceneTools.add_to_scene_from_class` / `add_to_scene_from_asset` | 从 `parsed_text` 提取新 Actor 名 → 加入 `cache.actors` |
| `handle_remove_from_scene` | `SceneTools.remove_from_scene` | `cache.actors[name].deleted = True` |
| `handle_set_label` | `ActorTools.set_label` | `cache.actors[name].label = args["label"]` |
| `handle_add_tag` | `ActorTools.add_tag` | `cache.actors[name].tags.append(args["tag"])` |
| `handle_remove_tag` | `ActorTools.remove_tag` | `cache.actors[name].tags.remove(args["tag"])` |
| `handle_add_component` | `ActorTools.add_component` | 标记 actor 需要 L2 验证 |
| `handle_remove_component` | `ActorTools.remove_component` | 标记 actor 需要 L2 验证 |
| `handle_set_parent_component` | `ActorTools.set_parent_component` | 更新组件关系 |
| `handle_select_actors` | `EditorAppToolset.SelectActors` | 替换 `cache.selected_actors` |
| `handle_load_level` | `SceneTools.load_level` | **触发 L3 全量刷新** |
| `handle_set_folder` | `SceneTools.set_actor_folder` | 更新 actor 文件夹属性 |
| `handle_rename_folder` | `SceneTools.rename_folder` | 标记 dirty_toolsets |
| `handle_delete_folder` | `SceneTools.delete_folder` | 标记 dirty_toolsets |

**未覆盖的 write tool**：如果 `event.name` 不匹配任何 handler，且它属于某个已知 toolset，则将那个 toolset 名加入 `cache.dirty_toolsets`。

### 004 的 Tier 1 如何消费

```python
# harness/context/prompt.py

def render_system_context(state: WorldState | None) -> str:
    """将 WorldState 渲染为 Tier 1 System Context 文本。

    state 为 None → 输出占位文本（008 未就绪时使用）。
    state 有值但 actors 为空 → 输出含警告的上下文。
    """
    if state is None:
        return (
            "当前 UE 状态：待 Harness #008 实现 State Cache\n"
            "请使用 SceneTools.find_actors 等工具手动查询场景状态。"
        )

    actor_count = sum(1 for a in state.actors.values() if not a.deleted)
    selected_str = ", ".join(state.selected_actors) or "无"

    lines = ["当前 UE 状态："]
    lines.append(f"- 地图：{state.map_path or '未知'}")
    lines.append(f"- PIE：{_pie_str(state.pie_running)}")
    lines.append(f"- 选中 Actor：{selected_str}")
    lines.append(f"- 场景 Actor 数：{actor_count}")

    if state.dirty_actors:
        lines.append(f"- ⚠ 以下 Actor 缓存可能过时（未做 L2 验证）：{', '.join(state.dirty_actors)}")
    if state.dirty_toolsets:
        lines.append(f"- ⚠ 以下工具集未受 State Cache 追踪，如需最新状态请手动查询：{', '.join(state.dirty_toolsets)}")

    return "\n".join(lines)


def _pie_str(pie: bool | None) -> str:
    if pie is None: return "未知"
    return "运行中" if pie else "已停止"
```

### 待定问题

- **PIE 状态**：architecture.md §1.2 标注 `SceneTools._is_pie()` 为私有方法，无法直接获取。当前先设 `None`。如果 004 渲染时看到 None，Tier 1 会显示"未知"——这会让 LLM 对 PIE 敏感操作保持谨慎。

- **Actor 属性的序列化深度**：`ObjectTools.set_properties` 接受的 JSON 参数和 `get_properties` 返回的数据都是嵌套结构。`ActorSnapshot.properties` 当前定义为 `dict`（浅 merge），复杂嵌套更新可能需要 pydantic 模型。当前 MVP 阶段先用 dict，后续按需加深。

---

## Contract 3: ContextProvider — 三层上下文提供者

**涉及模块**：004（Tier 1 / Tier 3）、005（Tier 2，未来）

**契约状态**：✅ 已锁定

### 接口定义

```python
# harness/context/provider.py

from abc import ABC, abstractmethod


class ContextProvider(ABC):
    """三层上下文管线的一个片段。

    每个 provider 产出自己的文本块，assembler 按 tier 分组后拼接。
    tier 决定注入顺序（1 → 2 → 3），同一 tier 内按 priority 排序。

    子类只覆写 render()，不覆写 tier/priority/enabled。
    """

    tier: int             # 1 = System, 2 = Task, 3 = Tool Reference
    priority: int = 0     # 同 tier 内的排序（值越小越前）
    enabled: bool = True  # 运行时可用条件关闭

    @abstractmethod
    def render(
        self,
        state: "WorldState | None",     # 来自 harness/state/models.py
        active_skill: dict | None,      # Skill YAML 解析后的 dict（005 负责填充）
    ) -> str: ...
```

### 各 Tier 的 provider 实现

**Tier 1 — System Context（始终存在）**

```python
class SystemContextProvider(ContextProvider):
    tier = 1
    priority = 0

    def render(self, state, active_skill):
        """Agent 身份 + State Cache 快照。state 为 None 时输出占位文本。"""
        agent_identity = (
            "你是一个运行在 Unreal Engine 5.8 中的 UE Editor Agent。\n"
            "你可以使用工具来控制 Unreal Editor。\n"
            "尽量使用截图验证你的修改。\n"
        )
        status = render_system_context(state)  # Contract 2 中定义的函数
        return agent_identity + "\n" + status
```

**Tier 2 — Task Context（Skill 匹配时注入，005 实现）**

```python
class TaskContextProvider(ContextProvider):
    tier = 2
    priority = 0

    def render(self, state, active_skill):
        """Skill 步骤 + 进度 + 工具白名单。active_skill 为 None 时返回空字符串。"""
        if active_skill is None:
            return ""
        # 005 负责实现详细渲染
        return _render_skill_context(active_skill)
```

**Tier 3 — Tool Reference（始终存在：工具名称 + 描述）**

```python
class ToolReferenceProvider(ContextProvider):
    tier = 3
    priority = 0

    def __init__(self, tool_list: list[dict]):
        self._tools = tool_list  # 已过滤的 UE 工具列表（来自 004 的 filter）

    def render(self, state, active_skill):
        """当前可用工具的名称和简述。LLM 可调 describe_toolset 获取完整 schema。"""
        lines = ["可用工具："]
        for t in self._tools:
            name = t.get("name", "")
            desc = t.get("description", "")[:120]  # 截断长描述
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)
```

### Assembler 的调用规范（004 在 server.py 中实现）

```python
# harness/context/assembler.py

def assemble_system_prompt(
    providers: list[ContextProvider],
    state: WorldState | None,
    active_skill: dict | None,
) -> str:
    """按 tier 分组、排序、渲染、拼接。"""
    enabled = [p for p in providers if p.enabled]
    # 按 tier 分组，同 tier 按 priority 排序
    enabled.sort(key=lambda p: (p.tier, p.priority))

    sections = []
    current_tier = 0
    for p in enabled:
        text = p.render(state, active_skill)
        if not text.strip():
            continue
        if p.tier != current_tier:
            sections.append("")  # tier 间空行分隔
            current_tier = p.tier
        sections.append(text)

    return "\n".join(sections).strip()
```

**server.py 注册：**

```python
# harness/server.py — build_server() 内

providers: list[ContextProvider] = [
    SystemContextProvider(),              # Tier 1 — 004 实现
    # TaskContextProvider() 留空         # Tier 2 — 005 实现后取消注释
    ToolReferenceProvider(filtered_tools), # Tier 3 — 004 实现
]
```

系统 prompt 在 LLM 每次工具选择前重新 assemble，确保反应最新的 WorldState 和活跃 Skill。

### 与 004 工具过滤的关系

ContextProvider（系统文本）和工具过滤（`tools/list` 返回值）是 004 的两个独立输出：

```
004 Context Assembly
├── 工具过滤 (filter.py)       → 影响 server.list_tools() 的返回值
└── 上下文组装 (prompt.py)     → 影响系统 prompt 文本
    ├── Provider: Tier 1 (System)
    └── Provider: Tier 3 (Tool Reference)
```

两者都依赖同样的输入（当前模式、活跃 Skill），但产出到 MCP 协议的不同通道。互不依赖对方的内部实现。

---

## 契约文件索引

| Contract | 文件 | 涉及模块 | 状态 |
|----------|------|---------|:---:|
| 1 — ToolCallInterceptor | `harness/interceptor.py` | 003, 008, 011 | ✅ |
| 2 — WorldState | `harness/state/models.py` | 004, 008 | ✅ |
| 3 — ContextProvider | `harness/context/provider.py` | 004, 005 | ✅ |
| 4 — LevelPersistenceToolset | UE 侧插件 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/` | 008, 014 | ✅ |

## 并行开发分工

并行阶段（方案 Z 的阶段 2），三个轨道独立开发：

| 轨道 | 模块 | 创建的文件 | 对着哪个契约 |
|------|------|-----------|:---:|
| 轨道 A | 004 | `harness/context/filter.py`, `harness/context/prompt.py`, `harness/context/assembler.py`, `harness/context/provider.py` | Contract 2（读 WorldState）、Contract 3（实现 Provider） |
| 轨道 B | 006 | `harness/verification/capturer.py`, `harness/verification/vision_agent.py` | 无契约依赖（独立 CLI，只消费 `call_tool`） |
| 轨道 C | 008 | `harness/state/models.py`, `harness/state/cache.py`, `harness/state/interceptor.py`, `harness/state/refresher.py` | Contract 1（实现 `ToolCallInterceptor`）、Contract 2（实现 `WorldState` + handlers） |

**集成点**（阶段 3）：
- `harness/server.py`：注册 `interceptors` 列表 + `providers` 列表
- `harness/config.py`：新增 `DEFAULT_TOOLS_ALLOWLIST` 配置项（004 使用）
- `harness/cli.py`：新增 `harness vision check` 子命令（006 使用）

---

## Contract 4: LevelPersistenceToolset — 关卡指纹与保存契约

**涉及模块**：008（Hard Boundary 指纹比对）、014（闭环验收）

**契约状态**：✅ 已锁定（UE 侧插件已实现，工具全限定名前缀 `LevelPersistenceToolset.LevelPersistenceToolset.`）

### 工具清单

| 工具 | 参数 | 返回值类型 | 用途 |
|---|---|---|---|
| `SaveCurrentLevel` | — | `FingerprintJSON` | 保存当前关卡 + 返回指纹 |
| `SaveAsset` | `AssetPath: string` | `FingerprintJSON` | 保存指定资产 + 返回指纹 |
| `SaveAll` | — | `SaveAllJSON` | 保存所有脏包 + 返回列表 |
| `ListDirtyPackages` | — | `JSON array of strings` | 查询所有脏包路径 |
| `GetLevelFingerprint` | `LevelPath: string`（传 `""`=当前关卡） | `FingerprintJSON`（含 `isLoaded: bool`） | 只读指纹，不保存 |

### FingerprintJSON schema

```json
// SaveCurrentLevel / SaveAsset 返回（status="saved" 时）:
{
  "status": "saved | partial | error",
  "packagePath": "/Game/Maps/MyLevel",
  "packageGuid": "4CE30229-49C3-2AE4-B86C-82AF5695A4EC",
  "filePath": "E:/Project/Content/Maps/MyLevel.umap",
  "fileSizeBytes": 12984,
  "lastModified": "2026-07-02T10:31:07.000Z",
  "actorCount": 145,
  "actorNameHash": "08c621d4",
  "externalActorPackages": 0,
  "externalActorsSaved": 0,
  "externalActorsFailed": 0
}

// GetLevelFingerprint 返回（已加载时）:
{
  "packagePath": "/Game/Maps/MyLevel",
  "packageGuid": "...",
  "filePath": "...",
  "fileSizeBytes": 12984,
  "lastModified": "2026-07-02T10:30:34.000Z",
  "isLoaded": true,
  "actorCount": 145,
  "actorNameHash": "08c621d4"
}

// GetLevelFingerprint 返回（未加载时）:
{
  "packagePath": "/Game/Does/Not/Exist",
  "packageGuid": "",
  "filePath": "...",
  "fileSizeBytes": 0,
  "lastModified": "",
  "isLoaded": false,
  "actorCount": null,
  "actorNameHash": null
}
```

### Harness 消费方式

**1. Hard Boundary 指纹比对：**

```python
# harness/state/refresher.py — 伪代码
async def check_fingerprint(ue_client, expected_fingerprint: dict | None) -> dict:
    """调用 GetLevelFingerprint，比对并返回 (match, current) 元组。"""
    result = await ue_client.call_tool(
        "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
        {"LevelPath": ""}
    )
    current = json.loads(result)  # FingerprintJSON

    if expected_fingerprint is None:
        return {"match": True, "current": current, "is_first_check": True}

    # 比对三个关键字段
    match = (
        current.get("packageGuid") == expected_fingerprint.get("packageGuid") and
        current.get("actorCount") == expected_fingerprint.get("actorCount") and
        current.get("actorNameHash") == expected_fingerprint.get("actorNameHash")
    )
    return {"match": match, "current": current}
```

**2. Dirty-diff 漂移检测：**

```python
# 记录 Harness 自己引发的 dirty 包
self._harness_dirty_packages: set[str]

# Hard Boundary 时：
current_dirty = set(await call_tool("ListDirtyPackages"))
current_dirty_filtered = {p for p in current_dirty if not p.startswith("/Script/")}

external_dirty = current_dirty_filtered - self._harness_dirty_packages
if external_dirty:
    # 发生了 Harness 外改动
    inject_drift_warning(external_dirty)
```

**3. Toolset 发现（Harness 启动时）：**

Harness 启动后应先 `load_toolset("LevelPersistenceToolset.LevelPersistenceToolset")` 确保工具可用，再执行业务调用。

### 工具返回值解析规则

ToolsetRegistry 将 `FString` 返回值包装为 `{"returnValue": "<the_json_string>"}`。Harness 解析时需：

```python
raw = json.loads(response_text)
inner_json_str = raw.get("returnValue", response_text)
data = json.loads(inner_json_str) if isinstance(inner_json_str, str) else inner_json_str
```

### 已知限制（见 ADR 0008）

- `actorNameHash` 只探测 Actor 增/删/改名，不探测 transform/属性/component 变化
- 属性级漂移依赖 dirty flag（未保存）+ mtime（已保存）间接捕获
- 三信号组合（hash + dirty + mtime）覆盖大部分实际场景，但 component 细节变更可能漏检
