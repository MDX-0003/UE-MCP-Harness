# 0006 — Vision Sub-Agent 独立上下文 + 结构化返回 + 可追问

**背景：** P3 Screenshot Verification Loop 是 Harness 与 Coding Agent 的本质差异。LLM 不应该自己看截图——这会占用主 Agent 宝贵的 context window（截图是 heavy content block）。Vision 判断需要独立的上下文、独立的推理能力，并能向主 Agent 追问。

**决策：** Vision 验证由独立的 Vision Sub-Agent 执行。

**Vision Sub-Agent 设计：**

**输入（来自 Harness Verification Engine）：**
- 截图：base64 PNG 图片（直接从 `FToolsetImage.Data` 解码，零转码）
- 预期描述：来自 Skill YAML 的 `verification.expected` 字段（如"场景具有温暖的低角度光照和长阴影"）
- 当前步骤上下文：来自 Task Memory 的 `current_step` + `completed` + `pending`
- 置信度阈值：来自 Skill YAML 的 `verification.tolerance`（默认 0.7）

**输出（结构化，仅返回给 Harness，不进入 LLM context）：**
```json
{
  "pass": false,
  "reason": "场景整体亮度过高，DirectionalLight 角度仍接近正午（约 60 度而非 10-20 度）。天空为亮蓝色而非暮色。",
  "adjustment": "将 DirectionalLight 旋转降至 15 度，强度降至 30%，将 SkyLight 强度降低 50%。考虑添加 PostProcessVolume 的 ColorGrading 暖色调。"
}
```

**追问机制：**
Vision Sub-Agent 可以在判断前向主 Agent 追问：
- "主 DirectionalLight 的当前旋转角度是多少？"
- "能截一张只包含天空区域的放大截图吗？"
- "场景中是否有 PostProcessVolume？如果有，它的 ColorGrading 设置是什么？"

Harness 暂停 Vision 判断，获取所需信息，返回给 Vision Sub-Agent，然后完成判断。

**状态保持：**
Vision Sub-Agent 在同一 Agent Session 内保持自己的状态——它记住之前看过的截图历史，可以追踪"上次太暗了，这次好一些但角度还不对"的渐变判断。这避免了每次判断都是孤立的。

**与主 Agent 的关系：**
- Vision Sub-Agent 不直接调用 UE 工具——它通过追问 Harness 来获取信息
- Vision Sub-Agent 不修改场景——它只做判断和建议
- Vision Sub-Agent 的结果注入主 Agent 的 System Context（Slot 1），作为"上次操作的视觉反馈"

**实施：**
- `harness/verification/vision_agent.py` — Vision Sub-Agent，使用独立的 LLM API 调用（Claude Vision / GPT-4V），拥有独立的对话历史
- 截图在发送前 resize 到 1024x768 最大（token 成本优化）
- 遥测：记录 vision model 延迟、token 成本、通过/失败率

**后果：**
- Vision Sub-Agent 的 API 调用有额外成本（tokens + latency）
- 每个需要视觉验证的步骤增加 2-5 秒延迟
- 追问机制意味着 Vision 判断不是即时的——它可能需要与 Harness 多次交互
- 如果 Skill 未定义 `verification`，该步骤跳过视觉验证（零额外开销）

---

## Amendment 1 — LLM → Vision 追问方向（2026-07-03, Issue 015）

**背景**：原 ADR 仅定义了 Vision → LLM 的追问方向（`VisionVerdict.need_more_info` → Harness 获取信息 → `continue_with_info()`）。Issue 015（Vision Session 架构）引入了反向追问：LLM 主动对同一张截图继续提问（"那个蓝色光具体在哪个位置？"），由 Harness 工具 `vision_ask` 承载。

**修订**：追问是双向的。

| 方向 | 触发 | 机制 | 使用场景 |
|------|------|------|---------|
| Vision → LLM | Vision 判断信息不足时自动 | `VisionVerdict.need_more_info` → `VisionSubAgent.continue_with_info()` | "图中太暗，光源的色温是多少？" |
| LLM → Vision | LLM 主动调 `vision_ask(question="...")` | `VisionSessionManager.ask()` → `VisionSubAgent.check()` with accumulated history | "上次说的蓝色光具体在哪？阴影方向对吗？" |

两者共享同一个 `VisionSession` 的对话历史（`VisionSubAgent._history`），Vision 可以引用之前的分析结果和截图。

**Session 抽象**：`VisionSession`（`harness/verification/session.py`）包装 `VisionSubAgent`，管理截图绑定、上下文自动注入、多轮对话追踪、Session 过期警告。`VisionSubAgent` 降级为纯 API client——只负责消息格式转换和 Anthropic API 调用。

**实施**：详见 Issue 015（`docs/issues/015-vision-targeted-questioning.md`）。
