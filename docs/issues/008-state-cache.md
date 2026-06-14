# 008 — State Cache：Write-Through 世界状态

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

实现 Write-Through State Cache——Harness 维护 UE 编辑器世界状态的内存快照，通过拦截 write tool call 参数即时更新，而非轮询。

三层策略：
- **L1 写穿透**：硬编码 ~15 个高频 write tool handler，在转发给 UE 前从参数中提取变更语义，即时更新缓存
- **L2 读验证**：写操作后可选重查（由 Skill verification 策略控制，当前阶段暂不强制）
- **L3 全量刷新**：仅在 Hard Boundary 事件触发（首次连接、`load_level` 调用、重连、LLM 显式 `cache_refresh`）

未覆盖的 write tool 仅标记 `dirty_actors`，在 System Context 中告知 LLM，不自动触发全量刷新。

## 验收标准

- [ ] Harness 首次连接 UE → 自动执行全量 `find_actors(glob='*')` → 填充 State Cache
- [ ] `set_actor_transform(A, xform)` 调用后，缓存中 `A.transform` 即时更新，无需额外 MCP 调用
- [ ] `set_properties(A, json)` 调用后，缓存中 `A.properties` 合并新值
- [ ] `add_to_scene_from_class(...)` 成功后，新 Actor 自动加入缓存
- [ ] `remove_from_scene(A)` 调用后，缓存中 `A.deleted = True`
- [ ] System Context（Tier 1）包含缓存快照：地图路径、Actor 数量、选中 Actor、PIE 状态占位
- [ ] 未覆盖的 write tool（如 `NiagaraToolsets.*`）→ 将受影响的 toolset 标记 dirty → System Context 中显示："⚠ 缓存可能过时：[NiagaraToolsets]，如需最新状态请调相关查询工具"
- [ ] `load_level(path)` 调用被拦截 → 触发 L3 全量刷新
- [ ] Harness 与 UE 断连 → Agent Session 状态保存到磁盘 → 重连 → L3 全量刷新
- [ ] LLM 调 `cache_refresh` → L3 全量刷新

## 阻塞

- #002（工具透传——需要能拦截和转发 tool call）

## 设计说明

硬编码的 ~15 个 handler 覆盖以下工具集的所有 write 方法：
- **SceneTools**：`add_to_scene_from_class`、`add_to_scene_from_asset`、`remove_from_scene`、`load_level`、`set_actor_folder`、`rename_folder`、`delete_folder`
- **ActorTools**：`set_actor_transform`、`set_label`、`add_tag`、`remove_tag`、`add_component`、`remove_component`、`set_parent_component`
- **ObjectTools**：`set_properties`
- **EditorAppToolset**：`SelectActors`

Handler 模式：
```python
@write_handler("ActorTools.set_actor_transform")
def handle_set_transform(self, params: dict, result: dict):
    actor_name = params["actor"]["name"]
    self.cache.actors[actor_name].transform = params["xform"]
```
