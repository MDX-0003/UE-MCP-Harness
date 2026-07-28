# 024 — Vision 提问质量 + Skill 强制激活 + match_reference 引导修正

> 触发 Session: `ses_059b` — MiMo 2.5-Pro 氛围匹配，match-atmosphere 已激活但存在
> 三方面偏差：color-diagnostics 未激活、vision_screenshot 提问过于简单、
> LLM 用 vision_screenshot 替代 match_reference 做氛围对比。

## 现状进度表

| # | 修复项 | 类型 | 状态 |
|:--|:-----|:-----|:---:|
| R1 | color-diagnostics 无条件强制激活 | Skill/Prompt | ⬜ |
| R2 | vision_screenshot 格式化提问模板 | 新功能 | ⬜ |
| R3 | match_reference 引导措辞修正 | 措辞修改 | ⬜ |
| — | 映射表属性去重 | Bug | ⬜ |

---

## 1. Problem Statement

Session `ses_059b` 中 match-atmosphere 已被成功激活，LLM 也使用了 `build_atmosphere_mapping`
和 `match_reference`。但在更细粒度上仍存在三方面偏差，导致调整质量未达预期：

1. **color-diagnostics 从未激活**：LLM 跳过了 match-atmosphere Step 2.5，
   直接进入批量调整。没有任何诊断框架指导它判断偏色根因，调整方向纯靠猜测。

2. **Vision 提问质量低下**：LLM 自创的 vision_screenshot 问题是简单的是非题
   （"天空颜色、雾气密度、整体色调是否接近蓝紫色黄昏氛围？"），
   Vision 返回 "符合预期" 后 LLM 即认为调整成功。
   实际上量化指标（R/B、直方图）每轮都在恶化。

3. **LLM 用 vision_screenshot 替代 match_reference**：match_reference 输出说
   "不要用 vision_ask 做氛围对比"，但 LLM 认为 vision_screenshot ≠ vision_ask，
   在第一轮调整后用了 vision_screenshot 而非 match_reference 做验证。

**额外发现**：映射表中同一属性在不同维度下出现多次重复条目（如 `intensity` 在亮度维度出现
3×2=6 次），`relativeRotation` 在阴影方向维度跨所有 5 个组件各出现 2 次。这是
`_classify_by_whitelist` 的一个 bug——同一属性被重复添加到同一维度。

## 2. Root Cause Analysis

### 2.1 color-diagnostics 未被激活

**直接原因**：LLM 跳过了 match-atmosphere Step 2.5。它执行了
`match_reference → activate_skill → build_atmosphere_mapping → 直接读取属性 → 批量调整`，
从未走到诊断步骤。

**深层原因**：
- Step 2.5 的条件是 "match_reference 报告颜色偏差时必须执行"——LLM 没意识到第一轮
  match_reference 的 MiMo 分析中色温 "cooler"、色调偏移 "偏红色" 就是颜色偏差信号
- get_context 和 SystemContextProvider 中只列出了 "氛围优先 → activate_skill('match-atmosphere')"，
  没有提 color-diagnostics
- 两个 Skill 之间只有 match-atmosphere → color-diagnostics 的间接引用，没有强制路径

### 2.2 Vision 提问过于简单

Harness 指令只说 `vision_screenshot(question="具体要验证什么？")`——"具体"两个字
不够。LLM 自创了一个闭合式是非题。Vision 回答了 "符合预期"，LLM 没有追问，
没有要求量化证据，直接接受了这个答案。

**证据**：2 次 vision_screenshot 调用（tool_calls.jsonl line 11, 24）的 question
都是 "是否符合预期" 型。Vision 两次都返回 "符合预期"，但 match_reference 的量化指标
（histogram 0.86→0.64, R/B 1.22→0.96）显示每轮都在恶化。

### 2.3 match_reference 引导不够精确

match_reference 输出说 "不要用 vision_ask 做氛围对比"，但 LLM 用了
vision_screenshot（不是 vision_ask）。LLM 的行为是合理的——它读到的指令
禁止 vision_ask，但没禁止 vision_screenshot。

### 2.4 映射表属性重复（附加 Bug）

`_classify_by_whitelist` 在遍历 property_index 时，同一个属性名（如 `intensity`）
因为出现在 component 级和 actor 级两套属性列表中（`_build_property_index` 的输出
同时包含 actor 顶层和 component 子对象的属性），导致每个匹配到的名称都被添加到
结果维度。Session 中 `intensity` 在亮度维度出现 6 次，`relativeRotation` 跨
5 个组件各出现 2 次。

## 3. Solution

### 3.1 R1: color-diagnostics 无条件强制激活

**不再是"遇到颜色偏差时才激活"，而是"涉及氛围调整就必须同时激活"。**

