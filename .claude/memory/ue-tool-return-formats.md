---
name: ue-tool-return-formats
description: UE MCP 工具的返回值存在两层包装：MCP content 数组 + ToolsetRegistry returnValue，且各工具包装不一致
metadata:
  type: gotcha
---

# UE MCP 工具返回值：两层包装不一致

## 两层包装结构

UE MCP 工具的返回值经过两层包装：

### 第一层：MCP 协议层（所有工具一致）

```json
{"content": [{"type": "text", "text": "..."}]}
```

这是 MCP 标准的 `CallToolResult` 格式，所有 UE 工具统一使用。`ue_client.call_tool()` 返回的就是这个格式的 JSON 字符串。

### 第二层：ToolsetRegistry `returnValue` 包装（各工具不一致）

`content[0].text` 内部可能是：

```
格式 A（直接）: "[75 fields] a, b, c"
格式 B（returnValue）: {"returnValue": "[75 fields] a, b, c"}
格式 C（returnValue + 嵌套 JSON）: {"returnValue": "{\"field\": {...}}"}
```

## 已知的各工具包装行为

| 工具 | 内层格式 | 备注 |
|------|---------|------|
| `find_actors` | returnValue（格式 B） | `{"returnValue": [{"refPath": "..."}, ...]}` |
| `get_properties` | returnValue + 嵌套 JSON（格式 C） | `{"returnValue": "{\"field\": {...}}"}` |
| `list_properties` | **不一致** — 有时格式 A（直接文本），有时格式 B（returnValue） | 可能随 UE 状态/toolset 加载阶段变化 |
| `GetCameraTransform` | returnValue（格式 B） | `{"returnValue": {"location": {...}, "rotation": {...}}}` |
| `SetCameraTransform` | returnValue | `{"returnValue": null}` |
| `GetSelectedActors` / `GetVisibleActors` | returnValue（格式 B） | 同 find_actors |

## 解析函数的正确分工

| 函数 | 解哪层 | 位置 |
|------|--------|------|
| `_read_sse_stream` / `call_tool` | 解 JSON-RPC 协议层，提取 `result` 字段（MCP content 数组） | `client.py` |
| `_extract_parsed_text` | 解 MCP content 数组 → 提取 `text` 字符串 | `server.py:1812` |
| `_unwrap_mcp_text` | 同上（L2 读回专用） | `verification/interceptor.py:311` |
| `_unwrap_return_value_text` | 解 returnValue 外层包装（`{"returnValue": "text"}` → 内层字符串） | `server.py:1386` |
| `_try_unwrap_return_value` | 解 returnValue + 内层 JSON（`{"returnValue": "{...}"}` → dict） | `server.py:1439` |
| `_extract_actor_names` | 解 MCP content + returnValue → actor refPath 列表 | `server.py:1250` |
| `_extract_property_names` | 从纯文本提取属性名（支持多行 `name: type` 和单行逗号分隔） | `server.py:1269` |

## 血的教训

1. **永远不要假设 UE 工具的返回格式。** 同一个工具（如 `list_properties`）在不同调用中可能返回不同格式。
2. **在 `_extract_property_names` 之前必须先调 `_unwrap_return_value_text`。** 否则 `{"returnValue"` 的 JSON key 片段会被当作属性名。
3. **测试 mock 无法覆盖这个场景**——mock 返回的是固定格式，不会模拟 UE 的格式漂移。真实 UE 调用是唯一可靠的验证手段。
4. **`_extract_property_names` 需要 `{` 前缀防御**——即使上游解包失败，也不应把 JSON 片段当属性名。

**Why:** 2026-07-12 的 `build_atmosphere_mapping` 真实调用发现映射表中所有属性名都被截断为 `{"returnValue"`。追溯到 `list_properties` 有时在 `content[0].text` 中包裹了 `returnValue` JSON 外壳，而解析管线缺少这一层解包。

**How to apply:** 
- 任何需要解析 UE 工具返回值的代码，在提取文本后先调 `_unwrap_return_value_text()` 再传给属性解析器
- 新增 Harness 自有工具 handler 时，直接连接真实 UE 验证，不要仅依赖 mock 测试
- 参见 `[[build-atmosphere-mapping-plan]]` 中的 Task 补丁记录
