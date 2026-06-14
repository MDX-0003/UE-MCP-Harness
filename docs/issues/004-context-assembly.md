# 004 — 上下文组装：三层 Prompt 管线

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

Harness 不再将原始 UE 工具列表透传给 LLM。Context Assembler 接管 `tools/list` 的返回内容，根据当前模式（自由探索 vs Skill 已匹配）过滤工具，注入三层上下文。

实现三层 prompt 构建管线：
- **Tier 1 System Context**：Agent 身份 + State Cache 快照（初期简陋版——先不做真正的 State Cache，用占位文本）
- **Tier 2 Task Context**：暂不实现（#005 Skill 系统负责），提供空 slot
- **Tier 3 Tool Reference**：按需 `describe_toolset` 加载，当前先全量透传所有工具名称 + 描述（Schema 细节仅在 LLM 显式调 `describe_toolset` 时返回）

## 验收标准

- [ ] 自由探索模式下，`tools/list` 返回 ~20 个默认工具（`SceneTools.*`、`ActorTools.*`、`ObjectTools.*`、`EditorAppToolset.*`、`SlateInspector.Screenshot`），而非 157 个
- [ ] `list_toolsets` 和 `describe_toolset` 始终对 LLM 可见，作为逃生通道
- [ ] System Context 包含 Agent 身份描述（"你是一个 UE Editor Agent..."）和基础的 State Cache 占位（"当前 UE 状态：待 Harness #008 实现"）
- [ ] `tools/list` 返回的工具描述从 UE 的 `GetToolsetDescription()` 获取，保持中文/英文原样
- [ ] 自由探索工具白名单可配置（`harness/config.py` 中的 `DEFAULT_TOOLS_ALLOWLIST`）
- [ ] Context Assembler 是独立的可测试模块：给定 mode + raw tool list → 输出 filtered tool list + prompt 文本

## 阻塞

- #002（工具透传——需要先能拉取工具列表）

## 设计说明

Tier 3 的"按需加载"在当前阶段简化为：LLM 调 `describe_toolset("NiagaraToolsets")` 时，Harness 转发给 UE 并返回结果。未来可优化为 Harness 内部缓存 `describe_toolset` 结果，避免重复查询。

自由探索模式的白名单策略从保守开始（~20 个最安全的读+写工具），后续根据 Skill 使用频率自动扩展。
