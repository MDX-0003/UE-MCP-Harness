# Handoff: Issue 015 Vision Session 架构实现

**日期**: 2026-07-06
**审查者**: 人工（非 AI 连续性文档）
**测试**: 287 passed, 4 skipped

---

## 1. 做了什么

### Issue 015 核心：Vision Session 架构

**问题**：Vision Agent 的提问方式是单向盲管——截图扔进去，泛泛描述扔出来。外部 LLM 无法针对性提问、无法追问、Vision 不知道场景上下文。

**方案**：三层增强——针对性提问 (`question` 参数) + 场景上下文自动注入 (dirty actors + recent writes) + 多轮追问工具 (`vision_ask` / `vision_tell` / `vision_reset` / `vision_status`)。

### 工具变更

| 操作 | 工具 | 说明 |
|------|------|------|
| 🆕 新增 | `vision_screenshot` | 截图 + 创建 Vision Session + 可选针对性提问 + 自动注入场景上下文 |
| 🆕 新增 | `vision_ask` | 同一 Session 内追问（不截新图），复用对话历史和所有截图 |
| 🆕 新增 | `vision_tell` | LLM 手动注入任务意图（系统无法推断的信息），不调 API |
| 🆕 新增 | `vision_reset` | 关闭 Session 归档，开启新 Session |
| 🆕 新增 | `vision_status` | 查看 Session 摘要（时长、截图数、提问数、上次结论） |
| ❌ 删除 | `take_screenshot` | 完全移除（工具列表 + handler + 所有字符串引用） |
| ❌ Deny | `CaptureAssetImage` | 加入 denylist，LLM 不再可见 |

### 新文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/verification/session.py` | 737 | VisionSession + VisionSessionManager + Context Builder + Token Cap + Session Warning + Recent Writes Buffer |
| `tests/test_vision_session.py` | 345 | 35 个单元测试 |
| `branch_mark/score.py` | 351 | JSONL 离线评分引擎（8 项指标，A/B 对比） |
| `branch_mark/tasks/modify_and_verify.yaml` | 35 | 任务 1：修改 Actor + 验证效果 |
| `branch_mark/tasks/describe_and_check.yaml` | 27 | 任务 2：观察场景 + 确认属性 |
| `branch_mark/README.md` | 50 | 使用说明 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `harness/cli.py` | VisionSessionManager 创建和 wiring + instructions 增强（验证 SOP + 相机定位 + 4 角度预设） |
| `harness/config.py` | `CaptureAssetImage` 加入 denylist |
| `harness/server.py` | 5 个 vision_* 工具的 schema + handler；Harness 工具日志记录；`take_screenshot` 完全移除 |
| `harness/context/prompt.py` | SystemContextProvider 加 1 行 SOP hint |
| `harness/verification/vision_agent.py` | `VISION_SYSTEM_PROMPT_QUESTION`；`check()` 加 `question`/`scene_context`；`continue_with_question()` |
| `harness/verification/interceptor.py` | VisionInterceptor 对接 SessionManager；修复 `vision_screenshot` 未创建 Session 的 bug |
| `harness/state/interceptor.py` | 12 个 handler 加 `record_write()` + `dirty_actors.add()` |
| `harness/observability/logger.py` | JSONL 位置改为 `{session_dir}/tool_calls.jsonl`；智能输出格式化（`list_properties` 摘要）；新字段 `ts/tool/input/output/ms/screenshot/verdict` |
| `harness/observability/snapshotter.py` | 暴露 `_last_saved_screenshot_path` 供 logger 回调 |
| `skills/scene-verification.yaml` | 更新 tools_allowlist（移除旧截图工具、加相机工具）；steps 增加相机定位和 4 角度轮换 |
| `docs/adr/0006-vision-sub-agent.md` | Amendment 1：LLM→Vision 追问方向 |
| `docs/issues/015-vision-targeted-questioning.md` | 完整设计文档（705 行，9 节） |

---

## 2. 关键设计决策

### Session ≠ Screenshot

Session 绑定到**任务**而非单张截图。多张截图累积在同一 Session，vision_ask 可引用历史。"每次截图开新 Session"的方案被废弃。

### 上下文自动注入，非 LLM 手动提供

`VisionSessionManager._build_auto_context()` 自动从 WorldState 提取 dirty actors + recent writes。LLM 无需手动调 `vision_tell`。仅当需要注入"任务意图"（系统无法推断的）时才手动 tell。

### Prompt 组合 > 代码包装

相机定位通过 prompt 引导 LLM 组合 UE 原生工具 (`SetCameraTransform` + `FocusOnActors`) 和 Harness 工具 (`vision_screenshot`)，而非在 Harness 代码里包装 UE tool call。零耦合。

### 4 角度轮换法

给 LLM 4 个预设 camera rotation（斜前侧/侧面/俯瞰/正面平视），每次 `SetTransform → FocusOnActors → 截图` 轮换尝试，找到使目标清晰可见的角度。

---

## 3. 已修复的 Bug

| Bug | 修复 |
|-----|------|
| `vision_screenshot` 调完 → `vision_ask` 报告"无活跃 Session" | `VisionInterceptor.post_call` 三处硬编码 `"take_screenshot"` 漏改 `"vision_screenshot"` |
| `vision_ask` / `vision_tell` / `vision_reset` / `vision_status` 调用不记入 JSONL | server.py 加 `_log_harness_call()` 辅助函数，手动触发 ToolCallLogger |
| JSONL 与截图分散在两个目录 | JSONL 写入 `{session_id}/tool_calls.jsonl`，与截图同目录 |
| `list_properties` 输出截断到 2000 字符，属性值被切掉 | 智能摘要：`[42 fields] propName1, propName2, ...` |
| Vision Session 归档在 `logs/` 根目录 | 移到 `logs/{session_id}/vision_sessions/` |

---

## 4. 验证方法

### Branch Mark 评估

```
# 1. 打印任务指令（不点名工具名称，让 LLM 自发走验证流程）
branch-mark show modify_and_verify

# 2. 复制指令给 LLM → 等待完成

# 3. 评分
branch-mark score .ue-harness/logs/<session>/tool_calls.jsonl

# 4. 改 Harness 前后各跑一次，A/B 对比
branch-mark score baseline.jsonl after.jsonl
```

### 快速冒烟测试

重启 Harness 后，LLM 调 `tools/list` 应只看到 `vision_screenshot`（没有 `take_screenshot` 和 `CaptureAssetImage`）。调 `vision_screenshot` 后应能正常 `vision_ask`，不应报告 "没有活跃的 Vision Session"。

### 全量测试

```bash
pytest tests/ -v
# 预期: 287 passed, 4 skipped
```

---

## 5. 待后续

| 项目 | 状态 |
|------|:--:|
| L2 读回结果自动注入 Vision context | 未实现（依赖 StateCacheInterceptor 捕获 L2 读回事件） |
| `vision_tell` 自动去重 | 未实现（重复 tell 会累积冗余） |
| Vision Session 跨 Harness 重启恢复 | 未实现（重启后 Session 清空） |
| Branch Mark Phase 2（脚本驱动 LLM 批量评估） | 未实现（需要 Harness 主动发消息给 LLM） |
