---
name: l2-readback-returnvalue-bug
description: returnValue 双层包装格式未被解析导致的 L2 误报告警 —— 根因、修复方案、修复前后对比
metadata:
  type: bug
---

# L2 读回 returnValue 包装解析 Bug

**发现日期**: 2026-07-08（实机日志验证）

## 现象

L2 读回对所有 `set_properties` 调用报告 "读回结果中缺失"，但紧接着 LLM 手动调 `get_properties` 发现数据完好：

```
⚠ L2 读回失配: set_properties(PointLight_1)
  intensity 读回结果中缺失         ← 误报
{"returnValue":true}

// 手动调 get_properties：
{"returnValue":"{"intensity":2}"}  ← 数据明明在
```

## 根因

UE MCP 的 Python 工具返回值经过**两层包装**：

```
UE Python 工具返回:  {"intensity": 2}
        ↓
ToolsetRegistry 包装: {"returnValue": "{\"intensity\": 2}"}    ← 第 1 层
        ↓
MCP content 包装:     {"content": [{"type": "text", "text": "{\"returnValue\":...}"}]}  ← 第 2 层
```

修复前的 `_parse_readback_result` 只解了第 2 层（MCP content wrapper），拿到了 `{"returnValue": "{\"intensity\": 2}"}` 就直接返回。`_diff_properties` 在这个 dict 里找 `intensity` key → 找不到 → 报告 "读回结果中缺失"。

此外，PostProcessVolume 的 `set_properties` 使用嵌套 JSON（`{"settings": {...}}`），修复前的 `_diff_properties` 只做浅层 key 查找，无法处理嵌套结构。

## 修复方案

**新增 3 个解包函数**：

- `_unwrap_mcp_text(raw: dict) -> str` — 解 MCP content wrapper（从旧代码提取）
- `_unwrap_return_value(text: str) -> dict | None` — **新增**：解 ToolsetRegistry returnValue 包装。检查 `json.loads(text)` 是否有 `returnValue` key，有则递归 `json.loads(rv)` 取内层值
- `_parse_properties_readback(text) -> dict` — 先尝试 `_unwrap_return_value`，失败则直接 JSON 解析

**改进 diff 逻辑**：

- `_diff_properties` 增加嵌套 dict 递归比较：只比对 intent 中的 key 子集，readback 返回的额外字段不触发告警
- 提取 `_values_equal(intent, actual) -> bool` 公共比较函数（数值容差 1e-6，字符串精确）

**拦截器链重排**：`ReadbackInterceptor` 移到 `ToolCallLogger` 之前，确保徽章注入后的 `parsed_text` 被记录到 JSONL。

## 修复前 vs 修复后

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| `set_properties` intensity=2 → UE 返回 intensity=2 | ❌ "intensity 读回结果中缺失" | ✅ 静默通过 |
| `set_properties` intensity=8000 → UE clamp 到 5000 | ❌ "intensity 读回结果中缺失"（未检测到 clamp） | ✅ "intensity 意图=8000 实际=5000" |
| PostProcessVolume settings 嵌套 dict | ❌ "settings 读回结果中缺失" | ✅ 递归子集比较 |
| JSONL 记录 | ❌ 无徽章（Logger 先于 Readback 运行） | ✅ 包含徽章 |

## 涉及文件

- [[l2-readback-mechanism]]: L2 读回完整机制
- `harness/verification/interceptor.py`: ReadbackInterceptor + 解包/解析函数
- `harness/cli.py`: 拦截器链顺序调整
- `tests/test_verification_interceptor.py`: TestReadbackInterceptor 13 个 case

**Why:** returnValue 格式是 ToolsetRegistry 的标准包装（contracts.md §4 有记载），所有通过 ToolsetRegistry 注册的 Python 工具都使用此格式。不处理它导致 L2 永远无法正确读回任何属性值。

**How to apply:** 任何从 ue_client 直接读取 ToolsetRegistry Python 工具返回值的代码，必须调用 `_unwrap_return_value()` 解包。不要假设返回值格式是纯 JSON —— 先用 `_unwrap_mcp_text` 解 MCP 层，再用 `_unwrap_return_value` 解 ToolsetRegistry 层。
