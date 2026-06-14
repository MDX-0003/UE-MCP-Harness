# 010 — UE 感知错误恢复

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

实现 UE 语义的错误分类器和重试策略引擎。当 tool call 失败时，Harness 不直接把错误返回给 LLM，而是先对错误分类（PIE_RUNNING、MAP_LOADING、ASSET_LOCKED、TIMEOUT 等），按类别应用预定义的重试策略。

例如：PIE 正在运行导致 `add_to_scene_from_class` 失败 → Harness 自动等待 PIE 结束 → 重试（最多 3 次）→ 重试成功才返回 LLM。这样 LLM 甚至不会感知到瞬时错误。

## 验收标准

- [ ] 错误分类器能识别以下错误类别（至少 4 类）：
  - `PIE_RUNNING`：tool 返回 "Cannot create actors while PIE is active"
  - `ASSET_LOCKED`：tool 返回 "cannot be saved" 或 "unsaved changes"
  - `TIMEOUT`：SSE 流 30 秒无响应
  - `UNKNOWN`：其他所有错误
- [ ] `PIE_RUNNING` 类：等待 3 秒 → 重试 → 最多 3 次 → 仍失败则上报 LLM
- [ ] `TIMEOUT` 类：重试一次 → 仍超时则上报 LLM
- [ ] `UNKNOWN` 类：立即上报 LLM，不重试
- [ ] 重试逻辑对 LLM 透明——LLM 只看到最终成功结果或最终失败错误
- [ ] 所有重试记录到可观测性日志
- [ ] 错误分类器可扩展——添加新错误类别只需新增一个匹配规则 + 重试策略

## 阻塞

- #008（State Cache——需要缓存来辅助判断 PIE 状态等上下文）

## 设计说明

错误分类通过检查 tool call 返回的错误字符串进行：
```python
class ErrorClassifier:
    RULES = [
        (r"Cannot create.*PIE is active", ErrorCategory.PIE_RUNNING),
        (r"cannot be saved|unsaved changes", ErrorCategory.ASSET_LOCKED),
        (r"timeout|timed out", ErrorCategory.TIMEOUT),
    ]
```

重试策略不用于视觉验证 FAIL——视觉 FAIL 是合理的业务结果（"还不够黄昏"），交给 LLM 决策。重试仅用于**基础设施错误**（PIE 冲突、超时、资产锁）。
