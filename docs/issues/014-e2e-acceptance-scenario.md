# 014 — 闭环验收场景：30 步任务 + 中途 Harness 外干预

**类型：** 需要人工配合（中途手动干预步骤由人执行）

## 要构建什么

本 Issue 是核心 demo：一个**可重复执行**的端到端验收场景，证明 Harness 的完整闭环——act → L2 结构化验证 → Vision 视觉验证 → checkpoint 保存 → 轨迹记录。"要不要做下去"只能对着具体的高强度场景回答，本场景就是那个标尺。

## 场景脚本

1. **Setup**：UE 运行中（加载测试关卡），Harness 运行中，Claude Code 作为 LLM 客户端接入 :9000。
2. **任务下达**：用户说"把场景改成黄昏氛围"（复用 evening-lighting Skill），预期 20-30 次工具调用。
3. **中途干预**（人工，在任务进行到约一半时）：在 UE 编辑器里手动删除一个 Actor、手动修改一盏灯的属性——不经过 Harness。
4. **预期行为**：
   - Harness 在下一个 Hard Boundary 通过指纹比对 / dirty-diff 检测到漂移；
   - System Context 出现漂移警告，LLM 被告知"世界在会话外被改动"；
   - LLM 重新观测（find_actors / get_actor_transform），修正认知后继续任务；
   - 写操作经 L2 读回验证（值级 diff），最终效果经 Vision 验证；
   - 任务完成后调用 `SaveCurrentLevel` 落盘；
   - 全程轨迹可用 `harness replay` 回放。
5. **数字采集**（demo 的说服力来源）：token 消耗、工具调用数、漂移检测事件时间点、L2/Vision 验证通过率——从 observability（stats/replay）导出。

## 前置开发（按顺序）

| # | 内容 | 预估 |
|:---:|------|:---:|
| 1 | LevelPersistenceToolset 接入 Hard Boundary：连接/任务始末/重连时调 `GetLevelFingerprint`；dirty-diff 检测（记录 Harness 自己引发的 dirty，过滤 `/Script/` 噪声包，多出的项即外部改动）；System Context 增加漂移警告行 | 1-2 天 |
| 2 | L2 读回验证：写操作 post_call 后重查实际值，与意图值 diff（ADR 0004 承诺、ADR 0008 升级为正确性主通道） | ~1 天 |
| 3 | 跑通验收场景 + 采集数字 + 录屏 | ~1 天 |

## 验收标准

- [ ] Hard Boundary（连接/任务始末/重连）自动执行指纹比对，结果写入 WorldState
- [ ] 手动删除 Actor / 改属性后，下一个 Hard Boundary 检测到漂移（dirty-diff 或指纹失配）
- [ ] 漂移警告出现在 `get_context` 返回的 System Context 中，LLM 据此重新观测
- [ ] `/Script/` 前缀的 dirty 包被过滤，不产生误报
- [ ] L2 读回验证：`set_actor_transform` 后读回值与意图值一致才算步骤成功
- [ ] 任务完成后 `SaveCurrentLevel` 成功，返回的指纹成为新基准
- [ ] `harness replay` 可完整回放本次任务，含漂移检测事件
- [ ] 产出物：demo 录屏 + stats 数字（token/调用数/验证通过率）

## 已知限制（接受，见 ADR 0008）

- `actorNameHash` 探测不到 transform/属性级修改——依赖 dirty flag（未保存）+ mtime（已保存）间接兜住。
- 干预时机依赖人工，场景不能全自动化回归——demo 属性决定这是可接受的。

## 阻塞

- ADR 0008（信任模型倒转）✅ 已记录
- Issue 007（验证闭环）✅ / Issue 012（重连）✅ / LevelPersistenceToolset 五工具 ✅（UE 侧已验证，见 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/test_tools.py`）

## 涉及文件

| 文件 | 改动 |
|------|------|
| `harness/state/refresher.py` | Hard Boundary 处增加指纹获取与比对 |
| `harness/state/models.py` | `WorldState` 增加 `level_fingerprint: dict \| None`、`drift_detected: bool` |
| `harness/state/interceptor.py` | 记录 Harness 自身引发的 dirty 包集合 |
| `harness/verification/interceptor.py` | 增加 `ReadbackInterceptor`（L2 读回验证，与 VisionInterceptor 同模块） |
| `harness/context/prompt.py` | `_render_state_snapshot` 增加观测时间戳 + 漂移警告行 |
| `tests/test_state.py` | 指纹比对、dirty-diff、`/Script/` 过滤测试 |
| `tests/test_verification_interceptor.py` | ReadbackInterceptor 测试 |
