# 023 — 评价指标修复：直方图相似度的结构性缺陷与后处理伪冷调检测

> 触发 Session: `420d8e34` — 模仿参考图氛围修改 UE 场景，5 小时 91 次工具调用，
> 直方图达到 0.86 但人眼主观视觉仍存在显著差异。根因分析见本文 §2。

## 现状进度表

| # | 修复项 | 类型 | 状态 |
|:--|:-----|:-----|:---:|
| P0-1 | 修复 build_atmosphere_mapping 全量 "Parameter error" | Bug | ✅ |
| P0-2 | ~~光照-后处理分歧检测~~ | 作废 | ❌ 被 P0-1 覆盖——build_atmosphere_mapping 修复后 LLM 可直接操作光源属性，分歧检测不再必要；State Cache 不存 LightColor、bUseTemperature 模式下数据不可靠。详见 2026-07-27 分析。 |
| P1-1 | 后处理过度使用警告（match_reference 输出注入） | 输出增强 | ⬜ |
| P1-2 | Vision vs 量化指标冲突时的升级逻辑 | 措辞修改 | ⬜ |
| P2-1 | match_reference 输出中引导 Skill 激活 | Prompt 注入 | ✅ |
| P2-2 | color-diagnostics Skill: 强化后处理优先级（五 Actor 顺序 + 互相影响警告） | YAML | ✅ |

---

## 1. Problem Statement

当前评价管线存在一条因果失效链，导致 LLM 在氛围匹配中产生虚假收敛：

```
build_atmosphere_mapping 返回全 Parameter error
  → LLM 无法有效操作光源属性（找不到 refPath／属性名）
    → 转向后处理做全局色彩映射（whiteTemp / saturation / sceneColorTint）
      → 后处理滤镜让直方图像素分布在统计上接近参考图
        → 直方图给出虚假高分（如 0.86）
          → LLM 被正面反馈强化，继续依赖后处理 → 死循环
```

核心缺陷：**直方图相似度对全局色彩映射（色温偏移、饱和度拉升、gamma 偏移）过于敏感，而对物理光照正确性无感知。** 暖光源 + 蓝色后处理滤镜可以产生和自然冷调场景几乎相同的像素值统计分布，但人眼能识别出"这不是自然光"——光源的光谱特征和后处理的人为偏移之间存在物理不一致性。

**注意**：本 Issue 不认为空场景（无 StaticMesh/Landscape）是问题。天空、雾气、光照方向、后处理的合理参数配置本身就可以逼近参考图的氛围。问题不在于场景空，而在于后处理被用作"光照的替代品"而非"氛围的补充"。

## 2. Root Cause Analysis

### 2.1 直方图失效的精确机制

```
参考图:  自然冷色温光照 → 像素值分布 P_ref
当前:   暖光源 + 后处理蓝滤镜 → 像素值分布 P_current

P_current ≈ T_postprocess( P_warm_lighting )

T_postprocess 是全局色彩映射：色温 10500K + 饱和度 2.0 + gamma_z=0.8 + sceneColorTint 偏蓝
这种极端参数可以系统性偏移所有像素值，使:
  similarity( P_ref, T_postprocess(P_warm) ) → 很高
  但 visually( 自然冷调 ) ≠ visually( 暖光 + 蓝滤镜 )
```

直方图只回答"像素值像不像"，不回答"光的来源对不对"。当后处理参数被推到极端值（whiteTemp > 8000K, saturation > 1.5）时，量化指标的可靠性急剧下降。

### 2.2 Session 420d8e34 的证据链

**光源从未变冷**：
- 初始 LightColor: `(r=1, g=0.89, b=0.38)` → R/B=2.63
- 最终 LightColor: `(r=0.85, g=0.82, b=0.78)` → R/B=1.09
- **蓝通道从未超过红通道**。DirectionalLight 始终是暖光。

**后处理制造了"伪冷调"**：
- whiteTemp: 6500 → 10500K
- saturation: 1.0 → 2.0
- sceneColorTint: 蓝通道逐步增强
- colorGamma: 蓝色通道 gamma 降低（提亮蓝通道）

