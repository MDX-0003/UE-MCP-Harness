# 016 — L2 读回验证 (ReadbackInterceptor) + 参考图对比

**状态：** ✅ 已完成（2026-07-14 交付：Part A ReadbackInterceptor + Part B 参考图匹配 + PLAN_0714 v3 倒计时停止机制）
**依赖关系：** Part B 依赖 Part A —— 参考图循环的"逐步执行"阶段需要廉价、确定性的每步验证器，否则每步都要烧一次 Vision API。

## 动机

三个事实叠加，暴露出系统的验证空洞：

1. **PLAN_0707 之后 Vision 不做二元判定**（`VisionVerdict.pass_ = None`），职责收窄为审美/整体效果判断（ADR 0008 的验证分工："像不像黄昏"归 Vision）。
2. **L1 write-through 记录的是写入意图，不是写入事实。** `StateCacheInterceptor` 从工具调用的*入参*更新缓存——UE 侧静默失败（属性名 no-op、值被 clamp、部分应用）时，缓存记下一条没发生过的观测。这是 ADR 0008 要杀死的"数据冒充事实"在写方向上的变体。指纹校验管"会话外有没有人改世界"，管不了"我自己的写有没有生效"。
3. **现在的兜底是 LLM 自觉执行 instructions 里的验证 SOP 第一步**（手动调 get 读回）——每次多一轮调用 + token，且 LLM 经常跳过。

结论："灯的 pitch 是不是真的写成了 15 度"这类值级问题，目前没有任何机制自动回答。ReadbackInterceptor 把这条 SOP 从"求 LLM 遵守"下沉为"Harness 自动执行"。这也是 ADR 0004 承诺、ADR 0008 升格为**正确性验证主通道**（确定性、零 Vision 成本）的 L2 读回，是 ADR 0007 grill 清单里最后一个未兑现的核心机制。

## 实施前 UE 源码审查（2026-07-08）

审查了 `UE_5.8/Engine/Plugins/Experimental/ToolsetRegistry/Content/Python/toolset_registry/toolsets/core/` 下的 UE MCP 工具实现源码。关键发现：

