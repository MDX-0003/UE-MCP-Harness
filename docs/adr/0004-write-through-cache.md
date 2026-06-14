# 0004 — State Cache 采用 Write-Through 策略，非轮询刷新

**背景：** State Cache 需要维护 UE 编辑器世界状态的内存快照。两个极端方案：（1）每次 LLM 需要时全量 `find_actors(glob='*')`——对大型关卡太重；（2）定期轮询——不知道何时该轮询。

**决策：** Harness 采用 Write-Through Cache（写穿透缓存）三层策略。因为 Harness 拦截所有 tool call，它天然知道每次修改了什么。

**三层策略：**

**L1 — 写穿透（即时，零额外开销）：**
Harness 在将 write tool call 转发给 UE 之前，从参数中提取变更语义，直接更新缓存：
- `set_actor_transform(Actor_0, xform={...})` → `cache.actors["Actor_0"].transform = xform`
- `set_properties(Actor_0, "{LightColor: (1.0, 1.0, 0.0)}")` → `cache.actors["Actor_0"].properties.LightColor = (1.0, 1.0, 0.0)`
- `add_to_scene_from_class(PointLight, "CafeLight_1", ...)` → 成功后从返回值提取新 Actor → 加入缓存
- `remove_from_scene(Actor_0)` → 标记 `cache.actors["Actor_0"].deleted = true`

**L2 — 读验证（按需，选择性）：**
写操作完成后，如果当前 Skill 定义了 verification，仅对修改的 Actor 做选择性重查：
- `set_actor_transform(Light_0, ...)` 成功 → 可选 `get_actor_transform(Light_0)` 确认值已生效
- 由 Skill 的 `verification` 策略控制，不是每次都做

**L3 — 全量刷新（仅在 Hard Boundary 事件触发）：**
触发条件严格限制为以下四种：
1. Harness 首次连接 UE（初始状态快照）
2. `load_level()` 调用检测（地图切换 → 世界状态全量失效）
3. Harness 与 UE 重连（连接中断后恢复 → 状态可能不一致）
4. LLM 显式请求 `cache_refresh`

未覆盖的 write tool（硬编码 handler 之外的 ~140 个工具）**仅标记缓存为 dirty，不自动触发全量刷新**。dirty 信息在下一轮的 System Context 中告知 LLM："缓存可能过时，如需最新状态请调 find_actors"。

**实施：**
- `harness/state/interceptor.py` — 硬编码 ~15 个高频 write tool 的语义 handler（SceneTools.*, ActorTools.*, ObjectTools.* 中的写操作）
- 每个 handler 知道：影响了哪个 Actor、修改了什么字段、如何更新缓存模型
- 未被 handler 覆盖的 write tool → 将受影响的 Actor 加入 `dirty_actors` 集合 → 在 System Context 中报告

**后果：**
- State Cache 的维护成本接近零（写穿透不需要额外 MCP 调用）
- "缓存可能不一致"的风险被明确追踪和报告
- P5 Transaction & Undo 从 MVP 移除——Write-Through Cache 在每次写操作前已记录旧值，轻量回退可行但非 MVP 必需
