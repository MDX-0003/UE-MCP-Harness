# 021 — 命名规整 + 结构性 Bug 修复

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

前面三个 Issue 把模块边界稳定了。本 Issue 做两件事：

### 一、命名规整

全仓公开函数/类按「前缀+功能」统一——前缀标明它属于哪个子系统。例如 `capture` → `capture_screenshot`、`apply_filter` → `ctx_filter_tools`。约 30 个符号。跨模块用的函数去掉下划线私有前缀（如 `_parse_ref_path` → `state_parse_ref_path`），让人一眼能看出它是公开的。

不改 LLM 看到的 MCP 工具名（activate_skill / vision_ask 等）——那是外部契约，要改得同步改 Skill YAML 和 instructions，代价太大，单独立项。

### 二、三个结构性修复

**record_write 回调注入。** 目前 `harness/state` 模块直接 `import` 了 `harness/verification` 的一个函数——这是反向依赖，形成了一个 import 环。修复方式：让调用方从外部注入回调函数，state 模块不再知道 verification 存在。这个改动还有一个附带收益：原来 15 个 write handler 里有 8 个忘了调 record_write（组件/文件夹操作对 Vision 上下文完全不可见），注入到单点后不再依赖每个 handler 手动记得调。

**_is_screenshot_tool 收敛。** 目前在三个位置各有一份"判断某个工具是不是截图工具"的逻辑，而且语义还不一样——logger 用精确名单（大小写敏感，会漏掉 UE 原生截图工具），vision 拦截器用关键词子串匹配。同一段工具调用在两个拦截器里可能一个被认作截图、另一个不认。收为一份。

**修复两个静默失效。** cli.py 在导入时绑定了一个当时为 None 的变量，导致 logger 的 screenshot 字段永远不写（B2）。session.py 两处读取 `last_verdict["pass"]` 和 `["reason"]`，但实际写入的 verdict 字典里从来没有这两个键——P3 上下文块和"上次结论"一直是空的（B7）。

## Acceptance criteria

### 命名

- [ ] 约 30 个公开符号重命名后，`grep` 在 harness/ 下搜不到旧名（测试文件同步更新）
- [ ] MCP 工具名不变——LLM 看到的 activate_skill / vision_* / match_reference 等名字原样不动
- [ ] skill YAML 文件不需要改

### 结构性修复

- [ ] state 模块不再 import verification 的任何符号（import 环断开）
- [ ] 任何一个 write handler 执行成功后，record_write 自动被调用——不再依赖各 handler 手动记得写（新增 handler 也不会漏）
- [ ] 判断"这是截图工具吗"全仓只走一份逻辑（删掉两份各自不同的实现）
- [ ] ToolCallLogger 的 screenshot 字段能正常写入截图路径（B2 修复，不再是 None）
- [ ] 上次 Vision 结论能正常出现在上下文注入和 Session 状态摘要里（B7 修复，不再永远是空的）

### 回归

- [ ] `uv run pytest tests/ -v` 全量绿
- [ ] `uv run harness start --help` 子命令名清晰可读

## Blocked by

- Issue 018+019+020 全部完成（模块边界稳定后命名才有意义；record_write 回调注入依赖 handler 已迁走）
