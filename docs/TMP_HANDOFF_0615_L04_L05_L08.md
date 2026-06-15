# 004 + 005 + 008 现状 Handoff — 2026-06-15

## 测试总览

```
004 Context Assembly:  21 tests  ✅
005 Skill System:       31 tests  ✅
008 State Cache:        18 tests  ✅
全量回归:             135 tests  ✅
```

---

## 004 Context Assembly

### 核心文件

| 文件 | 职责 |
|------|------|
| `harness/context/filter.py` | `apply_filter()`: 子串匹配白名单 + 逃生通道 |
| `harness/context/provider.py` | `ContextProvider` 抽象基类 |
| `harness/context/prompt.py` | 3 个 Provider 实现 + `assemble_system_prompt()` |
| `harness/config.py` | `default_tools_allowlist`: 5 个 toolset 前缀 |
| `harness/server.py` | `list_tools()` 注入过滤 + context_providers 注册 |

### 运行时流程

```
LLM 调 tools/list
  → server.list_tools()
    → _rebuild_tool_reference()
      → ue_client.list_tools() → 211 个原始工具（每次调都查 UE，无缓存）
      → apply_filter(raw_tools, config.default_tools_allowlist)
        → 子串匹配 5 个模式: EditorAppToolset./SlateInspector./SceneTools./ActorTools./ObjectTools.
        → + 逃生通道: list_toolsets / describe_toolset
        → Skill 模式: + Skill 的 tools_allowlist
      → 返回 ~20 个工具
    → 追加 Harness 自有工具: activate_skill / save_skill
  → LLM 收到过滤后的工具列表
```

### 测试覆盖

| 测试类 | 覆盖内容 |
|------|------|
| `TestApplyFilter` (6) | 逃生通道、allowlist 匹配、allowlist 为空、extra_allowed、工具名判断 |
| `TestSystemContextProvider` (5) | null state 占位、空 state、填充 state、dirty 警告、PIE 状态 |
| `TestTaskContextProvider` (2) | null skill 返回空、有 skill 渲染 |
| `TestToolReferenceProvider` (2) | 工具描述渲染、长描述截断 |
| `TestAssembler` (6) | 空列表、单 provider、tier 排序、priority、disabled 跳过、全管线 |

### 运行时风险

| # | 风险 | 严重度 | 复现条件 | 后果 |
|:---:|---|---|---|---|
| R1 | **组装后的 system prompt 从未传给 LLM** | 🔴 高 | 始终存在 | `server._harness_assemble_prompt` 只是一个 debug 属性，MCP 协议中并没有将组装后的 context 文本注入到 LLM 的 system prompt 的机制。LLM 收到的 system prompt 由其 MCP 客户端（Claude Code）自行管理。**004 的 Context Assembly 实际只生效了工具过滤部分（list_tools），三层 prompt 文本没有被消费。** |
| R2 | **`_rebuild_tool_reference()` 每次调用都查 UE** | 🟡 中 | LLM 频繁调 tools/list | 每次 tools/list 都走 `ue_client.list_tools()` → HTTP 往返，没有缓存。UE 端工具列表在 session 内不会变（除非有 BroadcastToolsListChanged），可以缓存但没做。 |
| R3 | **`SystemContextProvider` 占位文本已过时** | 🟡 中 | 008 已实现但 state 为 None 时 | 代码写的是 "当前 UE 状态：待 Harness #008 实现 State Cache"，但 008 已落地。当 WorldState 未传入时（当前就是未传入——因为 `_harness_assemble_prompt` 从未被真正调用），会输出这条过时的占位信息。 |
| R4 | **`apply_filter` 子串匹配可能误伤** | 🟢 低 | 工具名巧合包含 allowlist 模式 | 匹配逻辑是 `pattern in name`（子串匹配），如果某个工具名包含 "SceneTools" 但并非 SceneTools 下的工具，会被错误放行。反转同样：如果 Skill 的 tools_allowlist 用的短名和 UE 全限定名不包含公共子串，工具不会被放行。 |

