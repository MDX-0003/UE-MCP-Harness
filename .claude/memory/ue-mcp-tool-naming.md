---
name: ue-mcp-tool-naming
description: UE MCP 工具名的完全限定格式 vs 短名，以及 Harness 内两种调用路径的名称解析差异
metadata:
  type: gotcha
---

# UE MCP 工具名：完全限定 vs 短名

## 核心事实

UE MCP Server 注册的工具名是**完全限定格式**：

```
toolset_registry.toolsets.core.scene.SceneTools.find_actors
toolset_registry.toolsets.core.object.ObjectTools.list_properties
toolset_registry.toolsets.core.object.ObjectTools.get_properties
```

## 两条调用路径的名称解析差异

### 路径 A：LLM → Harness `call_tool` → UE（短名可用）

LLM 发起的工具调用走 `server.py:call_tool()` → UE 透传段：
```python
result_text = await ue_client.call_tool(name, arguments)
```

这条路径中，LLM 收到的工具列表来自 Harness `list_tools`（返回 UE 原始全名），LLM 用全名调用。Harness 在 SSE 协议层/UE MCP Server 端存在名字解析，短名有时也能工作——但**不可依赖此行为**。

### 路径 B：Harness handler 直接调 `ue_client.call_tool()`（必须全名）

Harness 自有工具 handler（如 `build_atmosphere_mapping`、`match_reference`）内部直接调 `ue_client.call_tool()`：

```python
# server.py call_tool 函数内
result_text = await ue_client.call_tool("SceneTools.find_actors", ...)  # ❌ 失败
result_text = await ue_client.call_tool("find_actors", ...)             # ❌ 失败
result_text = await ue_client.call_tool(
    "toolset_registry.toolsets.core.scene.SceneTools.find_actors", ...   # ✅ 正确
)
```

**这条路径绕过了 Harness 的工具名解析层——必须使用 UE 的完全限定名。**

## 发现正确工具名的方法

1. 通过 Harness `ue_client.list_tools()` 获取全量工具列表
2. 或直连 UE 的 8000 端口做 MCP 握手后调 `tools/list`
3. **不要**凭 allowlist 中的短名模式（如 `"SceneTools."`）猜测——那是过滤模式，不是真实名字

**Why:** 在 `build_atmosphere_mapping` 实现中连续两次犯错（先用了 `SceneTools.find_actors`，又改成了 `find_actors`），两次都返回 "Unknown tool"。直接探头 8000 端口才确认了完全限定格式。所有 Harness 主动调 UE 工具的 handler 代码都必须用完全限定名。

**How to apply:** 写新的 Harness 自有工具 handler 时，如果需要内部调 `ue_client.call_tool()`：
1. 先确认目标工具的完全限定名（查 `tools/list` 或现有 log）
2. 在代码中使用完全限定名
3. 写测试验证名称正确（参考 `[[test-build-atmosphere-mapping]]`）
4. 在代码注释中标注这是直调路径，需要全名