| 写工具 | UE 源码行为 | L2 读回判断 |
|--------|------------|:---:|
| `set_properties` ([object.py:80-92](object.py#L80-L92)) | 返回裸 `bool`，透传 C++ `set_object_properties`。不返回实际写入值。 | ✅ **必要** |
| `set_actor_transform` ([actor.py:115-134](actor.py#L115-L134)) | **无条件 `return True`**。无论值是否被 UE clamp，永远报告成功。 | ✅ **必要** |
| `set_label` ([actor.py:29-40](actor.py#L29-L40)) | `return actor.get_actor_label() == label` — UE 内部已做读回验证。返回 `True` 当且仅当值实际生效。 | ❌ 冗余 |
| `add_to_scene_from_class` ([scene.py:91-112](scene.py#L91-L112)) | 返回 `unreal.Actor`（创建的 actor 对象）；失败时 assert 抛异常。无需额外确认存在性。 | ❌ 冗余 |
| `add_to_scene_from_asset` ([scene.py:116-139](scene.py#L116-L139)) | 同上，返回创建的 actor。 | ❌ 冗余 |

**结论**：L2 白名单收窄为两条映射——每一条都有 UE 源码证据支撑其必要性，其余因 UE 侧已内置验证而移除。

**额外确认：**
- **Component 级读回**：`set_properties` 和 `get_properties` 共用同一个 `instance` 参数解析路径（`_get_instance_object`），refPath 支持 `SpotLight_0.LightComponent0` 组件子路径。读回可精确到组件级属性。
- **值格式对齐风险低**：两个工具都走 `unreal.ToolsetLibrary.set/get_object_properties`，同一套 C++ API 的 setter/getter 共享序列化逻辑。`get_actor_transform` 和 `set_actor_transform` 同样共享 MCP 传输层的 `Transform` 序列化。

---

## Part A — ReadbackInterceptor（L2 读回验证）

### 设计

- **位置**：`harness/verification/interceptor.py`，与 VisionInterceptor 同模块（沿用原 Issue 014 的规划）。挂在拦截器链中 StateCache 之后：
  `DebugPreCall → ToolCallLogger → StateCache → Readback → DriftAlert → VisionInterceptor → SnapshotRecorder`
- **触发**：白名单映射「写工具 → 读回工具 + 字段提取器」。经 UE 源码审查后，最终白名单：
  - `set_actor_transform` → `get_actor_transform`（location/rotation/scale）
  - `set_properties` → `get_properties`（按写入的属性名子集读回，支持 component 级 refPath）
- **流程**：`post_call` 中读回实际值，与意图值（复用 `normalize_tool_args` 归一化后的入参）做 diff。浮点比较带分类型容差（Transform 用 1e-3 默认值；Properties 按值类型分派——数值型浮点容差，字符串/颜色型精确匹配）。
- **红线遵守**：post_call 不改变 tool call 结果、异常不阻断主链路。失配结论写入 WorldState 观测 + JSONL 日志，由 `server.py` 经现有徽章通道（Vision 徽章同款路径）向 LLM 呈现一行警告，例如：`⚠ L2 读回失配: rotation.pitch 意图=15.0 实际=0.0`。读回调用自身失败时同样发出徽章：`⚠ L2 读回失败: get_properties(Actor_X) 超时 —— 缓存值未经证实`。
- **读回调用不经过拦截器链**（直接走 ue_client），避免递归触发；读回取得的实际值显式回写 WorldState，顺带修正 L1 的意图性观测——这是缓存里第一批"读回确认过的事实"。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `harness/verification/interceptor.py` | 新增 `ReadbackInterceptor`（写工具白名单 + diff 逻辑） |
| `harness/state/normalize.py` | 复用 `NormalizedCall.component_name` 组装 component 级读回请求 |
| `harness/server.py` | 读回失配/失败徽章（复用 Vision 徽章模式） |
| `harness/cli.py` | 拦截器链注册 |
| `tests/test_verification_interceptor.py` | 读回命中/失配/容差/白名单外跳过/读回自身失败不阻断/component 级读回 |

### 验收标准

- [ ] `set_actor_transform` 后自动读回，值级 diff 通过才静默；失配时 LLM 在下一条结果里看到徽章警告
- [ ] `set_properties` 后自动读回（含 component 级 refPath），仅对写入的属性名子集做 diff
- [ ] 浮点容差内的偏差不告警；容差外（clamp/no-op）告警
- [ ] 白名单外的工具（含 `set_label`、`add_to_scene_from_*`）零开销跳过
- [ ] 读回调用自身失败（超时/异常）不阻断主链路，**且向 LLM 发出徽章警告**
- [ ] 读回实际值回写 WorldState，观测标记为"已确认"
- [ ] 全量测试通过，新增 case 覆盖上述路径

**预估：** ~1 天（映射数减半但 diff 逻辑复杂度不变）

---

## Part B — 参考图对比

### 目标

用参考图替代口头表述来指定开发方向：用户提供一张目标效果图，LLM 驱动 Vision 回答"参考图与当前场景的区别"，差异以结构化形式返回，由主 LLM 拆解为可执行步骤，逐步执行（每步经 Part A 的 L2 读回确认），末尾再对比收敛。

### 分工约束（已确认）

- **主 LLM 无多模态**——参考图只能进 Vision 管线。
- **Vision 不认识 Harness 的工具词汇表**——Vision 只产出结构化差异（differences），**拆步骤留在主 LLM**（语言任务 + 需要工具知识）。

### 设计草案（形态细节待 grill 确认，见下）

- **入口**：`vision_screenshot` / `vision_ask` 增加参考图关联（参数级 vs Session 级绑定，待定）。
- **Vision 侧**：`vision_agent.py` 消息组装支持双图（现状截图 + 参考图）；`VisionVerdict` 统一 JSON 格式（PLAN_0707 的 `response_format` 基座）增加 `differences` 字段（schema 待定）。
- **存储**：参考图归档进 session 目录，随 Vision 会话生命周期管理（`session.py`）。
- **循环纪律**：新增 Skill YAML（如 `match-reference`）固化：对比 → 拆步骤 → 执行 → L2 读回 → 收尾再对比（终止条件待定）。

### 待定项（grill 进行中，确认后回填本节）

| # | 问题 | 候选 |
|---|------|------|
| 1 | 入口形态 | vision_screenshot 加 `reference` 参数 / 新工具 `vision_compare` / Vision Session 级绑定（绑定后 session 内所有对比自动带图） |
| 2 | 参考图如何进入系统 | LLM 传文件路径参数 / 约定目录（用户丢文件） / 任务开始时 CLI 指定 |
| 3 | differences schema | 自由文本列表 / `{aspect, current, target}` / 加 priority 与可行动建议 |
| 4 | 循环终止判定 | Vision 相似度评分 + 阈值 / 固定最大轮数 / 用户确认 / 主 LLM 自行判断 |

### 涉及文件（初估）

| 文件 | 改动 |
|------|------|
| `harness/verification/vision_agent.py` | 双图消息组装 + differences 输出格式 |
| `harness/verification/session.py` | 参考图存储与会话关联 |
| `harness/server.py` | 参数透传 / 工具 schema |
| `skills/` | `match-reference` Skill YAML |
| `tests/test_verification.py` | 双图组装、differences 解析 |

**预估：** ~2 天（形态确认后修订）

---

## 不做的事（本 Issue 范围外）

- 不给主 LLM 加多模态通道——架构上主 LLM 保持纯文本。
- 不让 Vision 输出工具级操作步骤——它不认识工具词汇表，拆步骤是主 LLM 的职责。
- 不做参考图的自动检索/生成——参考图由用户提供。
- 原 Issue 014 demo 不在本 Issue 内（已降级，见 014 头部说明）。