---

## 005 Skill System

### 核心文件

| 文件 | 职责 |
|------|------|
| `harness/context/skill_registry.py` | `SkillRegistry`: YAML 解析 + CRUD + 匹配引擎 + 验证 |
| `harness/server.py` | `activate_skill` + `save_skill` 两个 Harness 自有 MCP 工具 |
| `harness/cli.py` | `harness skill create/list/delete/update` |
| `skills/evening-lighting.yaml` | 预置 Skill 模板 |

### 运行时流程

```
harness start
  → SkillRegistry().load_skills()
    → 首次运行: 复制 skills/evening-lighting.yaml → ~/.ue-harness/skills/
    → 扫描所有 .yaml → 构建 {name: SkillInfo} 缓存
    → 1 个 Skill 就绪

LLM 调 activate_skill("黄昏")
  → skill_registry.match_skill("黄昏")
    → 遍历所有 SkillInfo: name + description + triggers 做子串匹配
    → 命中 evening-lighting (triggers 包含 "黄昏")
  → _active_skill = {name, description, triggers, tools_allowlist, steps}
  → 返回: "Skill 'evening-lighting' 已激活..."

  下轮 tools/list:
    → extra_allowed = frozenset(_active_skill["tools_allowlist"])
    → apply_filter(..., extra_allowed=extra) → 仅返回 10 个 Skill 白名单工具

LLM 调 save_skill("coffee-shop", yaml_content)
  → validate_skill(yaml_content) → {"name", "triggers", "steps"} 非空检查
  → 写入 ~/.ue-harness/skills/coffee-shop.yaml
  → 刷新缓存

harness skill create coffee-shop
  → skill_registry.create_template("coffee-shop")
  → 写入模板 YAML → 打开系统编辑器
```

### 匹配引擎详解

`match_skill(query)` 对每个 Skill 搜索三个字段：
- `name`（如 `evening-lighting`）
- `description`（如 `"将场景光照调整为黄昏/傍晚氛围"`）
- `triggers`（如 `["黄昏", "傍晚", "sunset lighting", "dusk"]`）

匹配规则：`query.lower()` in `field.lower()` — 大小写不敏感子串匹配。

| 输入 query | 命中 | 理由 |
|-----------|------|------|
| `"黄昏"` | ✅ | trigger 匹配 |
| `"evening"` | ✅ | name 子串匹配 |
| `"光照调整"` | ✅ | description 子串匹配 |
| `"sunset"` | ✅ | trigger 匹配 |
| `"coffee"` | ❌ | 无匹配 |
| `""` | ❌ | 空查询直接返回 `[]` |

### 测试覆盖

| 测试类 | 覆盖内容 |
|------|------|
| `TestParseSimpleYaml` (5) | 基本字段、literal block、列表、注释、空值 |
| `TestParseYamlList` (3) | dash items、引号剥离、空列表 |
| `TestValidateSkill` (5) | 有效/缺少 name、triggers、steps、空 triggers |
| `TestSkillRegistry` (17) | 空目录、list、save+load、delete、get、load_yaml、模板创建、无效保存；匹配 9 种场景 |
| `TestBuiltinTemplate` (1) | 模板通过验证 |

### 运行时风险

