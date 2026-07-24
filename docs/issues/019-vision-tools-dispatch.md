# 019 — Vision 工具组的独立分发

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

接续 Issue 018 的注册表基建，把 Vision 相关的 5 个工具（vision_screenshot / vision_ask / vision_tell / vision_reset / vision_status）从 if 链迁入独立 handler。

同时把每个 vision handler 里重复出现的守卫代码消除掉：原来 4 个 handler 各自写了一遍"Vision Session Manager 初始化了吗？没初始化就报错"的检查，现在收口为一行。

对 LLM 来说，vision_* 五个工具的行为不变。

## Acceptance criteria

- [ ] `vision_screenshot(mode=viewport)` → 截图 + 自动上下文注入 + Vision 分析结果与重构前完全一致
- [ ] `vision_screenshot` 的事件广播路径不变——VisionInterceptor 和 SnapshotRecorder 仍然能看到截图事件并消费
- [ ] `vision_ask(question)` → Session 内追问的结果一致（包括三级超时警告注入）
- [ ] `vision_tell(info)` → 手动注入上下文的返回文本一致
- [ ] `vision_reset()` / `vision_status()` → 结果一致
- [ ] 原来 4 处重复的 `vision_session_manager is None` 守卫检查收口为一份
- [ ] 原来每个 handler 里手写的计时 + 日志样板被消除（`t0 = time.monotonic()` 那套）
- [ ] server.py `call_tool` 的 if 链减少 ~150 行，vision 分支全部删除
- [ ] `uv run pytest tests/ -v` 全绿

## Blocked by

- Issue 018 — Skill 工具组分发（注册表基建 + ToolContext 由 018 铺设）
