---
name: l2-readback-mechanism
description: L2 读回验证的伪代码原理、数据流、拦截器链位置、返回值注入机制
metadata:
  type: reference
---

# L2 Readback Mechanism (Issue 016 Part A)

## 伪代码原理

```
LLM calls write_tool(args)
        │
        ▼
server.py: call_tool() → ue_client.call_tool() → UE 执行写入
        │
        ▼
ReadbackInterceptor.post_call(event):
  1. extract_short_name(event.name) → "set_properties" / "set_actor_transform"
  2. 查 _READBACK_MAP → "get_properties" / "get_actor_transform"
  3. normalize_tool_args(short, event.args) → NormalizedCall(actor_name, payload)
  4. event.name.replace(short, readback_short) → 读回工具全限定名
  5. _build_readback_args() → {"instance": refPath, "properties": [...keys]}
  6. ue_client.call_tool(readback_full, readback_args) → 直接调 UE，不经过拦截器链
  7. _parse_readback_result() → 解包 MCP content + returnValue → 实际值 dict
  8. _diff_values(intent, actual) → 比较，取 mismatches 列表
  9. 如有失配 → 注入徽章到 event.parsed_text（LLM 看到）
     如通过 → _confirm_cache() 更新 WorldState
     如读回调用自身失败 → 注入失败徽章
```

## 白名单

| 写工具 | 读回工具 | UE 源码依据 |
|--------|---------|-----------|
| `set_actor_transform` | `get_actor_transform` | actor.py:134 永远 `return True` |
| `set_properties` | `get_properties` | object.py:91 返回裸 bool |
| ~~`set_label`~~ | — | actor.py:40 内置读回 `== label`，冗余 |
| ~~`add_to_scene_from_*`~~ | — | scene.py:112/139 返回 actor 对象，冗余 |

## 拦截器链位置

```
DebugPreCall → ReadbackInterceptor → ToolCallLogger → StateCache → DriftAlert → Vision → SnapshotRecorder
```

**ReadbackInterceptor 必须在 ToolCallLogger 之前** —— 这样徽章注入后的 `event.parsed_text` 才能被 Logger 写入 JSONL。

## 双层解包

UE MCP 工具返回值经过两层包装：
1. **MCP content wrapper**: `{"content": [{"type": "text", "text": "..."}]}`
2. **ToolsetRegistry returnValue wrapper**: `{"returnValue": "<json_string>"}`

解析链路：`_unwrap_mcp_text(raw)` → `_unwrap_return_value(text)` → 实际值 dict

## 返回值注入

失配时修改 `event.parsed_text`，注入格式：
```
⚠ L2 读回失配: set_properties(PointLight_1)
  intensity 意图=8000 实际=5000

{"returnValue":true}
```

读回失败时：
```
⚠ L2 读回失败: get_properties(PointLight_1) — 连接超时

{"returnValue":true}
```

server.py 在拦截器链全部跑完后，将 `event.parsed_text` 同步回 `result_text`，LLM 在下一次对话轮看到。

## Diff 策略

- Transform: translation/rotation/scale3d 的 x/y/z 轴分别比较，浮点容差 1e-3
- Properties: 按 intent 的属性名子集逐一比较。嵌套 dict 递归只比对 intent 中的 key（readback 返回的额外字段不触发告警）
- 数值比较用 1e-6 容差，字符串精确匹配

**Why:** 解决了 [[006-dev-status]] 中 ADR 0008 要求但一直未实现的"写后读回"确定性验证。参考 [[003-vision-interceptor-007]]（同模块的另一个拦截器，Vision 分析）。

**How to apply:** 实现新拦截器时参考 `ReadbackInterceptor.post_call()` 的模式：白名单匹配 → 提取意图值 → 直接调 UE → 解析返回值（注意双层解包）→ diff → 注入徽章。readback 调用必须直接走 `ue_client.call_tool()` 避免递归触发拦截器链。
