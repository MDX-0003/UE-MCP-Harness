# 020 — 参考图子系统独立 + ReferenceImageSession

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

match_reference 是 server.py 里最大的一个 if 分支（~330 行），它维护了一个裸字典 `_session_reference`，用 14 个魔法字符串键（`_countdown_remaining`、`best_metrics`、`_snapshot_saved` 等）散落在各处读写。每次迭代往上加新键，没有类型、没有封装、没有单测。

本 Issue 做三件事：

1. **ReferenceImageSession**：把那个裸字典变成一个带类型的数据类，14 个键变成显式字段，叫停规则（hist≥0.70 激活 3 轮倒计时、最多 10 轮兜底、换参考图清空所有累积状态）变成方法——这样规则可以独立测试，不会因为手误再散落。
2. **match_reference 和 build_atmosphere_mapping 从 server.py 搬出**：各自归入独立模块。两者的输出文本逐字不动。
3. **StopLimitInterceptor 废止**：这个类虽然实现了 `ToolCallInterceptor`，但它从不覆盖 pre_call 或 post_call——拦截器链里只是空转一圈。真正的用途是 server.py 直接调它的 `build_summary` 方法生成硬终止文本。本 Issue 把摘要生成逻辑改为普通函数，放入参考图模块，从拦截器链里删掉这个空转项。

同批修复一个运行时 bug：连续两轮指标下降时，代码写错了变量名（`lines` 应该用 `body_lines`），会触发 Python 的 UnboundLocalError。这个 bug 在正常使用中很难触发（需要连续下降 + 恰好不再触发新最佳），但一旦触发就是崩溃。

## Acceptance criteria

### 行为不变

- [ ] `match_reference(path)` → 每一步的输出文本与重构前逐字一致（参考图加载成功/失败、量化指标表、MiMo 9 维度分析、倒计时状态、最佳点追踪标注、连续下降警告、尾部引导文本）
- [ ] `build_atmosphere_mapping()` → 5 类组件扫描汇总 + 属性索引 + MiMo 分类结果 + Markdown 渲染 = 与重构前一致
- [ ] 叫停逻辑的行为不变：倒计时归零 → 硬终止返回 isError=True + 回顾摘要文本；总轮次超 10 → 同上

### 状态类化

- [ ] `ReferenceImageSession` 有明确字段定义和类型，替代原来的 14 个散落魔法键
- [ ] `check_stop()` 返回 `None`（继续）或停止原因文本。调用方不需要理解倒计时公式内部
- [ ] 换参考图 → 倒计时重置为未激活、最佳记录清空、下降计数归零（有单测覆盖）
- [ ] `record_metrics()` 内部处理最佳点追踪 + 连续下降检测 + 倒计时递减 + SaveLevelAs 触发时机——调用方只传指标数据，不自己算

### Bug 修复

- [ ] 连续 2 轮直方图相似度下降 → 注入 ⚠ 警告文本（修复了原代码的 UnboundLocalError）
- [ ] SaveLevelAs 只在直方图首次跨过 0.70 时保存一次（行为不变，仅代码从散落魔法键收口为 `snapshot_saved` 字段）

### 清理

- [ ] `StopLimitInterceptor` 不在拦截器链中（它不是拦截器）。`build_summary` 逻辑已作为普通函数归入参考图模块
- [ ] server.py 的 match_reference 和 build_atmosphere_mapping 两个 if 分支全部删除（~500 行）

### 回归

- [ ] `uv run pytest tests/ -v` 全绿

## Blocked by

- Issue 018 — 注册表基建（handler 搬出依赖 HarnessTool + ToolContext）
- Issue 019 — 可与 019 并行，但都依赖 018
