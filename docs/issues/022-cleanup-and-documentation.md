# 022 — 周边清理 + 文档同步

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

前面的 Issue 搬完了家具（模块拆分、命名规整），本 Issue 打扫房间：修剩余的配置/统计/传输层的 bug，清理确认过的死代码，然后把所有文档回标到重构后的实际状态。

### 修复

- **vision 模型配置的来源统一**：目前同一个配置有三个不同的默认值散布在三处（类定义、环境变量默认值、verification 配置常量）——统一为一个。这是 0621 修过的同类 bug（sse_read_timeout 默认值也曾经三处分裂），本次一并根除。
- **`harness stats` 命令恢复可用**：目前 stats 读的日志字段名和实际写入的不一样，且只搜顶层目录而日志存在子目录里——结果永远为空。改读新字段名、递归搜索。
- **`_cancel_request` 的安全性核实**：HANDOFF_0624 记录这个函数因为误删 UE 侧活跃请求条目已被移除。但当前代码里它还在（5 秒超时 + 尽最大努力标记）。确认是回归还是有意恢复——如果危险就再删，如果有意就补注释。

### 清理

- **删死代码**：Vector3 模型（定义后从未被用）、VisionConfig（全仓无引用）、call_tool_blocking（流式路径已覆盖全场景）、record_harness_dirty（从未被接线）、三个废弃的 Vision 兼容属性（pass_/reason/adjustment 恒返回 None）、多处无用 import 和死变量
- **cli.py 拆瘦**：55 行的 instructions 大字串从启动函数里提出来，独立放置；`cmd_start` 里 337 行的巨型函数拆成几个装配步骤
- **Config 整理**：两份互相复制的 dotenv 解析合二为一；merge_cli_overrides 不再手工维护 19 字段名单（曾因此漏字段出过事故），改为自动遍历

### 文档同步

代码稳定后，把 `docs/` 下所有核心文档回标到实际状态——消除发现的 ~15 处文档与代码不一致的地方。重点：架构文档的目录结构按实际代码重绘、CONTEXT.md 按 ADR 0008 修订过时术语、契约文档追加修订段说明 pre_call 实际语义与示例代码不同、ADR 0005 标注已被 0008 推翻的条款。

## Acceptance criteria

- [ ] `harness stats` 能正常输出统计数据
- [ ] vision 模型配置只有一处默认值来源
- [ ] 已确认的死代码全部删除，`grep` 搜不到残留引用
- [ ] cli.py `cmd_start` 不再是一个 337 行的函数
- [ ] dotenv 解析全仓只有一个实现
- [ ] docs/architecture.md §4 目录树与实际代码一致
- [ ] docs/CONTEXT.md 的 State Cache 和 Task Memory 词条按 ADR 0008 修订
- [ ] docs/contracts.md 对 pre_call 语义和 ActorSnapshot 字段有修订段说明
- [ ] docs/adr/0005-session-decoupling.md 磁盘持久化条款有修订注记
- [ ] CLAUDE.md 测试数与实际一致；Issue 状态表准确
- [ ] `uv run pytest tests/ -v` 全量绿

## Blocked by

- Issue 021 — 命名规整 + 结构性修复完成后，文档才能准确回标
