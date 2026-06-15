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
- [ ] Harness 自有 MCP 工具 `activate_skill(name_or_desc)`：支持按 name/description 片段匹配 → 设置 `_active_skill`
- [ ] Harness 自有 MCP 工具 `save_skill(name, yaml_content)`：LLM 可调用此工具保存新 Skill
- [ ] `skill_registry.match_skill()` 支持多匹配时返回备选列表，LLM 选择后激活

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

匹配逻辑：支持以下方式激活 Skill：
  1. **trigger 子串匹配**：用户意图描述与 `triggers` 列表中任一项做子串匹配（大小写不敏感）
  2. **name/description 匹配**：支持按 Skill 名称或描述片段查找（`match_skill("黄昏")` 可命中 `evening-lighting`）
  3. **显式指定**：LLM 直接调 `activate_skill("evening-lighting")`

---

## Harness 自有 MCP 工具（005 新增）

005 在 Harness MCP Server 中注册两个自有工具（不走 UE 透传）：

### `activate_skill`
- **参数**：`skill_name_or_desc: str` — Skill 名称或描述片段
- **行为**：`skill_registry.match_skill(name_or_desc)` → 设置 `_active_skill` → 下轮 `tools/list` 返回 Skill 白名单
- **多匹配**：返回备选列表，LLM 选择后再次调用

### `save_skill`
- **参数**：`name: str`, `yaml_content: str`
- **行为**：`skill_registry.save_skill(name, yaml)` → 验证 YAML 格式 → 写入 `~/.ue-harness/skills/{name}.yaml`
- **YAML 模板生成**：LLM 可通过 `describe_toolset` 了解可用工具后，自行生成符合格式的 YAML

---

## 自动保存 Skill（save_skill 融入 Harness 循环）

`save_skill` 不仅是 LLM 手动调用的工具，也支持 Harness 自动触发：

| 触发路径 | 时机 | 行为 |
|---------|------|------|
| **A. LLM 显式调用** | 用户说"保存为 Skill" | LLM 生成 YAML → 调 `save_skill` |
| **B. 任务完成自动提示** | `active_skill` 所有步骤 `completed` | Harness 在 System Context 注入："本任务已完成 X/Y/Z。要保存为已验证 Skill 吗？" |
| **C. 日志回放导出**（未来） | `harness replay` 成功后 | 将回放 log 中的调用序列 + 参数生成为 Skill YAML 模板 |

路径 B 依赖 008（StateCacheInterceptor 追踪 write 调用）+ 009（TaskMemory 追踪 `completed`/`pending`）。路径 C 依赖 003（日志 JSONL）。
