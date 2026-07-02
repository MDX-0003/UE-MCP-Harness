# 013 — State Cache 磁盘持久化与恢复

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

WorldState（[models.py](../harness/state/models.py)）当前是纯内存对象——Harness 进程崩溃后缓存全丢，重连后必须执行昂贵的 L3 全量刷新（调 N 次 `find_actors` + `get_current_level`）。本次实现 pydantic 自带序列化到 `~/.ue-harness/sessions/{id}.json`，Harness 启动时自动加载，崩溃后从磁盘恢复。

与 Issue 012（[012-connection-health.md](issues/012-connection-health.md)）的关系：012 解决 UE 崩溃后重连，通过 `full_refresh()` 从 UE 重新拉数据。013 解决 Harness 自身崩溃——从磁盘加载上次快照，减少甚至跳过 L3 全量刷新。两者互补。

## Q1: WorldState 当前有哪些内容

### WorldState 顶层字段（[models.py:39-58](../harness/state/models.py#L39-L58)）

| 字段 | 类型 | 默认值 | 内容 |
|------|------|--------|------|
| `map_path` | `str` | `""` | 当前加载的地图路径，如 `/Temp/Untitled_1` |
| `actors` | `dict[str, ActorSnapshot]` | `{}` | 以 Actor 名为 key 的快照字典 |
| `selected_actors` | `list[str]` | `[]` | 编辑器当前选中的 Actor 名列表 |
| `dirty_actors` | `set[str]` | `set()` | L1 写入但未做 L2 验证的 Actor |
| `dirty_toolsets` | `set[str]` | `set()` | 未被 handler 覆盖的 toolset（缓存可能漂移） |
| `pie_running` | `bool \| None` | `None` | PIE 状态，`None` = 未知 |
| `last_full_refresh` | `datetime \| None` | `None` | 上次 L3 全量刷新的时间戳 |
| `last_vision_verdict` | `dict \| None` | `None` | 最近一次视觉验证结果（Issue 007） |
| `_needs_refresh` | `bool` | `False` | `load_level` 后标记，触发 L3 刷新（不持久化） |

### ActorSnapshot 字段（[models.py:26-36](../harness/state/models.py#L26-L36)）

| 字段 | 类型 | 内容 |
|------|------|------|
| `name` | `str` | Actor 名称（作为 actors dict 的 key） |
| `class_name` | `str \| None` | 类名，如 `"DirectionalLight"` |
| `transform` | `dict \| None` | `{"location": {...}, "rotation": {...}, "scale": {...}}` |
| `properties` | `dict` | `{"LightColor": "(1,0.5,0.3)", ...}` — L1 write-through 合并填充 |
| `label` | `str \| None` | 编辑器标签 |
| `tags` | `list[str]` | 标签列表 |
| `components` | `list[str]` | 附加的组件名列表 |
| `deleted` | `bool` | 是否已被删除（软删除，不移出 dict） |
| `last_updated` | `datetime \| None` | 最后更新时间 |

**序列化能力：** 全部字段（除 `_needs_refresh` 和 `dirty_actors`/`dirty_toolsets` 中的 `set` 需转 `list`）都是 pydantic 原生可序列化的——`model_dump_json()` 一行即可。

## Q2: 读写路径逐文件分析

### 写入路径（谁改 WorldState）

```
┌─ cli.py:cmd_start() ──────────────────────────────┐
│ _cache = WorldState()         ← 创建空实例          │
│ 传给 build_server() → 全局共享                     │
└────────────────────────────────────────────────────┘
    │
    ├─► StateCacheInterceptor.post_call()  [state/interceptor.py:35]
    │     L1 写穿透：每次 UE write tool 成功返回后，
    │     从 event.args 提取变更语义，即时更新缓存。
    │     15 个 handler 覆盖的写入：
    │       actors[name].transform    ← set_actor_transform
    │       actors[name].properties   ← set_properties
    │       actors[name]              ← add_to_scene / remove_from_scene
    │       actors[name].label        ← set_label
    │       actors[name].tags         ← add_tag / remove_tag
    │       actors[name].components   ← add_component / remove_component
    │       actors[name].*            ← set_parent_component (dirty)
    │       selected_actors           ← SelectActors
    │       map_path + _needs_refresh ← load_level
    │       dirty_actors/dirty_toolsets ← set_folder/rename/delete_folder
    │
    ├─► full_refresh()  [state/refresher.py:22]
    │     L3 全量刷新（Hard Boundary 事件触发）：
    │       map_path        ← get_current_level()
    │       actors[name]    ← find_actors("*") — 空壳 ActorSnapshot
    │       selected_actors ← GetSelectedActors()
    │       dirty_* 清空
    │       last_full_refresh ← 当前时间戳
    │
    └─► VisionInterceptor.post_call()  [verification/interceptor.py:62]
          last_vision_verdict ← VisionSubAgent.check() 返回的
          {"pass": bool, "reason": str, "adjustment": str, "at": iso}
```

