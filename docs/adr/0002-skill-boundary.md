# 0002 — Harness Skill（任务级）与 UE AgentSkill（工具级）的分工边界

**背景：** UE5.8 有 `UAgentSkill`——一个 UObject DataAsset，生成描述如何使用特定工具集的 prompt（如 Niagara emitter 层级结构）。Harness 方案也包含一个"Skill"系统，用于捕获多步任务工作流（如"构建咖啡馆"）。两个问题：（1）概念重复；（2）若两边都注入 prompt，上下文污染。

**决策：** Harness 和 UE 在不同语义层级管理 Skill，生命周期不同。两者不合并。

**边界：**

| 维度 | UE `UAgentSkill` | Harness Skill |
|------|------------------|---------------|
| 语义层级 | 工具级："如何使用 Niagara emitter schema 属性" | 任务级："如何构建一个黄昏场景" |
| 触发条件 | LLM 调用 `describe_toolset("NiagaraToolsets")` 时 | 用户意图与 Skill 的 `triggers` 字段匹配时 |
| 作者 | 工具集开发者或 TA，在 UE Editor 内创建 | Harness 自动固化成功任务轨迹，或人工编写 YAML |
| 生命周期 | 随项目加载/卸载 | 跨 session 持久化于 `~/.ue-harness/skills/` |
| 存储 | `.uasset` 位于项目 Content 中 | `.yaml` 位于项目外部 |
| 内容 | 使用说明 + 工具集依赖 | 步骤列表 + 工具白名单 + 验证标准 + 恢复策略 |

**上下文组装顺序：** Harness 在发送给 LLM 之前组装上下文。UE Skill 内容按需通过 `describe_toolset` 获取——仅在 LLM 表示需要特定工具集时。Harness Skill 内容在任务意图匹配时预先注入。两者永不占据同一 context slot——Harness Skill 是"要做什么"，UE Skill 是"如何使用某步骤中调用的具体工具"。

**防污染机制：** Harness 的 Context Assembler（`harness/context/prompt.py`）将上下文分离为三个 slot：
1. **System**（始终存在）：基础 Agent 身份 + State Cache 快照 + 安全护栏
2. **Task**（Skill 匹配时注入）：Harness Skill 步骤 + 工具白名单
3. **Tool Reference**（延迟加载）：`describe_toolset` 输出，仅在 LLM 首次使用该工具集中的工具时加载

UE Skill 内容仅进入 Slot 3，永不进入 Slot 1 或 2。