| # | 风险 | 严重度 | 复现条件 | 后果 |
|:---:|---|---|---|---|
| R5 | **自定义 YAML 解析器的局限性** | 🔴 高 | Skill YAML 含复杂语法 | `_parse_simple_yaml()` 不处理：嵌套字典、`key: value` 中 value 含 `:` 的字符串（如 `expected: "场景: 黄昏"` 会把 `"场景` 视为 key）、多级缩进列表、YAML 锚点/引用。如果用户手动写的 YAML 超出解析器能力，Skill 加载可能静默丢失字段。 |
| R6 | **`_active_skill` 激活后无取消机制** | 🟡 中 | LLM 激活了一个 Skill 后想退出 | 没有 `deactivate_skill` 工具。一旦 `_active_skill` 被设置，`tools/list` 始终返回 Skill 白名单，LLM 无法回到自由探索模式（除非重启 Harness）。需要的逃生通道：`activate_skill("")` 或 `deactivate_skill()` → 设 `_active_skill = None`。 |
| R7 | **`save_skill` 无重复名检查** | 🟡 中 | LLM 保存同名 Skill | 直接覆盖文件，不提示、不备份。 |
| R8 | **外部 YAML 变更不感知** | 🟡 中 | 用户手动在 ~/.ue-harness/skills/ 添加/修改文件 | `SkillRegistry` 只在 `load_skills()` 时扫描目录。添加新文件后不会自动出现在列表中，需要重启 Harness。 |
| R9 | **TaskContextProvider 输出与 004 同命运** | 🟡 中 | 同 R1 | `server._harness_assemble_prompt` 未被实际消费，所以即使 Skill 激活了，Tier 2 的步骤指令也不会传给 LLM。LLM 只能通过 `activate_skill` 的返回值看到 Skill 信息，然后自行按步骤执行。**这不是 bug——LLM 确实收到了 Skill 信息，只是没有通过 prompt 注入的形式。** |

---

## 008 State Cache

### 核心文件

| 文件 | 职责 |
|------|------|
| `harness/state/models.py` | `WorldState` + `ActorSnapshot` + `Vector3` pydantic 模型 |
| `harness/state/interceptor.py` | `StateCacheInterceptor` + 15 个 write handler |
| `harness/state/refresher.py` | L3 全量刷新 |
| `harness/cli.py` | 注册 `StateCacheInterceptor` + 启动时 L3 刷新 |

### 运行时流程

```
harness start
  _cache = WorldState()                           ← 创建空缓存
  cache_interceptor = StateCacheInterceptor(_cache) ← 创建拦截器
  interceptors = [DebugPreCall, ToolCallLogger, StateCacheInterceptor]
  ...
  full_refresh(ue_client, _cache)                 ← L3: 首次连接
    → get_current_level() → cache.map_path
    → find_actors(glob='*') → cache.actors = {...}
    → GetSelectedActors()  → cache.selected_actors

运行时 LLM 调 set_actor_transform(Light_0, xform):
  server.call_tool("set_actor_transform", {"actor": {"name": "Light_0"}, ...})
    → pre interceptors
    → ue_client.call_tool(...) → UE 执行 → 返回成功
    → post interceptors:
        ToolCallLogger → 写 JSONL
        StateCacheInterceptor.post_call(event)
          → _build_handlers()["toolset_registry...set_actor_transform"] → _handle_set_transform
          → cache.actors["Light_0"].transform = {xform: {...}}  ← 即时更新
```

### Handler 覆盖矩阵

| Handler | 覆盖工具 | 缓存字段 | L2 验证 |
|---------|---------|---------|:---:|
| `_handle_set_transform` | `set_actor_transform` | `.transform` | — |
| `_handle_set_properties` | `set_properties` | `.properties` (merge) | — |
| `_handle_add_to_scene` | `add_to_scene_from_class/asset` | 新建 `ActorSnapshot` | — |
| `_handle_remove_from_scene` | `remove_from_scene` | `.deleted = True` | — |
| `_handle_set_label` | `set_label` | `.label` | — |
| `_handle_add_tag` | `add_tag` | `.tags.append` | — |
| `_handle_remove_tag` | `remove_tag` | `.tags.remove` | — |
| `_handle_add_component` | `add_component` | `.components` + dirty | ✅ |
| `_handle_remove_component` | `remove_component` | `.components` + dirty | ✅ |
| `_handle_set_parent_component` | `set_parent_component` | dirty | ✅ |
| `_handle_select_actors` | `SelectActors` | `.selected_actors` | — |
| `_handle_load_level` | `load_level` | 清空缓存 | L3 触发 |
| `_handle_set_folder` | `set_actor_folder` | dirty | — |
| `_handle_rename_folder` | `rename_folder` | `dirty_toolsets` | — |
| `_handle_delete_folder` | `delete_folder` | `dirty_toolsets` | — |

