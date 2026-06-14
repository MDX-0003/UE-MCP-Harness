# 006 — 视觉验证管线：截图→Vision→结构化判断

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

实现独立 Vision Sub-Agent——不占用主 Agent context window，拥有独立上下文和状态。接收截图（通过 MCP 调用 `SlateInspector.Screenshot()` 或 `EditorAppToolset.CaptureEditorImage()` 获取）和预期描述，返回结构化判断结果 `{pass, reason, adjustment}`。

本轮仅实现"单次判断"——Harness 截图 → Vision Sub-Agent 分析 → 返回结果。不包含自动循环（#007 负责）。

## 验收标准

- [ ] `harness vision check --image <base64_or_file> --expected "场景应有暖色低角度光"` → 返回结构化 JSON
- [ ] 返回格式：`{"pass": bool, "reason": "具体原因", "adjustment": "如果不通过，建议如何调整"}`
- [ ] 截图在发送前自动 resize 到最大 1024x768（保持宽高比，超过任一边则等比缩放）
- [ ] Vision Sub-Agent 使用独立的 LLM API 调用（`anthropic.messages.create` 或 `openai.chat.completions.create`）
- [ ] Vision Sub-Agent 有自己的对话历史——第二次判断可引用"上次太暗了"
- [ ] Vision Sub-Agent 可以向调用者（Harness Verification Engine）追问：
  - 可返回 `{"need_more_info": true, "question": "主 DirectionalLight 的当前旋转角度？"}`
  - Harness 获取信息后重新调用 Vision Sub-Agent
- [ ] Vision API 调用记录延迟和 token 消耗到日志
- [ ] Vision API key 从环境变量或 `.env` 文件读取，不硬编码
- [ ] 有单元测试：给定 mock 截图 → 验证返回格式正确

## 阻塞

- #002（工具透传——需要通过 MCP 调用截图工具）

## 设计说明

**为什么是独立 Agent 而非主 Agent 直接看截图？**
- 截图是 heavy content block——会占用主 Agent 宝贵的 context window
- Vision 判断独立于 LLM 推理，可审计
- 追问机制让 Vision Sub-Agent 可以请求更多信息而不污染主 Agent 上下文

**追问流程：**
```
Harness → Vision Sub-Agent: 截图 + "场景应像黄昏"
Vision Sub-Agent → Harness: {"need_more_info": true, "question": "DirectionalLight 的当前旋转角度？"}
Harness → UE MCP: get_actor_transform(DirectionalLight_0)
UE → Harness: {rotation: (60, 0, 0)}
Harness → Vision Sub-Agent: "DirectionalLight 旋转角度为 (60, 0, 0)"
Vision Sub-Agent → Harness: {"pass": false, "reason": "角度相当于正午", "adjustment": "降至 15 度"}
```
