# 011 — 安全护栏：规则引擎

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

在每个 tool call 转发给 UE 之前，Harness 执行预检钩子——检查 tool name 和参数是否匹配任何安全规则。每条规则定义一个条件（工具名模式、Actor/Asset 路径模式、Actor 类模式、PIE 状态）和一个动作（ALLOW、ASK_USER、DENY）。

默认规则集保护关键游戏元素：PlayerStart、Engine Content、批量删除。

## 验收标准

- [ ] `harness safety rules list` 列出所有活跃规则及其动作
- [ ] `harness safety rules add/remove` 增删规则
- [ ] 默认规则生效：
  - 删除 `PlayerStart` 类 Actor → DENY
  - 操作 `/Engine/` 路径下的资产 → ASK_USER（需要用户确认）
  - 批量删除 >10 个 Actor → ASK_USER
  - PIE 运行时的任何写操作 → DENY（`SceneTools` 自身也会拒绝，Harness 提前拦截）
  - 所有读操作 → ALLOW（默认）
- [ ] 规则匹配效率：每个 tool call 预检耗时 <1ms（纯字符串/模式匹配，无 I/O）
- [ ] DENY 时 LLM 收到明确的被拒原因："操作 `remove_from_scene(PlayerStart_0)` 被安全规则拒绝：禁止删除 PlayerStart 类 Actor"
- [ ] ASK_USER 时用户收到交互式确认提示（CLI 交互模式或通过 LLM 转发）
- [ ] 护栏配置持久化到 `~/.ue-harness/safety_rules.yaml`

## 阻塞

- #002（工具透传——需要 tool call 拦截点）

## 设计说明

规则条件支持的模式匹配：
- `tool_name: "SceneTools.remove_from_scene"` — 精确匹配
- `tool_name: "*.remove_*"` — glob 匹配
- `actor_class: "PlayerStart"` — Actor 类型检查
- `asset_path: "/Engine/*"` — 资产路径 glob
- `pie_running: true` — PIE 状态条件

动作优先级：DENY > ASK_USER > ALLOW。多条规则同时命中时，取最严格的动作。