### 测试覆盖

| 测试类 | 覆盖内容 |
|------|------|
| `TestWorldState` (2) | 默认状态、deleted 不计入 count |
| `TestHandlers` (12) | 每个 handler 独立测试：更新、合并、新建、标记 |
| `TestStateCacheInterceptor` (3) | post_call 正常路径、error 跳过、未覆盖标记 dirty |
| `TestHelpers` (2) | `_is_write_tool` 判定、`_extract_toolset` 提取 |

### 运行时风险

| # | 风险 | 严重度 | 复现条件 | 后果 |
|:---:|---|---|---|---|
| R10 | **Handler 路由表与实际 UE 工具名不匹配** | 🔴 高 | UE 工具名不符合生成的命名规则 | `_build_handlers()` 通过笛卡尔积生成全限定名：`toolset_registry.toolsets.core.{module}.{ToolsetName}.{short_name}`（9 种组合 × 15 个 short_name = 135 个 key）。但 UE 实际工具名可能使用不同的大小写、下划线、或完全不同的命名空间前缀。**L3 测试已验证 Python 工具名格式是 `toolset_registry.toolsets.core.scene.SceneTools.find_actors`（小写模块名 + 驼峰工具名），但 handler 表中的方法名（如 `set_actor_transform` vs `set_actor_transform`）必须精确匹配——如果 UE 用的是 `SetActorTransform`，就不会命中。** 匹配不上 → 标记 dirty → LLM 看到警告但缓存不更新。 |
| R11 | **L3 刷新 partial failure 不报错** | 🟡 中 | UE 部分工具调用失败 | `full_refresh()` 对三个步骤都做了 try/except，单步失败只记 warning。如果 `find_actors(glob='*')` 失败了但后面两步成功，缓存里只有选中的 Actor 列表，所有其他 Actor 丢失。 |
| R12 | **`_handle_load_level` 只清空，不重刷新** | 🟡 中 | LLM 调 `load_level` | Handler 清空 `cache.actors` + `cache.selected_actors` + dirty 标记。但**没有触发新的 L3 刷新**。刷新依赖外部调用 `full_refresh()`，而当前只有 `cli.py:cmd_start` 在启动时调了一次。`load_level` 之后缓存是完全空的，直到下次手动刷新或 LLM 逐个工具写入才会重新填充。 |
| R13 | **`_is_write_tool` 启发式误判** | 🟢 低 | 工具名含 set_/add_/remove_ 但不是写操作 | 如 `get_actor_bounds` 不含任何 write 关键词 → 正确判断为读。但 `SelectActors` 单独处理了。潜在的误报：没有已知案例。 |
| R14 | **缓存与 UE 状态漂移** | 🟡 中 | 用户在 UE 编辑器里手动操作（拖拽 Actor、删除、添加） | Harness 只追踪通过 MCP 工具调用的变更。用户在 UE 编辑器里的直接操作对缓存不可见。ADR 0004 认为这是可接受的权衡——"L3 全量刷新仅在 Hard Boundary 事件触发"。 |
| R15 | **WorldState 通过 SystemContextProvider 传给 LLM 的路径不通** | 🟡 中 | 同 R1 | 即使缓存数据准确，LLM 也看不到——因为 `assemble_system_prompt` 的返回值没有被 MCP 协议消费。**当前 LLM 看不到 Tier 1 的状态快照**，除非它通过 `tools/list` 可见的工具名称或 `call_tool` 的返回值推导。 |
| R16 | **并发工具调用的缓存一致性** | 🟢 低 | LLM 同时发两个 write tool call | `StateCacheInterceptor.post_call` 对 `WorldState` 做 in-place 修改，没有锁。如果两个调用同时命中同一个 Actor 的不同字段（transform + properties），理论上可能丢失一个更新。**但 UE 在 game thread 上串行处理 MCP 调用（handoff 文档 §2.5），所以两个调用的 SSE 响应天然是序列化的——实际上不存在真正的并发。** |