**改 1**：SystemContextProvider（[prompt.py:23](harness/context/prompt.py#L23)）的
工作模式段，在 `氛围优先 → activate_skill("match-atmosphere")` 之后追加：

```
  注意: activate_skill("match-atmosphere") 的同时也必须激活
  activate_skill("color-diagnostics")——后者提供颜色诊断决策树
  （症状→根因→修复优先级），是氛围匹配不可或缺的组件。
```

**改 2**：match-atmosphere Skill steps 的**开头**（Step 1 之前）加入：

```
  ## Step 0 — 同时激活诊断 Skill（强制）
  在开始任何氛围工作流之前，必须同时激活两个 Skill：
    activate_skill("match-atmosphere")  — 工作流控制
    activate_skill("color-diagnostics") — 颜色诊断决策树
  
  两个 Skill 互补：match-atmosphere 提供流程步骤，
  color-diagnostics 提供每个步骤中的诊断知识。
  只激活 match-atmosphere 是不够的——没有诊断框架，你会陷入试错调整。
```

**改 3**：match-atmosphere Step 2.5 从 "条件执行" 改为 "确认已激活"：
原 "调 activate_skill('color-diagnostics')" → 改为 "确认 color-diagnostics 已激活
（应在 Step 0 完成）。参考其决策树判断偏色根因。"

**涉及文件**：
- `harness/context/prompt.py` — SystemContextProvider
- `skills/match-atmosphere.yaml` — Step 0 + Step 2.5
- `harness/cli.py` — `_build_instructions` Skill 列表标题可追加提示

### 3.2 R2: vision_screenshot 格式化提问模板

**在 Harness 端预定义一套结构化模板——LLM 调用 vision_screenshot 时
自动注入标准提问格式。**

方案：在 `build_scene_context` 或 `vision_screenshot` handler 中，
当检测到当前 Session 是氛围匹配模式（skill 为 match-atmosphere 时），
在 question 上自动追加标准维度的检查项：

```
[Vision 提问格式]
当 Skill 为 match-atmosphere 时，vision_screenshot 的 question 应包含：
  1. 整体色调判定（冷/暖/中性）
  2. 天空颜色描述（渐变方向、主色调）
  3. 光源特征（颜色、强度、方向）
  4. 雾气/大气效果（密度、颜色）
  5. 与上一轮的视觉差异（如有）
  6. 与参考图的差距方向（更接近/更远离）
```

实现方式有两种选择：

**A. 纯 Prompt 注入**（推荐）——在 match-atmosphere Skill 的 steps 中
添加一段视力提问模板。LLM 读 Skill 时自然学到如何构造好问题。
不需要改 Python 代码。

**B. Harness 端自动注入**——在 vision_screenshot handler 中检测
活跃 Skill，自动 append 模板到 question。更可靠但侵入性更大。

先走方案 A。在 match-atmosphere Skill 的 Step 3 和 Step 4 的提到
vision_screenshot 处，追加模板说明。

**涉及文件**：
- `skills/match-atmosphere.yaml` — 追加 vision_screenshot 提问模板

### 3.3 R3: match_reference 引导措辞精确化

**改 1**：match_reference 输出中 "不要用 vision_ask 做氛围对比"
→ "每轮调整后用 match_reference 做量化对比——不要用 vision_screenshot
或 vision_ask 判断氛围变化。vision_screenshot 仅用于非参考图任务
的视觉验证。"

**改 2**：在 `在存在参考图的任务里，每轮迭代请使用 match_reference(...)`
段后追加 "调整后先调 match_reference 看量化指标，确认收敛方向后再用
vision_screenshot 做视觉确认。"

**涉及文件**：
- `harness/verification/reference.py` — match_reference 输出模板

### 3.4 附加: 映射表属性去重

`_classify_by_whitelist` 中，同一属性被多次匹配到同一维度时应去重。
在添加到 result[dim] 之前检查是否已存在同名同 refPath 的条目。

**涉及文件**：
- `harness/verification/atmosphere.py` — `_classify_by_whitelist`

---

## 4. Implementation Decisions

1. **R1 优先于 R2/R3**：color-diagnostics 强制激活是基础——没有诊断框架，
   再好的提问模板和引导也无法阻止 LLM 的试错调整。

2. **R2 走纯 Prompt 方案（方案 A）**：在 match-atmosphere Skill 中嵌入
   vision_screenshot 提问模板。不入代码——模板内容本质上是 Skill 知识，
   应随 Skill 版本迭代。

3. **三个 R 可并行**：互不依赖，各自修改独立文件。

4. **R1 的三处改动形成一个闭环**：
   - SystemContextProvider（get_context）→ 初次连接时告知
   - match-atmosphere Step 0 → 激活 Skill 时看到
   - Step 2.5 → 执行时的二次确认

---

## 5. Testing Decisions

- **R1**: `test_skill.py` 验证 match-atmosphere.yaml 语法有效；
  端到端：下次 MiMo Session 中确认两个 Skill 都被激活
- **R2**: YAML 验证（validate_skill）；端到端：观察 LLM 的
  vision_screenshot 问题是否包含模板中的维度
- **R3**: 纯字符串模板改动，回归验证 match_reference 输出完整性和测试
- **去重 Bug**: 单元测试验证 `_classify_by_whitelist` 返回去重后的条目

---

## 6. Out of Scope

- 不修改 vision_screenshot handler 的 Python 逻辑（走纯 Prompt 方案）
- 不在 build_atmosphere_mapping 中增加 PostProcessVolume 的 settings 子属性解析
  （那是白名单完善，不是本 Issue 范围）
- 不改变 match_reference 的 countdown 机制

---

## 7. Further Notes

1. **R1 的本质**：color-diagnostics 不是 match-atmosphere 的"子步骤"，
   而是平行的"诊断引擎"。当前设计把它当成了可选辅助（Step 2.5 "如颜色偏差"
   的条件激活），但实际使用中 LLM 根本不会主动判断"是否有颜色偏差"。
   改为无条件强制激活后，两个 Skill 在 LLM 的工作记忆中同时存在，
   诊断知识不再依赖于 LLM 的判断力。

2. **vision_screenshot 提问模板**的措辞关键是"开放性问题"而非"是否问题"——
   "描述天空的渐变方向" 而非 "天空是否为蓝紫色"。Vision 对开放式问题的回答
   通常包含更多可操作的细节。

3. **映射表去重**同时解决两个问题：减少 LLM 上下文浪费（107→约 40 个条目）、
   避免 LLM 对同一个属性的重复出现产生困惑。