**直方图给了虚假验证**：
- 从 0.71 → 0.04（过调崩溃） → 0.86（恢复后最佳）
- LLM 的推理: "直方图相似度达到了0.86，这是一个很好的结果！"
- 实际上：直方图 0.86 匹配的是"后处理滤镜输出"而非"物理光照正确性"

## 3. Solution

### 3.1 P0-1: 修复 build_atmosphere_mapping 全量 Parameter error

**症状**：Session 中 `build_atmosphere_mapping` 对全部 5 个氛围组件返回 `Parameter error`，19 个属性条目全部不可用。LLM 只能通过手动 `list_properties` → 逐层导航 `lightComponent`/`skyAtmosphereComponent`/`heightFogComponent` 来摸索 refPath 和属性名。

**影响**：这直接导致 LLM 无法高效操作光源属性，被迫转向它更容易操作的后处理参数（PostProcessVolume 的属性路径更短、更直观）。

**修复方向**：定位 `atmosphere.py` 中 `_resolve_component_properties` 或 `_build_property_index` 的逻辑错误，确保组件属性名和 refPath 正确提取。

**涉及文件**：`harness/verification/atmosphere.py`

### 3.2 P0-2: 光照-后处理分歧检测

**新指标：光源-画面色温分歧度**

在 `match_reference` 的输出中新增一行诊断：

```
光源-画面色温分歧度: 光源 R/B=1.09, 画面 R/B=1.39 | 分歧=0.30 ⚠
→ 画面看起来比光源实际更冷。后处理可能掩盖了光源的真实色温。
→ 考虑先调整 DirectionalLight.LightColor（增加蓝通道、降低红通道），而不是继续调后处理色温。
```

**数据来源**：
- **光源 R/B**：从 State Cache 中读取 DirectionalLight.LightColor，计算 `r / max(b, 0.001)`
- **画面 R/B**：已存在于 match_reference 的量化指标中
- **分歧度**：`|光源R/B - 画面R/B|`，阈值暂定 > 0.20 触发警告

**设计约束**：
- 仅在 DirectionalLight 存在且 LightColor 可读时计算
- 分歧度警告是**可操作的建议**而非阻断——LLM 仍然可以自行判断
- 警告文案提供具体的行动方向（"增加蓝通道"），符合 Harness 的"引导而非切断"哲学

**涉及文件**：`harness/verification/reference.py`（match_reference handler）

### 3.3 P1-1: 后处理过度使用警告

在 `match_reference` 输出中检测 PostProcess 参数是否超过合理阈值，并附加警告：

```
⚠ 后处理参数警告:
  whiteTemp: 10500K（自然日光范围 2000-8000K, 当前值远超此范围）
  saturation: 2.0（典型范围 0.5-1.5, 当前值极高）
  → 当前色调很大程度来自后处理滤镜。如果光源色温尚未匹配参考图，
    考虑先调整 DirectionalLight.LightColor 和 SkyAtmosphere 散射参数。
```

**检测规则**：
- `whiteTemp > 8000` 或 `whiteTemp < 2000` → 警告
- `colorSaturation (x) > 1.5` 或 `< 0.5` → 警告
- 以上任一触发即输出警告段

**涉及文件**：`harness/verification/reference.py`

### 3.4 P1-2: Vision vs 量化指标冲突时的升级逻辑

当前 match_reference 的输出中有一句：

> "⚠ MiMo 分析与量化指标方向一致 → 高置信；不一致 → 以量化指标为准。"

这句话在 Session 420d8e34 中起到了**反作用**——MiMo 说差距仍然存在，量化指标说直方图 0.86，LLM 选择了信任后者。

**修订为**：

> "⚠ MiMo 分析与量化指标方向一致 → 高置信。
> 不一致时 → 量化指标可能存在结构性偏差（如后处理滤镜模拟自然光照）。
> 调 vision_screenshot 做人工视觉确认，以 Vision 的 qualitative 观察判断
> 是否为「光照正确 + 微调」而非「后处理强行覆盖」。"

**涉及文件**：`harness/verification/reference.py`（输出模板字符串）