### 读取路径（谁读 WorldState）

```
SystemContextProvider.render()  [context/prompt.py:39]
  │  Tier 1 System Context：
  │  读取 map_path, actors (计数+名称), selected_actors,
  │       dirty_actors, dirty_toolsets, pie_running
  │  → 渲染为 LLM 可读的状态快照文本
  │
  └─► assemble_system_prompt()  [context/prompt.py:169]
        └─► server.py get_context handler  [server.py:321]
              → CallToolResult → MCP SDK → LLM 收到

TaskContextProvider.render()  [context/prompt.py:124]
  │  读取 _active_skill（非 WorldState，但从同一上下文传入）
  │  输出 Skill 步骤 + 进度 + 工具白名单

VisionInterceptor.post_call()  [verification/interceptor.py:62]
  │  读取 _active_skill.verification.expected → 传给 VisionSubAgent
  │  写入 last_vision_verdict → 供 SystemContextProvider 下次渲染

SnapshotRecorder  [observability/snapshotter.py]
  │  读取 map_path, last_vision_verdict → 写入 session.json
  │  读取 _cache.model_dump_json() → 完整快照归档
```

## Q3: 持久化方案

### 保存时机

**方案：每次 L1 写穿透后保存。** 理由：
- StateCacheInterceptor 每次写入后 WorldState 已是最新，此时保存无额外数据一致性开销
- 不额外增加 I/O——写穿透本身已有内存操作，加一次磁盘写入即可
- 简单且不会漏——不需要维护"脏"标记来决定要不要保存

**具体做法：** `StateCacheInterceptor.post_call()` 末尾，handler 执行成功后，调用 `_maybe_persist(cache, session_id)`。内部做简单的去抖动——当前秒内已保存过则跳过（避免高频工具调用时刷盘风暴）。

**额外保存点：**
- Skill `deactivate_skill` 时（任务自然结束）
- Harness 进程收到 SIGINT/SIGTERM 时（优雅关闭）
- VisionInterceptor 写入 `last_vision_verdict` 后（视觉验证结果不应丢失）

### 加载时机

