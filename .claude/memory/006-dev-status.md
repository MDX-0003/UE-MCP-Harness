---
name: dev-status-2026-07-14
description: 2026-07-14 开发进度——398 tests，Issue 016 完整交付（L2 读回 + 参考图匹配 + 停止机制）
metadata:
  type: project
---

# 开发进度 2026-07-14

## 全量测试: 398 passed, 4 skipped

## 已完成 Issue

| Issue | 模块 | 说明 |
|:---:|------|------|
| 001-008, 012, 015 | 骨架/透传/日志/Context/Skill/Vision/State/验证闭环/重连/Vision Session | 全部完成 |
| 016 Part A | L2 读回验证 (ReadbackInterceptor) | `harness/verification/interceptor.py:173`，cli.py 注册在 ToolCallLogger 之前注入徽章；白名单收窄为 set_actor_transform + set_properties（UE 源码审查后），详见 [[l2-readback-mechanism]] |
| 016 Part B | 参考图匹配 | `build_atmosphere_mapping` + `match_reference` 工具 + `match-atmosphere` Skill；详见 [[reference-image-design-0708]] |
| PLAN_0713 | validation error 日志 | `harness/transport.py:51` `_ErrorLoggingSendWrapper` 在 ASGI 层捕获 SDK 校验失败，写入 `tool_errors.jsonl` |
| PLAN_0713 | match_reference 收敛 | 删除 `vision_compare`（单一双图对比工具），删除 `_analyze_viewpoint` 视角自动对齐（单图估计不可靠） |
| PLAN_0714 v3 | 倒计时停止机制 | `harness/stop_limit.py` + server.py 状态机：hist≥0.70 激活 3 轮倒计时硬切断，10 轮兜底；SaveLevelAs 首次跨越 0.70 时快照（bOpenSavedLevel=false 编辑器不切关卡） |
| — | LevelPersistenceToolset 7 工具 | UE 侧 SaveLevelAs + LoadLevel 完成，详见 [[level-persistence-toolset]] |

## 当前 interceptor 链顺序
```
DebugPreCall → Readback → ToolCallLogger → StateCache → DriftAlert → VisionInterceptor → SnapshotRecorder
(StopLimitInterceptor 独立挂载，仅兜底 match_reference 硬终止)
```

## 关键机制

### L2 读回验证 (Issue 016 Part A)
- 写工具（set_actor_transform / set_properties）成功后自动调对应读工具（get_actor_transform / get_properties）做值级 diff
- 失配时通过徽章通道向 LLM 警告：`⚠ L2 读回失配: rotation.pitch 意图=15.0 实际=0.0`
- 读回实际值回写 WorldState，标记为"已确认"
- 白名单外工具零开销跳过

### 参考图匹配 (Issue 016 Part B)
- `build_atmosphere_mapping()`：扫描 5 类氛围组件属性 → MiMo 筛选 → 9 维度映射表（refPath 可直接用于 get/set_properties）
- `match_reference(path)`：双图对比 + 9 维度差异 + 5 项量化指标（直方图相似度、R/B 比值、饱和度、亮度、对比度）
- Skill `match-atmosphere`：引导 LLM 按组件逐项调整，每轮 match_reference 验证方向

### 倒计时停止机制 (PLAN_0714 v3)
- **激活**：hist≥0.70 首次跨越时，`_countdown_remaining=3`，`_max_allowed_rounds = match_count + 3`
- **递减**：每轮 match_reference 后 -1
- **硬切断**：倒计时归零或总轮次超限时 pre_call 拦截，返回 isError=True + StopLimitInterceptor 摘要
- **快照**：hist 首次跨越 0.70 时调 SaveLevelAs 保存一次（`bOpenSavedLevel=false` 编辑器留在原关卡）
- **兜底**：始终未达 0.70 时 10 轮硬终止
- **输出**：每轮显示"第 N 轮（最多 M 轮，剩余 X 次调整机会）"

## 待做（2026-07-14 状态）

| 优先级 | 内容 | 说明 |
|:---:|------|------|
| — | Issue 011 安全护栏 | 远期 |
| — | Issue 014 demo | 降级为素材性工作，若录制基准=有/无 Harness 对照 |
| — | 收敛效率优化 | 用户提出的真实痛点：如何减少轮次、避免反复调参越调越差（讨论中，未立项） |

## Session 会话产物
| 文件 | 位置 | 内容 |
|:---|:---|:---|
| `tool_calls.jsonl` | `{session_dir}/` | 工具调用日志（含 L2 读回 verdict） |
| `tool_errors.jsonl` | `{session_dir}/` | SDK validation error 日志（PLAN_0713 新增） |
| `vision_calls.jsonl` | `{session_dir}/` | Vision Q&A 实时配对记录 |
| `instructions.md` | `{session_dir}/` | LLM 收到的行为准则 |
| `screenshots/` | `{session_dir}/` | 截图 PNG + `.verdict.json` |
| `vision_sessions/{id}.json` | `{session_dir}/` | Vision 会话归档 |
| `session.json` | `{session_dir}/` | 主会话元数据 |

Why: 每次会话的起点——快速了解已完成什么、待做什么。避免重做已解决的工作。
How to apply: 对照此清单决定下一步开发优先级。
