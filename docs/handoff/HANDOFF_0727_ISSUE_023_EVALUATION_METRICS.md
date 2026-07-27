# Handoff: Issue 023 评价指标修复 + Pipeline 全链路补全 + Skill 系统强化

**日期**: 2026-07-27
**关联 Issue**: [docs/issues/023-evaluation-metrics-fix.md](../issues/023-evaluation-metrics-fix.md)
**Session 分析 Skill**: `C:\Users\Administrator\.claude\skills\harness-session-analyzer\SKILL.md`
**测试**: 395 passed, 1 pre-existing failure, 4 skipped
**Commit**: `405ce00`

---

## 1. 做了什么

### 1.1 P0-1: 修复 `build_atmosphere_mapping` 全量 `Parameter error`

**根因**：三层 bug 组成的失效链。

| 层 | Bug | 影响 | 修复位置 |
|:--|:----|:-----|:---------|
| ① | `_render_mapping_markdown` 和 fallback 路径用 `split(":")[-1]` 截断 refPath | LLM 拿到 `PersistentLevel.DirectionalLight_0.LightComponent0` 调 API → Parameter error | [atmosphere.py:348](harness/verification/atmosphere.py) |
| ② | `ref_extract_full_paths` 缺少 MCP content 信封解包（`{content:[{text:"..."}]}`） | `build_atmosphere_mapping` 扫描到 5 个组件但返回 0 个属性 | [normalize.py:292](harness/state/normalize.py) |
| ③ | `_parse_property_names` 不处理 `list_properties` 返回的 JSON 对象格式 | 属性名列变成 JSON 片段 `{"directionalLightComponent"` | [atmosphere.py:37](harness/verification/atmosphere.py) |

修复后 `build_atmosphere_mapping` 输出中 refPath 为全路径（可直接用于 API）、属性名为真实 UE 属性名（intensity, lightColor, rayleighScattering 等）。

### 1.2 MiMo 分类降级机制

原来 `classify()` 失败时 fallback 倾倒全部 1200+ 属性到 LLM 上下文。新增白名单降级：

```
classify() 成功 → MiMo 语义分类（智能化 9 维度筛选）
classify() 失败 → ATMOSPHERE_WHITELIST 硬编码筛选（零延迟，永远可用）
```

白名单 `ATMOSPHERE_WHITELIST` ([atmosphere.py](harness/verification/atmosphere.py)) 覆盖 9 维度 × ~50 个已知氛围属性名。诊断脚本 `tests/diag_classify.py` 可独立验证 MiMo 代理对纯文本分类的支持状态。

### 1.3 函数重命名：`ref_` 前缀统一

| 旧名 | 新名 | 影响文件 |
|:-----|:-----|:--------|
| `state_parse_ref_path` | `ref_parse_path` | normalize.py, interceptor.py, tests |
| `state_parse_actor_names` | `ref_parse_actor_names` | normalize.py, refresher.py, atmosphere.py |
| `_resolve_actor_list` | `_ref_resolve_list` | normalize.py (内部) |

新增 `ref_extract_full_paths()`：从 find_actors 返回值提取完整 refPath（不做短名截断），用于 API 调用。

### 1.4 LLM 指令强化：100% Skill 激活

**问题发现**：Session `ses_05d1` 中 LLM 5 次看到 match_reference 输出的 `💡 建议调 activate_skill` 全部无视。`skill_search` 返回空后 LLM 认为"没有匹配的 skill"。

**修复**：

