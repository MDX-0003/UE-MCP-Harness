# 005 — Skill 系统：任务模板 CRUD 与注入

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

实现 Harness Skill 的完整生命周期——创建、读取、更新、删除、列表、匹配。Skill 以 YAML 文件存储在 `~/.ue-harness/skills/`。

当用户意图与某个 Skill 的 `triggers` 字段匹配时，Harness 将 Task Context（Tier 2）注入 LLM prompt，同时将 `tools/list` 返回的工具列表限制为该 Skill 的 `tools_allowlist`。

## 验收标准

- [ ] `harness skill create <name>` 在 `~/.ue-harness/skills/` 创建模板 YAML 文件并打开编辑器
- [ ] `harness skill list` 列出所有已安装的 Skill（名称 + 描述 + 触发器）
- [ ] `harness skill delete <name>` 删除 Skill 文件
- [ ] `harness skill update <name>` 更新 Skill 文件
- [ ] Skill YAML 格式验证：缺少必填字段（`name`、`triggers`、`steps`）时加载报错，但不影响其他 Skill
- [ ] 用户消息匹配 Skill trigger（大小写不敏感中文匹配）→ System Context 中自动注入 Task Context
  - 包含：`completed`、`pending`、`current_step`、步骤指令
- [ ] Skill 激活时，`tools/list` 仅返回 `tools_allowlist` 中指定的工具
- [ ] 多个 Skill 的 trigger 同时匹配 → 提示 LLM 选择（在 System Context 中列出备选 Skill）
- [ ] 无 Skill 匹配 → 回退到自由探索模式（#004 的白名单）
- [ ] 预置示例 Skill：`evening-lighting.yaml`（中文 triggers + steps）

## 阻塞

- #004（Context Assembly——Skill 注入依赖于 Context Assembler 的 Tier 2 slot）

## 设计说明

Skill YAML 格式：
```yaml
name: evening-lighting
description: "将场景光照调整为黄昏/傍晚氛围"
triggers: ["黄昏", "傍晚", "sunset", "dusk"]
tools_allowlist:
  - "SceneTools.find_actors"
  - "ActorTools.set_actor_transform"
  - "ObjectTools.get_properties"
  - "ObjectTools.set_properties"
  - "SlateInspector.Screenshot"
  - "EditorAppToolset.CaptureEditorImage"
steps: |
  1. 找到场景中所有 DirectionalLight
  2. 将主光旋转调整为低角度（地平线上 10-20 度）
  ...
verification:
  type: screenshot
  expected: "场景具有温暖的低角度光照和长阴影"
  tolerance: 0.7
```

匹配逻辑：用户消息（不含 system prompt）与 `triggers` 列表中的每一项做子串匹配（大小写不敏感）。