---

## 跨模块交叉风险

| # | 风险 | 涉及模块 | 说明 |
|:---:|---|---|---|
| R17 | **Context Assembly 的 prompt 输出无人消费** | 004 + 005 + 008 | 三个模块都为此投入了资源（`SystemContextProvider` / `TaskContextProvider` / `assemble_system_prompt` / WorldState 渲染），但**当前没有 MCP 协议通道将组装后的系统文本传给 LLM**。这些代码不是无用——它们为 #007（Verification Loop 中的 System Context 注入）和未来的 MCP prompt 集成做好了准备——但现阶段 LLM 不会看到它们。 |
| R18 | **Harness 自有工具与 UE 工具的命名冲突** | 005 | `activate_skill` 和 `save_skill` 不与任何 UE 工具名冲突，但没有机制保证这一点。如果未来 UE 插件注册了同名工具，Harness 的处理器会优先拦截，UE 工具永远不可达。低风险——UE 不太可能注册这两个名字的工具。 |
| R19 | **`_active_skill.tools_allowlist` 用短名 vs UE 全限定名** | 004 + 005 | `evening-lighting.yaml` 的 `tools_allowlist` 写的是短名（如 `SceneTools.find_actors`），但 `apply_filter` 做的是 `pattern in name` 子串匹配，其中 `pattern` 来自 `extra_allowed`。如果 UE 全限定名是 `toolset_registry.toolsets.core.scene.SceneTools.find_actors`，子串 `SceneTools.find_actors` 可能不直接等于任意一段子串……但实际上 `"SceneTools.find_actors" in "toolset_registry...SceneTools.find_actors"` 是 True，所以这个能工作。但如果 Skill 写了 `ActorTools.SetActorTransform`（大小写不同），就不会匹配 `actor.ActorTools.set_actor_transform`。 |

---

## 测试覆盖不到的运行时路径

| 路径 | 涉及的模块 | 为什么测试不到 |
|------|:---:|---|
| `harness start` 完整启动 → LLM 连接 → 实际调 `tools/list` 看过滤结果 | 004 | 需要运行中的 UE + MCP SSE 客户端（类似 L3 测试） |
| LLM 调 `activate_skill` → 再看 `tools/list` 变化 | 004 + 005 | 需要完整 MCP 会话 |
| `save_skill` → 检查文件落盘 → 重启 Harness → Skill 仍在 | 005 | 需要跨进程验证 |
| `set_actor_transform` 被 LLM 调用 → `StateCacheInterceptor` 更新 → `WorldState` 变化 | 008 | 需要运行中的 UE |
| `load_level` → 缓存清空 → 后续 tool call 看不到旧 Actor | 008 | 需要运行中的 UE |
| Vision + Skill + Cache 联合：激活 Skill → 改 Actor → 截图 → Vision 判断 | 005+006+008 | #007 才做 |

---

## 建议的修复优先级

| 优先级 | 修复 | 涉及风险 |
|:---:|------|:---:|
| P0 | 实现 MCP prompt 通道——让 `assemble_system_prompt` 的输出到达 LLM | R1, R9, R15, R17 |
| P0 | 添加 `deactivate_skill` 或 `activate_skill("")` 清除活跃 Skill | R6 |
| P1 | `_handle_load_level` 后自动触发 L3 刷新 | R12 |
| P1 | Handler 路由表对照 UE 实际工具名做一次全量校验 | R10 |
| P2 | `_rebuild_tool_reference()` 加缓存 | R2 |
| P2 | `save_skill` 加重复名提示 | R7 |
| P2 | 替换 `_parse_simple_yaml` 为完整 YAML 解析器（pyyaml） | R5 |
| P3 | 更新 `SystemContextProvider` 占位文本 | R3 |