| 文件 | 改动 |
|:-----|:-----|
| [cli.py:102-114](harness/cli.py#L102) | 顶部插入 REQUIRED 段："氛围匹配任务的第一个动作必须是 activate_skill"；明确说明外部 skill_search 找不到 Harness Skill |
| [cli.py:87](harness/cli.py#L87) | Skill 列表标题：`可用 Skill(触发词)` → `Harness Skill 列表(调 activate_skill 激活)` |
| [reference.py:500-505](harness/verification/reference.py#L500) | match_reference 输出："建议调" → "必须先调"，附带跳过后果说明 |

**验证**：Session `ses_05d0` 中 LLM **首次主动激活** match-atmosphere，并正确执行 Step 1.5（`bEnabled=false`）。

### 1.5 Skill 系统内容强化

**`match-atmosphere.yaml`**：

| 改动 | 说明 |
|:-----|:-----|
| Step 1.5 (新) | 强制关闭 PostProcessVolume (`bEnabled=false`)。未找到时用 `add_to_scene_from_class` 新建后关闭 |
| Step 1.5 → Step 4 闭环 | 完成时恢复 Volume (`bEnabled=true`)，微调边界 ±2000K / ±0.5 |
| Step 3 | 修正调整顺序：光源(1) → 天光(2) → 大气+云(3) → 雾(4)（原顺序是反向的） |

**`color-diagnostics.yaml`** (新建)：

- D1: 7 种偏色 × 最可能根因 × 优先动作对照表
- D2: 散射能否解释偏色（光照 vs 后处理区分）
- D3: 五 Actor 严格调整顺序 + 后处理互相影响警告 + 量化退出条件

### 1.6 Session 分析 Skill

`harness-session-analyzer` 位于 `C:\Users\Administrator\.claude\skills\harness-session-analyzer\SKILL.md`。五阶段检查单：

1. **元数据** — skills_activated 为首选检查项
2. **工具调用序列** — 错误模式、后处理滥用、顺序违规
3. **Vision 分析质量** — 置信度、警告、与量化指标冲突
4. **已知 Issue 交叉对照** — 与 CLAUDE.md 同步的故障模式表
5. **Skill 激活验证** — instructions.md + tool_calls.jsonl 交叉检查

---

## 2. 关键设计决策

### 2.1 白名单 vs. 修复 MiMo 代理

**决策**：白名单降级，不修代理调用。

**理由**：
- MiMo 代理 (`token-plan-cn.xiaomimimo.com/anthropic`) 对纯文本分类不稳定（诊断脚本验证当前可用，但 Session 中曾失败）
- 氛围属性名是 UE 引擎定义级常量，几乎不变
- 白名单零延迟、零外部依赖、永远可用

### 2.2 为什么不做 P0-2（光源-画面 R/B 分歧检测）

**分析发现**：
- State Cache 不存储 LightColor 值 → 需要额外 `get_properties` 调用
- `bUseTemperature=true` 模式下 LightColor 字段不可靠
- P0-1 修复后 LLM 可直接操作光源属性，分歧检测的原始动机已消除
- Session 420d8e34 中 LLM 不缺色温偏差信息，缺的是正确操作光源的能力

**标记**：P0-2 → ❌ 作废

### 2.3 为什么 refPath 截断存在过

原始设计意图：Markdown 表格显示可读性——每行不重复 `/Game/NewWorld.NewWorld:` 前缀。但对 LLM 来说映射表是拿来用的，不是拿来看的。全路径直接可用 > 显示简洁。

---

## 3. 已知故障模式速查表

在分析新 session 时，对号入座：

| 症状 | 根因 | 状态 |
|:------|:-----|:---:|
| `build_atmosphere_mapping` 全量 "Parameter error" | refPath 提取 Bug（三层） | ✅ 已修复 |
| `build_atmosphere_mapping` 5 组件全空 | MCP 信封解包缺失 | ✅ 已修复 |
| 映射表 refPath 以 `PersistentLevel.` 开头 | 截断 Bug | ✅ 已修复 |
| LLM 用截断 refPath → Parameter error | 同上 | ✅ 已修复 |
| Skill 未被激活（instructions.md 中有列表） | 指令措辞太弱（"建议"） | ✅ 已修复为 REQUIRED |
| Skill 未被激活（LLM 用 skill_search 搜不到） | 外部 skill_search 不索引 Harness Skill | ✅ 指令中已说明 |
| `classify()` 失败 → fallback 倾倒 1200 行 | MiMo 代理不稳定 | ✅ 白名单降级 |
| match_reference timeout (-32001) | Vision API 延迟 > 60s | ⬜ 待查 |
| Vision 重复报告 "场景为空/无被照表面" | LLM 在空场景调灯光（非本 Issue 范围） | ⬜ |

---

## 4. 涉及文件

### 新建

| 文件 | 行数 | 职责 |
|:-----|:--:|:-----|
| `docs/issues/023-evaluation-metrics-fix.md` | 203 | Issue 023 PRD + 进度表 |
| `skills/color-diagnostics.yaml` | 117 | 颜色诊断 Skill |
| `tests/diag_classify.py` | 99 | classify() 纯文本诊断脚本 |

### 修改

| 文件 | 改动 |
|:-----|:-----|
| `harness/cli.py` | `_build_instructions`: REQUIRED 段 + Skill 列表标题强化 + 移除 MiMo 特定措辞 |
| `harness/state/normalize.py` | `ref_parse_path`, `ref_parse_actor_names`, `_ref_resolve_list` 重命名；新增 `ref_extract_full_paths` + MCP 信封解包 |
| `harness/state/interceptor.py` | 导入 `ref_parse_path` |
| `harness/state/refresher.py` | 导入 `ref_parse_actor_names` |
| `harness/verification/atmosphere.py` | 全路径 refPath；`_parse_property_names` JSON dict 格式；`ATMOSPHERE_WHITELIST` + `_classify_by_whitelist`；MiMo → 白名单降级 |
| `harness/verification/reference.py` | match_reference 输出：激活提示从"建议" → "必须先调" |
| `skills/match-atmosphere.yaml` | Step 1.5(关闭 PP) + Step 3(修正顺序) + Step 4(恢复 PP) |
| `tests/test_build_atmosphere_mapping.py` | 更新 fallback 测试断言 |
| `tests/test_normalize.py` | ref_ 重命名 + 新增 `ref_extract_full_paths` 测试 |

---

## 5. 待验证 / 下一步

1. **重启 Harness 后验证全链路**：`build_atmosphere_mapping` → refPath 全路径 → `activate_skill("match-atmosphere")` → Step 1.5 关闭 PP → Step 3 逐组件调整 → match_reference 每轮验证 → Step 4 恢复 PP 微调

2. **MiMo 分类稳定性监控**：如果在多个 Session 中 `classify()` 持续失败，考虑直接移除 MiMo 分类路径，只用白名单

3. **match_reference timeout 排查**：Session `ses_05d0` 中出现 -32001 timeout。检查 Vision API 延迟曲线

4. **`ref_extract_full_paths` 的 MCP 信封解包是否正确**：在真正的 MiMo → Harness Session 中验证（非 MCP 直连测试）

5. **Skill 激活率持续观察**：如果 `_build_instructions` + `match_reference` 提示双重强化后仍有 Skill 未激活的情况，考虑在 `handle_match_reference` 中实现自动激活

---

## 6. Session 分析 Skill 使用指南

```
/skill harness-session-analyzer
```

指向 `.ue-harness/logs/<session_id>/` 目录，按五阶段检查单执行。

**典型使用场景**：
- MiMo 跑完一轮氛围匹配后，分析 LLM 的流程质量
- 部署新 Harness 代码后，验证 Skill 是否被正确激活
- 发现奇怪的工具调用序列时，诊断根因

**输出格式**：Markdown 诊断报告，每个问题引用 tool_calls.jsonl 的具体行号，附带 Harness 代码文件路径和修复建议。
