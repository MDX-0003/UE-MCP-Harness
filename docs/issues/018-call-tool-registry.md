# 018 — Skill 工具组的独立分发

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

目前 `call_tool` 是一个 900 行的函数，所有 Harness 自有工具（skill 系、vision 系、参考图系）和 UE 透传全部挤在一个 `if name == "..."` 的大平铺里。

本 Issue 开第一刀：把 Skill 相关的 4 个工具（activate_skill / save_skill / deactivate_skill / get_context）从 if 链里抽出来，变成独立的 handler。同时搭好"工具注册表"这套基建——后续 Issue 把 vision 系、参考图系逐个迁入时，只加 handler、不改分发骨架。

对 LLM 来说，这四个工具的行为与重构前完全一样：同样的工具名、同样的参数、同样的返回文本。

## Acceptance criteria

### 对 LLM 可见行为不变

- [ ] `activate_skill(name)` → 匹配到的 Skill 信息文本与重构前逐字一致
- [ ] `save_skill(name, yaml)` → 保存成功/失败的返回文本一致（包括重复名检查和格式错误提示）
- [ ] `deactivate_skill()` → 退出 Skill 模式的提示文本一致
- [ ] `get_context()` → 返回的完整 prompt 文本一致（UE 状态快照 + 活跃 Skill + 可用工具列表）
- [ ] `tools/list` → Harness 暴露给 LLM 的工具列表不变（相同的名称、描述、inputSchema）

### 基建落地

- [ ] `HarnessTool` 类型定义好了——一个工具 = 名称 + 描述 + inputSchema + handler 函数，四者写在一起，不再出现"handler 支持某参数但 inputSchema 忘了声明"的漂移
- [ ] `ToolContext` 类型定义好了——handler 需要的所有依赖（配置、UE 客户端、WorldState、Skill 注册表等）打包在一个对象里，替代原来靠闭包捕获的 11 个参数
- [ ] `tool_ok(text)` / `tool_fail(text)` 两个构造器——消灭原来 25+ 处各自手写 `CallToolResult(content=[TextContent(...)])`
- [ ] 本地工具调用后的 JSONL 日志写入不再靠字符串 `"ToolCallLogger"` 硬匹配类名（重命名就会静默失效的坑）

### 回归

- [ ] `uv run pytest tests/ -v` 全量绿

## Blocked by

- Issue 017 — MCP 结果解包收敛（handler 搬运时需要用到统一的解包函数）
