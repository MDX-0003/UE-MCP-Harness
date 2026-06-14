# 003 — 可观测性：全量日志与回放

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

Harness 记录每个 tool call 的完整请求/响应（含时间戳、耗时、错误、截图路径、验证结果），以结构化 JSONL 格式写入 `~/.ue-harness/logs/{session_id}.jsonl`。

基于日志实现回放引擎——读取日志文件，对运行中的 UE 实例按顺序重放 tool call 序列，用于 bug 复现和回归测试。

提供 `harness stats` 命令快速查看工具调用统计。

## 验收标准

- [ ] 每次 tool call（转发给 UE 之前和收到结果之后）自动写入一条 JSON 行到日志文件，无需手动触发
- [ ] 日志行包含：`timestamp`、`session_id`、`task_id`（可选）、`tool_name`、`tool_input`、`tool_output`、`error`、`duration_ms`
- [ ] 日志写入是异步的——不增加 tool call 的感知延迟
- [ ] `harness stats` 输出：总调用数、按工具分组计数、平均/最大耗时、错误率
- [ ] `harness replay <log_file>` 读取日志文件，连接 UE，按顺序重放每个 tool call
- [ ] Replay 模式下跳过验证步骤（截图路径标记为不可重现）
- [ ] Replay 工具调用失败时，输出失败的 step 编号和错误信息，不继续执行
- [ ] 日志文件按 session 自动轮转，最大保留 30 天或 100 个文件（可配置）

## 阻塞

- #002（工具透传——没有 tool call 就没东西可记录）

## 设计说明

日志格式：
```json
{
  "timestamp": "2026-06-13T10:00:00.000Z",
  "session_id": "abc123",
  "task_id": "coffee-shop-001",
  "tool_name": "SceneTools.find_actors",
  "tool_input": {"glob": "DirectionalLight*"},
  "tool_output": "[\"DirectionalLight_0\", \"DirectionalLight_1\"]",
  "error": null,
  "duration_ms": 45
}
```

Logger 以装饰器/中间件形式包裹 `harness/client.py` 的 `call_tool()` 方法，而非在业务逻辑中手动插桩。
