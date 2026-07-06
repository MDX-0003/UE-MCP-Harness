# Branch Mark — Harness 改动效果评估

独立于 Harness 的离线评分工具。读取 JSONL 日志，基于 tool call 序列模式匹配输出评估指标。

```
branch-mark show <task>                 # 打印任务指令
branch-mark score <jsonl> [baseline]   # 评分 / A/B 对比
```

## 流程

```
# 1. 打印任务指令
branch-mark show modify_and_verify

# 2. 复制指令 → 粘贴给 LLM → 等待完成任务

# 3. LLM 完成后，评分
branch-mark score .ue-harness/logs/<session_id>/tool_calls.jsonl

# 4. 改 Harness 后重复 1-3，保存两份 JSONL 做 A/B 对比
branch-mark score baseline.jsonl after.jsonl
```

## 评估指标

| 指标 | 类型 | 含义 |
|------|:--:|------|
| L2 读回验证 | bool | 写操作后是否读了 Actor 属性确认写入值 |
| 针对性提问 | bool | 截图时是否带了具体验证问题（非 "描述场景"） |
| 问题具体度 | 0-1 | 提问是否包含 Actor 名、属性名等可验证细节 |
| 追问使用 | bool | 是否调了 vision_ask 深入分析 |
| Session 关闭 | bool | 是否调了 vision_reset |
| 闭环完成 | bool | 完整走了 L2→截图→追问→关闭 全流程 |
| 总 tool call 数 | int | 会话中工具调用总数 |
| Vision API 调用次数 | int | vision_screenshot + vision_ask 总次数 |

## 加权总分

```
L2 读回验证    15%
针对性提问     20%
问题具体度     15%
追问使用       10%
Session 关闭   15%
闭环完成       25%
────────────────100
```

## 目录

```
branch_mark/
├── score.py                      # 评分引擎
├── tasks/
│   ├── modify_and_verify.yaml    # 任务 1：修改 + 验证
│   └── describe_and_check.yaml   # 任务 2：观察 + 确认
└── README.md                     # 本文件
```
