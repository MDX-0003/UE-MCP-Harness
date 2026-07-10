# UE MCP `find_actors` glob 参数不可靠问题

**日期：** 2026-07-10
**关联：** [[ue-mcp-tool-naming]]、[[build_atmosphere_mapping]]

## 现象

`build_atmosphere_mapping` handler 调用 `find_actors` 查找 5 类氛围组件时，5 次全部返回空数组——但场景中确实存在这些 Actor。

## 证据链

### 1. `find_actors` tool schema（直连 UE 8000 端口获取）

```json
{
  "name": "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
  "inputSchema": {
    "type": "object",
    "properties": {
      "root":       {"type": "object", "title": "/Script/Engine.Actor",   "default": null},
      "glob":       {"type": "string", "default": "*"},
      "actor_type": {"type": "object", "title": "/Script/CoreUObject.Class", "default": null},
      "tag":        {"type": "string"}
    },
    "required": ["tag"]
  }
}
```

4 个参数，仅 `tag` 必填。`glob` 做名称模式匹配，`actor_type` 做类引用精确匹配。

### 2. 真实会话对比

| 来源 | glob 参数 | tag | 结果 |
|------|----------|-----|------|
| 我们的 handler（v1） | `*DirectionalLight*` | `""` | **空** |
| 我们的 handler（v2） | `*SkyAtmosphere*` | `""` | **空** |
| 我们的 handler（v3） | `*ExponentialHeightFog*` | `""` | **空** |
| LLM 手动尝试 1 | `*DirectionalLight*` | `*` | **空** |
| LLM 手动尝试 2 | `*Light*` | `""` | **找到 2 个** |
| LLM 手动尝试 3 | `*Sky*` | `""` | **找到 2 个** |
| LLM 手动尝试 4 | `*Fog*` | `""` | **找到 1 个** |
| LLM 手动尝试 5 | `*Cloud*` | `""` | **找到 1 个** |
| LLM 手动尝试 6 | `actor_type: "/Script/Engine.DirectionalLight"` | `""` | **找到 1 个** |

### 3. 结论

- `*DirectionalLight*` 无法匹配 `DirectionalLight_UAID_A85E45CFE40401D200_1470382761`，但 `*Light*` 能匹配
- 规律：长 glob 模式（>8 字符通配内容）在 UE 5.8 `find_actors` 实现中不可靠
- `actor_type`（class refPath）是精确匹配，5 次测试全部成功
- `tag` 参数：`"*"` 会匹配所有有标签的 Actor（大部分 Actor 无标签，故返回空）；`""` 不过滤标签

## UE 源码位置

`find_actors` 工具注册在 Engine MCP ToolLibrary 框架中（`IModelContextProtocolTool` 接口）。具体实现在 Blueprint 或编译后的 C++ 中，无法从源码直接审查 glob 匹配逻辑。

关键文件：
- `Engine/Plugins/Experimental/ModelContextProtocol/Source/ModelContextProtocol/Public/IModelContextProtocolTool.h` — 工具接口定义
- `Engine/Plugins/Experimental/ModelContextProtocol/Source/ModelContextProtocolEngine/Private/ModelContextProtocolToolLibrary.cpp` — BP/C++ 工具注册框架

## 已采取的修复

`build_atmosphere_mapping` handler 改用 `actor_type` + UE class refPath：

```python
ATMOSPHERE_TYPES: dict[str, str] = {
    "DirectionalLight": "/Script/Engine.DirectionalLight",
    "SkyAtmosphere": "/Script/Engine.SkyAtmosphere",
    "ExponentialHeightFog": "/Script/Engine.ExponentialHeightFog",
    "VolumetricCloud": "/Script/Engine.VolumetricCloud",
    "PostProcessVolume": "/Script/Engine.PostProcessVolume",
}

# 调用
ue_client.call_tool(
    "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
    {"tag": "", "actor_type": {"refPath": class_path}},
)
```

## 给未来开发者的建议

1. **永远用 `actor_type`（class refPath）而非 `glob`** 做按类型查找 Actor。`actor_type` 是 UE 类系统的精确匹配，不依赖 display name。
2. **UE MCP 的 glob 匹配实现不可靠**，仅对短通配符（`*Light*`）有效，长通配符（`*DirectionalLight*`）返回空。
3. **`tag` 必须传 `""`（空字符串）**——`"*"` 不是通配符，而是字面匹配有标签的 Actor。
4. **工具名必须用完全限定格式**——`toolset_registry.toolsets.core.scene.SceneTools.find_actors`，不能用 `SceneTools.find_actors` 或 `find_actors`。详见 [[ue-mcp-tool-naming]]。
5. **要发现正确的工具名和参数 schema**，直连 UE 8000 端口调 `tools/list`——不要凭 allowlist 模式猜测。6
6. **所有 handler 内直调 `ue_client.call_tool()` 的代码必须在测试中验证参数名和工具名**——参考 `tests/test_build_atmosphere_mapping.py::test_uses_correct_tool_names`。
