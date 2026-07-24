# 022 — 文档同步：架构/契约/词汇表的全面回标

**类型：** AFK

**依赖关系：** 依赖 Issue 017-021 全部完成（代码已稳定，文档才能准确回标）。

## 要构建什么

经过 Issue 017-021 的重构，代码的模块边界、函数命名、拦截器链顺序、文件清单均已变化。本 Issue 将 `docs/` 下所有核心文档同步至重构后的实际状态——消解已发现的 ~20 处文档-代码漂移点，确保外部读者（UE 同事、下游开发者）看到的文档与代码一致。

**受众原则**：`docs/` 读者是外部人，不写 "Issue 017 之后"，只描述现状。

## 验收标准

### architecture.md

- [ ] **§1.1 工具数量**：更新为当前实际工具数（211）并删 `~157` 过期口径
- [ ] **§1.2 角色表格**：pitch/capture/mapping 等新增职责归入正确角色行
- [ ] **§2.4 状态缓存**：L2 读回从"可选"→"ReadbackInterceptor，Issue 016 已落地，正确性主通道"；加入 ADR 0008 指纹校验叙述
- [ ] **§3 实施阶段**：P5 移除、P6(009) 作废、P7(010) 跳过 → 标注状态；P8(011) 仍待开发；增加 017-022 重构里程碑
- [ ] **§4 目录结构**：按重构后实际代码重绘——加入 tools.py / reference.py / atmosphere.py / vision_tools.py / skill_tools.py；移出 stop_limit.py；标注 memory/recovery/safety 为占位；补 state/models.py、state/normalize.py、context/provider.py、verification/config.py

### CONTEXT.md

- [ ] **State Cache 词条**：按 ADR 0008 改写——UE 是唯一权威，WorldState 是带指纹校验的观测记录，Write-Through 从"核心策略"降级为"观测速记"（注：Issue 017-021 未改策略语义，是文档表述对齐）
- [ ] **Task Memory 词条**：删除旧定义（结构化 JSON 替代对话历史），替换为轨迹记忆（引用 ADR 0008 §5），标注实现待 014/016 验收后另立 Issue
- [ ] **新术语**：补充 Readback / ReadbackInterceptor / ReferenceImageSession / atmosphere-mapping / ToolContext
- [ ] 删除 `~157`、Issue 013 等已作废引用

### contracts.md

- [ ] **Contract 1 pre_call 语义**：在调用规范示例代码旁追加**修订段**——"2026-07-24 修订：当前实现中 pre_call 抛异常将阻断调用（`error=e; break`），而非示例中的仅记日志继续。此语义与 Issue 011 Safety 的 DENY 需求一致。"
- [ ] **Contract 2 ActorSnapshot schema**：补充 `label` / `tags` / `components` 字段（已在 models.py 实际代码中存在，contract 文档漏列）
- [ ] **Contract 2 docstring 刷新策略引用**：WorldState 注释中 `ADR 0004` → `ADR 0008`（信任模型已倒转）；标记 Issue 013 已作废
- [ ] **Contract 4 涉及模块**：`014` → 标记为已降级，L2 读回属于 Issue 016；追加 017-022 重构不改变五工具契约的声明
- [ ] **文档范围**：头部补充覆盖 012/015/016/017-022
- [ ] **拦截器链注册点**：更新为重构后的实际顺序（DebugPreCall → Readback → Logger → StateCache → DriftAlert → Vision → SnapshotRecorder），停止引用 StopLimit

### ADR 回标

- [ ] **ADR 0005 后果**：「MCP 断开时保存 Agent Session 到磁盘」→ 追加修订注记：已按 ADR 0008 作废，Issue 013 文件已删，跨会话恢复改为轨迹记忆+指纹校验（参照 0004 的修订注记写法）
- [ ] **ADR 0007**：在已完成项中标注 Issue 017-022 重构覆盖的条目（L2 读回落地、命名统一、模块拆分）
- [ ] **ADR 0008**：确认文档与代码一致（无新增修订点——重构不改变信任模型）

### CLAUDE.md

- [ ] **当前状态表**：增加 017-022 行 => ✅；测试数更新（重构后实际数）
- [ ] **目录**：§2 代码树与实际一致（同上 architecture.md §4 的变更）
- [ ] **Agent Skills 表**：删除 `to-issue`（不存在），补充重构后恢复稳定的 `codegen` / `neat-freak` / `gortex-*` 使用说明
- [ ] **社区 Skills 索引**：若 gortex daemon re-index 后有变化，更新

### 其他文档（按需）

- [ ] docs/setup.md：测试计数更新
- [ ] docs/handoff/ 中引用 take_screenshot 的手递文 → 改 vision_screenshot（015 已做但 handoff 文字未改）
- [ ] docs/tmp_issues/0713/analysis.md：已由后续 0714 plan 解决的问题标注状态
- [ ] docs/knowledge/basic_knowledge.md Q6/Q7：标注传输层跟踪基于旧 SSE 时代，非现行 Streamable HTTP 实现

### 验证

- [ ] `grep -r '~157' docs/` 零命中
- [ ] `grep -r '\\b010\\b' docs/` 仅出现在状态表/历史记录中，无"待开发"误标
- [ ] `grep -r '\\b014\\b' docs/` 仅出现降级标记，无"当前里程碑"误标
- [ ] CLAUDE.md 测试数与 `uv run pytest tests/ --collect-only | tail` 一致

## 设计说明

**修订段追加模式**：contracts.md 的修改规则是"已锁定 Contract 只做追加、不改前文"——所以 pre_call 语义和 ActorSnapshot 字段的修正以追加修订段形式落地（如 Contract 2 末尾加 `> **2026-07-24 修订：** ActorSnapshot 新增 label/tags/components 字段...`），不直接改原段。

**文档 Cleanup 的范围**：本次仅回标与 017-022 重构直接相关的漂移。已发现的 but 非本次触发的过期内容（如 CAPTURE_ASSET_IMAGE_TIMEOUT 手递文中的行号漂移、TAKE_SCREENSHOT_VIEWPORT_FLOW 的行号）若不影响外部读者理解可推迟——neat-freak 遵循"重构动了什么文档就修什么文档"的最小原则。

**ADR 0007 不是全量更新**：它是 2026-06-20 的时点快照，不要求逐条标注"已做完/没做完"——只标注 Issue 016/017-022 覆盖的可识别条目。

## 涉及文件

- docs/architecture.md
- docs/CONTEXT.md
- docs/contracts.md
- docs/adr/0005-session-decoupling.md
- docs/adr/0007-grill-assessment-0620.md
- docs/setup.md
- docs/handoff/*（take_screenshot → vision_screenshot 的 5+ 处残留引用）
- CLAUDE.md