**Harness 启动时**（[cli.py:97](../harness/cli.py#L97) 的 `run()` 入口），在 `full_refresh()` 之前：

```python
# 尝试从磁盘恢复
session_file = Path.home() / ".ue-harness" / "sessions" / f"{session_id}.json"
if session_file.exists():
    try:
        _cache = WorldState.model_validate_json(session_file.read_text())
        logger.info("从磁盘恢复 State Cache: %s", session_file)
        # 可选：异步触发一次 L3 全量刷新来比对差异
        asyncio.create_task(full_refresh(ue_client, _cache))
    except Exception as e:
        logger.warning("State Cache 恢复失败: %s", e)
        _cache = WorldState()
else:
    _cache = WorldState()
```

### 文件格式

```json
// ~/.ue-harness/sessions/05551ee9.json
{
  "map_path": "/Temp/Untitled_1",
  "actors": {
    "DirectionalLight_0": {
      "name": "DirectionalLight_0",
      "class_name": null,
      "transform": {
        "rotation": {"pitch": -75.0, "yaw": 43.73, "roll": 112.36}
      },
      "properties": {
        "LightColor": {"r": 1.0, "g": 0.7, "b": 0.4},
        "Intensity": 2.5
      },
      "deleted": false
    }
  },
  "selected_actors": [],
  "dirty_actors": [],
  "dirty_toolsets": [],
  "pie_running": null,
  "last_full_refresh": "2026-07-01T15:49:29Z",
  "last_vision_verdict": {
    "pass": true,
    "reason": "光照角度正确，阴影长度符合预期",
    "adjustment": null,
    "at": "2026-07-01T15:52:00Z"
  }
}
```

### `dirty_*` 的 set→list 转换

pydantic 默认将 `set` 序列化为 JSON 不支持的格式。在 `model_dump_json()` 前将 `dirty_actors` 和 `dirty_toolsets` 转为 `list`，加载时转回 `set`：

```python
cache.dirty_actors = set(cache.dirty_actors)
cache.dirty_toolsets = set(cache.dirty_toolsets)
```

或直接在 WorldState 上覆盖 `model_dump` 做转换。

### 不持久化的字段

| 字段 | 理由 |
|------|------|
| `_needs_refresh` | 临时标记，仅 `load_level` 后一个生命周期有效 |
| `last_vision_verdict` | **应持久化**——视觉验证结果有价值，恢复后 LLM 能看到上次验证结论 |

## Q4: 持久化数据何时作为"历史记录"覆盖现有 state

这个问题本质是：**磁盘快照和 UE 实时状态谁更可信？**

| 场景 | 磁盘快照 | UE 实时状态 | 决策 |
|------|----------|------------|------|
| Harness 启动，UE 正在运行 | 有，但可能过时 | `full_refresh()` 可获取 | **加载后异步刷新**：先用磁盘数据让 LLM 立即工作，后台 L3 刷新更新差异 |
| Harness 启动，UE 未运行 | 有 | 不可达 | **纯磁盘恢复**：没有 L3 可用，磁盘数据是唯一的 |
| Harness 崩溃重启，UE 仍在运行 | 有（崩溃前的） | 当前真实状态 | **加载后强制刷新**：崩溃前快照可能不完整，反正 UE 活着 |
| UE 和 Harness 同时崩溃后重启 | 有 | 可获取 | **加载 + 刷新**：同上 |
| 用户首次启动 Harness | 无 | 可获取 | **纯 L3 刷新**：没有磁盘文件 |

**核心原则：磁盘数据是"急救包"，不是"权威源"。** UE 编辑器是唯一的真实状态来源。磁盘快照的价值在于：
- Harness 启动时能给 LLM 一个即时可用的上下文（不等 L3 刷新）
- Harness 崩溃后恢复上次的视觉验证结论和 Actor 属性
- 作为 L3 刷新的基准——对比新旧差异，只更新变化的部分

**不会被覆盖的情况：**
- `last_vision_verdict`——这是 Harness 独有的数据，UE 侧没有对应信息，磁盘恢复是最佳路径
- Actor `properties`——L3 刷新不填充属性（只建空壳），L1 写穿透填充的属性依赖磁盘恢复

**会被覆盖的情况：**
- `map_path`、`actors` 列表、`selected_actors`——L3 刷新从 UE 重新拉取后会覆盖
- `dirty_*`——L3 刷新后清空

## 验收标准

- [ ] Harness 正常退出 → `~/.ue-harness/sessions/{id}.json` 文件写入
- [ ] Harness 进程崩溃 → 下次启动自动从磁盘加载最近一次快照
- [ ] 磁盘快照加载后 → LLM 调 `get_context` 立即看到缓存内容（不等 L3）
- [ ] L3 全量刷新后 → Actor 列表与 UE 一致，磁盘文件更新
- [ ] `last_vision_verdict` 跨 Harness 重启保留
- [ ] `deactivate_skill` 时触发一次保存
- [ ] SIGINT/SIGTERM 时触发最后一次保存
- [ ] 旧 session 文件不影响新 session（启动时按 session_id 匹配文件）
- [ ] 新增测试：`tests/test_state_persistence.py` — save/load round-trip, 崩溃恢复模拟

## 阻塞

- Issue 008（State Cache）✅ 已就绪
- Issue 012（连接健康）✅ 已就绪——重连后 L3 刷新的持久化协同

## 涉及文件

| 文件 | 改动 |
|------|------|
| `harness/state/models.py` | `WorldState` 增加 `dirty_*` 的序列化适配（set→list） |
| `harness/state/interceptor.py` | `post_call()` 末尾增加 `_maybe_persist()` |
| `harness/cli.py` | `run()` 入口增加磁盘加载逻辑；shutdown 增加保存 |
| `harness/server.py` | `deactivate_skill` 增加保存 |
| `tests/test_state_persistence.py` | 新增 round-trip + 崩溃恢复测试 |

## 测试策略

```
tests/test_state_persistence.py

class TestSaveLoadRoundTrip:
    async def test_save_and_load_identical(self):
        """序列化 → 反序列化 → 字段全等"""
    async def test_empty_cache_round_trip(self):
        """空 WorldState 保存加载"""
    async def test_populated_cache_with_actors(self):
        """含 Actor + properties + verdict 的完整 round-trip"""
    async def test_dirty_sets_serialized_as_lists(self):
        """dirty_actors/dirty_toolsets 在 JSON 中为数组"""

class TestCrashRecovery:
    async def test_loads_from_disk_when_file_exists(self):
        """磁盘有文件 → 启动时加载而非创建空 WorldState"""
    async def test_fresh_start_when_no_file(self):
        """磁盘无文件 → 创建空 WorldState"""
    async def test_corrupted_file_graceful_fallback(self):
        """JSON 损坏 → 警告日志 + 回退到空 WorldState"""

class TestPersistenceTriggers:
    async def test_saved_after_write_handler(self):
        """StateCacheInterceptor 写入后触发保存"""
    async def test_debounce_prevents_storm(self):
        """高频写入时去抖动不重复保存"""
    async def test_saved_on_deactivate_skill(self):
        """Skill 停用时触发保存"""
    async def test_saved_on_shutdown(self):
        """SIGTERM 时触发最后一次保存"""
```
