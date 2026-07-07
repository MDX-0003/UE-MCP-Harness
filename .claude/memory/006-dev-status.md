---
name: dev-status-2026-07-07
description: 2026-07-07 开发进度——331 tests，P0/P1 bug 基本清零，Vision 闭环完整
metadata:
  type: project
---

# 开发进度 2026-07-07

## 全量测试: 331 passed, 4 skipped

## 已完成 Issue

| Issue | 模块 | 说明 |
|:---:|------|------|
| 001-008, 012, 015 | 骨架/透传/日志/Context/Skill/Vision/State/验证闭环/重连/Vision Session | 全部完成 |
| PLAN_0706 | class_name 会话内补全 | 四层写入路径 + `_parse_actor_list` Bug 2 修复。见 [[plan-0706-class-name]] |
| PLAN_0707 | Vision 统一结构化输出 | `VisionVerdict` 重构 + unified prompt + `response_format` JSON 模式 + `vision_calls.jsonl`。见 [[vision-unified-output]] |
| Bug Fixes | P0-1/P0-2/P0-3/P1-4/P1-9/P0-8 | 全部已解决。Bug_analysis_0706.md 追踪状态 |
| — | 灯光 SOP instructions | 光照验证前置检查 + 附近几何体注入 |
| — | Vision 追踪 | `vision_calls.jsonl` + `instructions.md` + 自动归档 `close_active()` |

## 待做

| 优先级 | 内容 | 说明 |
|:---:|------|------|
| P1 | P1-10 残留: Vision confidence=low 时 Harness 端追加警告 | `server.py` 检测 caveats 触发 |
| P2 | P1-5 残留: tool_calls.jsonl off-by-one | `vision_calls.jsonl` 已覆盖 |
| P2 | P2-7 截图高度偏低 (1024×321) | `vision_max_size` 加高度下限 |
| P2 | P1-6 Session 归档统计失真 | 纯记账问题 |
| — | Issue 011 安全护栏 | 远期 |
| — | Issue 014 闭环验收场景 | 远期 |

## 当前 interceptor 链顺序
```
DebugPreCall → ToolCallLogger → StateCache → DriftAlert → VisionInterceptor → SnapshotRecorder
```

## Vision API 配置
- Base URL: `https://token-plan-cn.xiaomimimo.com`
- Model: `mimo-v2.5-pro`
- `response_format: {"type": "json_object"}` 通过 `extra_body` 启用
- `max_tokens=4096`（新结构化格式需要更多空间）
- Key: 通过 `.vision.env` 或 `HARNESS_VISION_API_KEY` 环境变量
- 见 [[vision-model-config]]

## Session 会话产物
| 文件 | 位置 | 内容 |
|:---|:---|:---|
| `tool_calls.jsonl` | `{session_dir}/` | 工具调用日志（含 off-by-one verdict） |
| `vision_calls.jsonl` | `{session_dir}/` | Vision Q&A 实时配对记录 |
| `instructions.md` | `{session_dir}/` | LLM 收到的行为准则 |
| `screenshots/` | `{session_dir}/` | 截图 PNG + `.verdict.json` |
| `vision_sessions/{id}.json` | `{session_dir}/` | Vision 会话归档 |
| `session.json` | `{session_dir}/` | 主会话元数据 |

Why: 每次会话的起点——快速了解已完成什么、待做什么。避免重做已解决的工作。
How to apply: 对照此清单决定下一步开发优先级。
