---
name: snapshot-recorder
description: SnapshotRecorder — 会话级状态快照与归档
metadata:
  type: project
---

# SnapshotRecorder — 会话快照归档

## 实现
- **新建**: `harness/observability/snapshotter.py` — `SnapshotRecorder(ToolCallInterceptor)`
- **新建**: `tests/test_snapshotter.py` — 12 tests
- **修改**: `harness/cli.py` — 拦截器链注册 + shutdown 时写 session.json
- **修改**: `harness/server.py` — `build_server()` 新增 `snapshot_recorder` 参数

## 目录结构
```
.ue-harness/logs/{session_id}/
├── {session_id}.jsonl              ← ToolCallLogger（已有）
├── session.json                    ← 会话元数据
├── screenshots/
│   ├── {ts}_{tool}.png             ← 截图 PNG
│   └── {ts}_{tool}.verdict.json    ← Vision 判决
├── contexts/
│   ├── {ts}_context.txt            ← get_context 全量文本
│   └── {ts}_state.json             ← WorldState JSON
└── skills/
    ├── {ts}_activate_{name}.yaml   ← 激活时的 Skill YAML
    └── {ts}_deactivate.txt         ← 停用标记
```

## 触发时机
| 事件 | 保存内容 |
|------|------|
| 截图工具成功（有 base64） | PNG + verdict JSON |
| get_context 调用成功 | context.txt + state.json |
| activate_skill | Skill YAML 副本 |
| deactivate_skill | 停用时间戳 |
| Harness shutdown | session.json（started/ended/tool_count/vision_count/skills/map_path） |

## 惰性创建
目录和文件仅在首次写入时创建。JSONL 为空 = 无工具调用；screenshots/ 不存在 = 无成功截图。

Why: 让 Harness 的内部状态和外部 MCP 调用结果可追溯、可复盘。
How to apply: 重启 Harness 后自动生效。日志目录在 `{repo}/.ue-harness/logs/{session_id}/`。