### 3.5 P2-1: match_reference 输出中引导 Skill 激活

在 match_reference 的输出末尾追加：

> "💡 建议调 `activate_skill('match-atmosphere')` 获取完整的参考图匹配工作流和颜色诊断决策树。"

利用 LLM 对 match_reference 输出高度信任的特点，在它注意力最集中的位置引导正确的行为路径。

Session 420d8e34 中 LLM 总共 91 次工具调用、6 次 match_reference——Skill 从未被激活。如果在第一次 match_reference 返回时就提示激活，流程可能完全不同。

**涉及文件**：`harness/verification/reference.py`（输出模板）

### 3.6 P2-2: color-diagnostics Skill 新增分歧度诊断项

在 [skills/color-diagnostics.yaml](skills/color-diagnostics.yaml) 的 Step D2（区分光照/后处理问题）表中新增一行：

```markdown
| 光源 R/B 与画面 R/B 差距 > 0.2 | 后处理滤镜掩盖了光源真实色温 |
  停止调整后处理 → 回到 DirectionalLight.LightComponent.LightColor
  → 把蓝通道推到 > 红通道（如 r=0.8, g=0.85, b=0.95）
```

纯 YAML 改动，不需要修改 Python 代码。

**涉及文件**：`skills/color-diagnostics.yaml`

## 4. Implementation Decisions

1. **P0 优先、P1 和 P2 可并行**：P0-1（Bug 修复）和 P0-2（分歧检测）是独立模块，可以同时开工。P1 和 P2 各项互不依赖。

2. **分歧检测不引入新工具**：光源 R/B 和画面 R/B 的计算逻辑嵌入现有 `match_reference` handler，作为输出的一部分，不需要新 MCP tool。

3. **阈值是经验值**：分歧度 0.20、whiteTemp 8000 上限、saturation 1.5 上限——这些阈值来自 Session 420d8e34 的数据点，后续可能需要根据更多 session 调优。在代码中用常量定义，方便调整。

4. **不引入阻断逻辑**：所有修复都是**信息注入**（在输出中追加警告和建议），不阻断 LLM 的后续动作。保持 Harness 的"引导而非切断"哲学。

5. **build_atmosphere_mapping 修复需要回归测试**：修改 `atmosphere.py` 后需跑对应测试确认映射表恢复正常，并验证 Session 420d8e34 中出现的全 Parameter error 不再复现。

## 5. Testing Decisions

- **P0-1**：在 `tests/` 下已有 atmosphere 相关测试，修复后确认映射表条目非空且 refPath 有效
- **P0-2**：新增测试验证分歧度计算：正常光源+无后处理（分歧度低），暖光源+冷后处理（分歧度高）
- **P1-1**：测试后处理参数在不同阈值边界处的警告触发/不触发
- **P1-2 + P2-1**：纯字符串模板改动，回归验证 match_reference 输出完整性
- **P2-2**：YAML 验证（validate_skill），无需 Python 测试

## 6. Out of Scope

- 不修改直方图算法本身（直方图在正常场景中是有效的，问题是它的"过度信任"而非算法错误）
- 不引入 SSIM 或其他图像质量指标替代直方图
- 不阻断 LLM 调整后处理（后处理本身是合法参数空间，只是不应用于"模拟光照"）
- 不修改 Interceptor 链

## 7. Further Notes

1. **场景空不是问题，后处理做假才是**：即使没有几何体，合理的天空颜色、雾气浓度、太阳角度、后处理微调也可以产生接近参考图的氛围。问题在于 LLM 把后处理从"微调"用成了"光照替代品"——分歧检测和过度使用警告正是为此设计。

2. **与已有 color-diagnostics Skill 的协同**：`color-diagnostics` 已包含 D3 优先级树（先调光、后处理最后）和 D2 区分规则（散射能否解释偏色）。P2-2 将新增的"光源-画面分歧"作为新诊断信号接入已有决策树——LLM 看到 match_reference 的分歧度警告后，激活 color-diagnostics 就能找到对应的行动指引。

3. **阈值数据来源**：当前阈值基于 Session 420d8e34 的单次观察。更多 session 数据积累后需要校准。
