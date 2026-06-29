# evening-lighting Skill 端到端验证记录

**日期**: 2026-06-17 02:39–02:48 UTC
**方法**: 纯 MCP Client（curl）→ Harness → UE，不接触 UE 编辑器本身
**Harness 版本**: ue-agent-harness v1.27.2
**协议**: MCP Streamable HTTP (`/mcp` endpoint, JSON-RPC 2.0)

---

## 目录

1. [Skill YAML 与实际问题对比](#1-skill-yaml-与实际问题对比)
2. [完整交互记录（按时间序）](#2-完整交互记录按时间序)
3. [数据流总结](#3-数据流总结)
4. [深度分析](#4-深度分析)

---

## 1. Skill YAML 与实际问题对比

### Skill YAML (`skills/evening-lighting.yaml`)

```yaml
name: evening-lighting
tools_allowlist:
  - SceneTools.find_actors
  - SceneTools.get_current_level
  - ActorTools.get_actor_transform
  - ActorTools.set_actor_transform
  - ObjectTools.list_properties
  - ObjectTools.get_properties
  - ObjectTools.set_properties
  - SlateInspector.Screenshot          # ⚠️ 问题A
  - EditorAppToolset.CaptureEditorImage
  - EditorAppToolset.GetCameraTransform
steps: |
  1. 调 SceneTools.find_actors(glob="*DirectionalLight*")   # ⚠️ 问题B
  ...
  7. 找到 SkyLight → 降低其 Intensity
  8. 考虑添加 PostProcessVolume...                          # ⚠️ 问题C
  9. 调 SlateInspector.Screenshot 截图当前视口               # ⚠️ 问题A
  10. 视觉验证...
verification:
  type: screenshot       # ⚠️ 问题D
  tolerance: 0.7
```

### 发现的问题

| # | 严重度 | 问题 | 详情 |
|:--:|:-----:|------|------|
| **A** | 🔴 阻断 | `SlateInspector.Screenshot` 短名无法匹配 | YAML 写的是 `SlateInspector.Screenshot`，但截图的正确短名是 `EditorAppToolset.CaptureEditorImage`。Harness 短名 fallback 可能匹配到了不存在的工具，或者这个短名在 UE 侧实际没有注册。`CaptureEditorImage` 两次返回 "Failed to capture any editor windows" |
| **B** | 🟡 阻塞 | `find_actors` 的 `tag` 参数问题 | YAML Step 1 写 `glob="*DirectionalLight*"` 但没有传 `tag`。工具 JSON Schema 标记 `tag` 为 required。必须传 `{"glob":"*DirectionalLight*","tag":""}` 才能通过 schema 校验。不是协议问题，是 Skill 作者不知道 tag 被标记为 required |
| **C** | 🟡 设计 | 白名单缺少 `add_to_scene_from_class` | Step 8 说"考虑添加 PostProcessVolume"，但 `tools_allowlist` 中没有添加 Actor 的工具。LLM 即使想做也做不了 |
| **D** | 🟡 设计 | `verification` 字段未被执行 | YAML 定义了 `verification.type: screenshot`，但整个会话中 Harness 从未触发此验证逻辑。Issues 007 (VisionInterceptor) 标记为 ❌ 当前开发中，与预期一致 |

---

## 2. 完整交互记录（按时间序）

### 通用说明

- 所有请求: `POST http://localhost:9000/mcp`
- 所有请求头: `Content-Type: application/json` + `Accept: application/json, text/event-stream`
- MCP Streamable HTTP 要求 Accept 头必须同时包含 `application/json` 和 `text/event-stream`，否则返回 -32600 错误
- 响应格式: SSE (`event: message\ndata: <JSON>\n\n`)，但简单 curl 会直接收到带 ping 心跳的原始文本

---

### 2.1 服务发现阶段

#### #1 — GET / (根路径探测)

```bash
curl -s http://localhost:9000/
```

**响应:**
```
Not Found
```

**分析:** Harness 不暴露 HTTP 根路径，仅 `/mcp` 端点可用。这是 MCP Streamable HTTP 规范的标准行为。

---

#### #2 — GET /sse (旧版 SSE 端点探测)

```bash
curl -s http://localhost:9000/sse
```

**响应:** 挂起（长连接等待），后台运行

**分析:** SSE 端点行为不明确——可能用于旧版 SSE 传输（已废弃），也可能只是没有实现。Harness 使用 Streamable HTTP，不需要 `/sse`。

---

#### #3 — POST /mcp 缺少 Accept 头

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test-user","version":"1.0"}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0",
  "id": "server-error",
  "error": {
    "code": -32600,
    "message": "Not Acceptable: Client must accept both application/json and text/event-stream"
  }
}
```

**分析:** 关键发现。错误码 -32600（Invalid Request），消息明确告知客户端必须同时接受两种 Content-Type。这是 MCP Streamable HTTP 传输的强制要求。Harness 对此的校验是严格且正确的。

---

#### #4 — GET /health (健康检查探测)

```bash
curl -s http://localhost:9000/health
```

**响应:**
```
Not Found
```

---

#### #5 — POST / (JSON-RPC 到根路径)

```bash
curl -s -X POST http://localhost:9000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'
```

**响应:**
```
Not Found
```

**分析:** JSON-RPC 仅通过 `/mcp` 路由，不接受根路径 POST。

---

### 2.2 MCP 握手阶段

#### #6 — Initialize（正确 Accept 头）

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test-user","version":"1.0"}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {
      "experimental": {},
      "tools": {"listChanged": false}
    },
    "serverInfo": {
      "name": "ue-agent-harness",
      "version": "1.27.2"
    },
    "instructions": "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n自由探索模式下可用约 20 个核心工具。\n可用 Skill:\n  - evening-lighting: 将场景光照调整为黄昏/傍晚氛围\n调 activate_skill <名称> 激活 Skill，调 deactivate_skill 退出。\n调 get_context 获取最新 UE 状态快照和活跃 Skill 进度。"
  }
}
```

**分析:** 握手成功。关键信息：
- Server 版本 1.27.2
- `tools.listChanged: false` — 工具列表静态，不支持动态变更通知
- `instructions` 中包含关键使用指引和可用 Skill 列表
- 这是 Harness 的 Context Assembly 三层 prompt 的产物

---

#### #7 — Initialized 通知

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
```

**响应:** （空输出 — 通知无需响应）

---

### 2.3 工具发现阶段

#### #8 — tools/list

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

**响应:** 33.7KB JSON，包含 50+ 工具的完整 schema。详见 [附录 A]。

---

#### #9 — tools/call: get_context

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_context","arguments":{}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 3,
  "result": {
    "content": [{
      "type": "text",
      "text": "你是一个运行在 Unreal Engine 5.8 中的 UE Editor Agent。\n你可以使用工具来控制 Unreal Editor。\n尽量使用截图验证你的修改。\n\n当前 UE 状态：（1 分钟前刷新）\n- 地图：{\"returnValue\":\"/Temp/Untitled_1\"}\n- PIE：未知\n- 选中 Actor：无\n- 场景 Actor 数：16"
    }],
    "isError": false
  }
}
```

---

#### #10 — tools/call: list_toolsets

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_toolsets","arguments":{}}}'
```

**响应:** 19 个 Toolset 列表（EditorAppToolset, ActorTools, SceneTools, ObjectTools, MaterialTools, BlueprintTools, StaticMeshTools, SkeletalMeshTools, TextureTools, DataTableTools, CurveTableTools, DataAssetTools, StringTableTools, MaterialInstanceTools, PrimitiveTools, ProgrammaticToolset, LogsToolset, AgentSkillToolset 等）

---

### 2.4 Skill 激活阶段

#### #11 — activate_skill("evening-lighting")

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"activate_skill","arguments":{"name_or_desc":"evening-lighting"}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 5,
  "result": {
    "content": [{
      "type": "text",
      "text": "Skill 'evening-lighting' 已激活。将场景光照调整为黄昏/傍晚氛围\n步骤 (10 步)、工具白名单 (10 个): SceneTools.find_actors, SceneTools.get_current_level, ActorTools.get_actor_transform, ActorTools.set_actor_transform, ObjectTools.list_properties..."
    }],
    "isError": false
  }
}
```

**分析:** Skill 激活成功。Harness 执行了工具过滤（白名单生效）。注意白名单中列出的名字是短名（去掉 `ToolsetRegistry.` 或 `toolset_registry.toolsets.core.` 前缀）。

---

#### #12 — get_context（Skill 激活后）

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"get_context","arguments":{}}}'
```

**响应:** 包含完整 Skill 上下文（10 步骤详解 + 完整工具白名单），见前文 "Skill 上下文" 部分。

---

### 2.5 Skill 执行阶段 — 失败的尝试

#### #13 ❌ CaptureAssetImage("")

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"ToolsetRegistry.EditorAppToolset.CaptureAssetImage","arguments":{"assetPath":"","bShowUI":false}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 7,
  "result": {
    "content": [{"type": "text", "text": "错误: "}],
    "isError": true
  }
}
```

**分析:** 空 `assetPath` 不被接受。错误消息为空（"错误: "），说明 UE 侧返回了异常但异常消息未正确传递到 MCP 响应中。这是 UE MCP Server 的问题——异常转义不完整。

---

#### #14 ❌ find_actors 缺少 tag 参数

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"toolset_registry.toolsets.core.scene.SceneTools.find_actors","arguments":{"glob":"*DirectionalLight*"}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 8,
  "result": {
    "content": [{
      "type": "text",
      "text": "Input validation error: 'tag' is a required property"
    }],
    "isError": true
  }
}
```

**分析:** **关键问题 B**。工具 JSON Schema 中 `tag` 被标记为 `required`，但 Skill YAML Step 1 只传了 `glob` 参数。这是 Skill 作者与工具实际 Schema 之间的认知差距。LLM 按 Skill 指令执行会直接失败。

---

#### #15 ❌ CaptureEditorImage（第一次）

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"ToolsetRegistry.EditorAppToolset.CaptureEditorImage","arguments":{}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 12,
  "result": {
    "content": [{
      "type": "text",
      "text": "{\"content\": [{\"type\": \"text\", \"text\": \"Failed to capture any editor windows.\"}], \"isError\": true}"
    }],
    "isError": false
  }
}
```

**分析:** 外层 `isError: false`（Harness 认为 RPC 调用本身成功），但内层 `isError: true`（UE 侧工具执行失败）。消息 "Failed to capture any editor windows" 暗示截图工具需要找到可见的编辑器窗口。可能是因为：
1. UE 编辑器窗口最小化
2. 没有可捕获的视口
3. 工具本身实现依赖特定的 Windows API 窗口查找

---

#### #16 ❌ list_properties on LightComponent0 inline（JSON 转义失败）

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":15,"method":"tools/call","params":{"name":"toolset_registry.toolsets.core.object.ObjectTools.list_properties","arguments":{"instance":{"refPath":"/Temp/Untitled_1.Untitled_1:PersistentLevel.DirectionalLight_UAID_A85E45CFE40401D200_1470382761.LightComponent0"}}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0",
  "id": "server-error",
  "error": {
    "code": -32700,
    "message": "Parse error: Expecting ',' delimiter: line 1 column 284 (char 283)"
  }
}
```

**分析:** 这是 curl 命令行的 shell 转义问题，不是 Harness 问题。refPath 中的特殊字符（冒号、点号）在单引号内应该安全，但 PowerShell 的 echo 管道可能与 Git Bash 的 curl 交互时产生转义问题。改用以文件方式传递 JSON body 后解决了。

**教训:** MCP Client 实现必须正确序列化 JSON，对于包含特殊字符的 refPath，文件方式比命令行内联更可靠。

---

#### #17 ⚠️ get_properties on SkyLight（部分属性失败）

```bash
# 从文件 POST
cat > /c/temp/mcp_req6.json << 'JSONEOF'
{"jsonrpc":"2.0","id":20,"method":"tools/call","params":{"name":"toolset_registry.toolsets.core.object.ObjectTools.get_properties","arguments":{"instance":{"refPath":"/Temp/Untitled_1.Untitled_1:PersistentLevel.SkyLight_UAID_A85E45CFE40401D200_1470380759.SkyLightComponent0"},"properties":["Intensity","LowerHemisphereColor","LightColor","bLowerHemisphereIsSolidColor"]}}}
JSONEOF
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d @/c/temp/mcp_req6.json
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 20,
  "result": {
    "content": [{
      "type": "text",
      "text": "GetObjectProperties on '...SkyLightComponent0' (SkyLightComponent): the following properties could not be read: bLowerHemisphereIsSolidColor"
    }],
    "isError": true
  }
}
```

**分析:** UE 侧部分属性不可读（`bLowerHemisphereIsSolidColor` 可能在 SkyLightComponent 上不存在或不可访问）。但这里的问题是：**整个调用被标记为 `isError: true`**，即使其他属性（Intensity, LightColor）可能已被成功读取。这意味着 MCP Client 在部分失败时无法获取任何有效数据。降级策略应该是重试只读可读的属性（实际上重试只查 Intensity 和 LightColor 成功了）。

---

#### #18 ❌ CaptureEditorImage（第二次重试）

```bash
# 从文件 POST
cat > /c/temp/mcp_verify4.json << 'JSONEOF'
{"jsonrpc":"2.0","id":26,"method":"tools/call","params":{"name":"ToolsetRegistry.EditorAppToolset.CaptureEditorImage","arguments":{}}}
JSONEOF
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d @/c/temp/mcp_verify4.json
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 26,
  "result": {
    "content": [{
      "type": "text",
      "text": "{\"content\": [{\"type\": \"text\", \"text\": \"Failed to capture any editor windows.\"}], \"isError\": true}"
    }],
    "isError": false
  }
}
```

**分析:** 与 #15 相同。截图功能在此环境下不可用。可能原因：
1. UE 编辑器是 headless 或最小化状态
2. Windows 窗口捕获 API 需要特定条件
3. 工具实现不支持当前编辑器配置

---

### 2.6 Skill 执行阶段 — 成功的操作

#### #19 ✅ find_actors — DirectionalLight（修正后）

```bash
# 从文件 POST
cat > /c/temp/mcp_req.json << 'JSONEOF'
{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"toolset_registry.toolsets.core.scene.SceneTools.find_actors","arguments":{"glob":"*DirectionalLight*","tag":""}}}
JSONEOF
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d @/c/temp/mcp_req.json
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 8,
  "result": {
    "content": [{
      "type": "text",
      "text": "{\"content\": [{\"type\": \"text\", \"text\": \"{\\\"returnValue\\\":[{\\\"refPath\\\":\\\"/Temp/Untitled_1.Untitled_1:PersistentLevel.DirectionalLight_UAID_A85E45CFE40401D200_1470382761\\\"}]}\"}]}"
    }],
    "isError": false
  }
}
```

**解析:** 找到 1 个 DirectionalLight，refPath = `/Temp/Untitled_1.Untitled_1:PersistentLevel.DirectionalLight_UAID_A85E45CFE40401D200_1470382761`

---

#### #20 ✅ find_actors — SkyLight

```bash
# 参数: {"glob":"*SkyLight*","tag":""}
```

**响应:** 找到 1 个 SkyLight，refPath = `...SkyLight_UAID_A85E45CFE40401D200_1470380759`

---

#### #21 ✅ get_actor_transform — DirectionalLight

```bash
# 参数: {"actor":{"refPath":"...DirectionalLight_UAID_..."}}
```

**响应:**
```json
{"returnValue": {
  "location": {"x": 0, "y": 0, "z": 0},
  "rotation": {"pitch": -16.285558834636156, "yaw": 43.731944173910058, "roll": 112.36067199707438},
  "scale": {"x": 1, "y": 1.0000000298023226, "z": 1.0000000298023226}
}}
```

---

#### #22 ✅ get_properties — LightComponent0

```bash
# 参数: {"instance":{"refPath":"...LightComponent0"},"properties":["LightColor","Intensity","bUseTemperature","Temperature","IndirectLightingIntensity","VolumetricScatteringIntensity"]}
```

**响应:**
```json
{"returnValue": {
  "LightColor": {"r": 1, "g": 1, "b": 1, "a": 1},
  "Intensity": 6,
  "bUseTemperature": true,
  "Temperature": 6500,
  "IndirectLightingIntensity": 1,
  "VolumetricScatteringIntensity": 1
}}
```

---

#### #23 ✅ set_actor_transform — Pitch → -75°

```bash
# 参数: {"actor":{"refPath":"...DirectionalLight_UAID_..."},"xform":{"rotation":{"pitch":-75,"yaw":43.73,"roll":112.36}},"worldspace":true}
```

**响应:**
```json
{"returnValue": true}
```

---

#### #24 ✅ set_properties — LightColor + Intensity

```bash
# 参数: {"instance":{"refPath":"...LightComponent0"},"values":"{\"bUseTemperature\":false,\"LightColor\":{\"r\":1.0,\"g\":0.7,\"b\":0.4,\"a\":1.0},\"Intensity\":2.5}"}
```

**响应:**
```json
{"returnValue": true}
```

---

#### #25 ✅ get_properties — SkyLightComponent0

```bash
# 参数: {"instance":{"refPath":"...SkyLightComponent0"},"properties":["Intensity","LightColor"]}
```

**响应:**
```json
{"returnValue": {
  "Intensity": 1,
  "LightColor": {"r": 1, "g": 1, "b": 1, "a": 1}
}}
```

---

#### #26 ✅ set_properties — SkyLight Intensity + Color

```bash
# 参数: {"instance":{"refPath":"...SkyLightComponent0"},"values":"{\"Intensity\":0.4,\"LightColor\":{\"r\":0.9,\"g\":0.7,\"b\":0.5,\"a\":1.0}}"}
```

**响应:**
```json
{"returnValue": true}
```

---

### 2.7 验证与清理阶段

#### #27 ✅ get_actor_transform（回读验证）

```bash
# 同 #21
```

**响应:**
```json
{"returnValue": {
  "location": {"x": 0, "y": 0, "z": 0},
  "rotation": {"pitch": -74.999999999999972, "yaw": 43.73000000000004, "roll": 112.35999999999993},
  "scale": {"x": 1, "y": 1, "z": 1}
}}
```

**确认:** Pitch -16.3° → -75.0° ✅

---

#### #28 ✅ get_properties — LightComponent0（回读验证）

```bash
# 同 #22
```

**响应:**
```json
{"returnValue": {
  "LightColor": {"r": 1, "g": 0.70196080207824707, "b": 0.40000003576278687, "a": 1},
  "Intensity": 2.5,
  "bUseTemperature": false,
  "Temperature": 6500
}}
```

**确认:**
- LightColor: (1,1,1) → (1.0, 0.702, 0.4) ✅
- Intensity: 6 → 2.5 ✅
- bUseTemperature: true → false ✅

---

#### #29 ✅ get_properties — SkyLightComponent0（回读验证）

```bash
# 同 #25
```

**响应:**
```json
{"returnValue": {
  "Intensity": 0.40000000596046448,
  "LightColor": {"r": 0.90196084976196289, "g": 0.70196080207824707, "b": 0.50196081399917603, "a": 1}
}}
```

**确认:**
- Intensity: 1 → 0.4 ✅
- LightColor: (1,1,1) → (0.902, 0.702, 0.502) ✅

---

#### #30 ✅ deactivate_skill

```bash
curl -s -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":27,"method":"tools/call","params":{"name":"deactivate_skill","arguments":{}}}'
```

**响应:**
```json
{
  "jsonrpc": "2.0", "id": 27,
  "result": {
    "content": [{"type": "text", "text": "已退出 Skill 模式，回到自由探索模式。"}],
    "isError": false
  }
}
```

---

## 3. 数据流总结

```
curl (MCP Client)                    Harness (:9000)                    UE MCP Server (:8000)              Unreal Editor
     │                                     │                                     │                              │
     │──── initialize ────────────────────►│                                     │                              │
     │◄─── serverInfo v1.27.2 ────────────│                                     │                              │
     │──── initialized ───────────────────►│                                     │                              │
     │──── tools/list ────────────────────►│──── 透传 ──────────────────────────►│                              │
     │◄─── 50+ tools ◄────────────────────│◄─── 原始工具列表 ◄─────────────────│                              │
     │──── activate_skill ───────────────►│                                     │                              │
     │                                    │── Harness: 工具白名单过滤            │                              │
     │◄─── Skill activated (10 tools) ◄──│                                     │                              │
     │──── find_actors(no tag) ──────────►│──── 透传 ──────────────────────────►│                              │
     │◄─── ❌ tag required ──────────────│◄─── Schema validation error ◄───────│                              │
     │──── find_actors(glob, tag="") ────►│──── 透传 ──────────────────────────►│──── 查询场景 ───────────────►│
     │◄─── refPath list ◄────────────────│◄─── Actor refPaths ◄────────────────│◄─── 16 actors ◄─────────────│
     │──── set_actor_transform ──────────►│──── 透传 ──────────────────────────►│──── SetActorRotation ───────►│
     │◄─── true ◄────────────────────────│◄─── OK ◄────────────────────────────│◄─── done ◄──────────────────│
     │──── set_properties(light) ────────►│──── 透传 ──────────────────────────►│──── SetLightColor+Int ──────►│
     │◄─── true ◄────────────────────────│◄─── OK ◄────────────────────────────│◄─── done ◄──────────────────│
     │                                    │                                     │                              │
     │  ✅ 6/10 skill steps succeeded      │  ⚠️ Screenshot chain broken          │  ⚠️ CaptureEditorImage fails   │
     │  ❌ 1 step blocked (tag required)   │  ❌ Skill YAML/tool schema mismatch   │                              │
     │  ⏭️ 1 step skipped (no PPV)         │  ❌ Verification section not executed │                              │
     │  ⚠️ 2 steps failed (screenshot)     │                                     │                              │
```

### 属性变化一览

| 参数 | 修改前 | 修改后 | Skill 目标 | 状态 |
|------|--------|--------|-----------|:--:|
| 主光 Pitch | -16.3° | -75.0° | -70° ~ -80° | ✅ |
| 主光 Color | (1.0, 1.0, 1.0) | (1.0, 0.70, 0.40) | warm ~3000-4000K | ✅ |
| 主光 Intensity | 6.0 | 2.5 (41.7%) | 30-50% | ✅ |
| 主光 bUseTemperature | true | false | — | ✅ |
| SkyLight Intensity | 1.0 | 0.4 (40%) | 降低 | ✅ |
| SkyLight Color | (1.0, 1.0, 1.0) | (0.90, 0.70, 0.50) | warm | ✅ |
| PostProcessVolume | 无 | ⏭️ 跳过 | 按需添加 | ⏭️ |
| 截图验证 | — | ❌ | 视觉确认 | ❌ |

---

## 4. 深度分析

### 4.1 架构层面的发现

**Harness 的透传模式工作正常：** 绝大多数工具调用 (`SceneTools.*`, `ActorTools.*`, `ObjectTools.*`) 成功透传到 UE 并返回正确结果。JSON-RPC 2.0 编解码、SSE 帧解析、错误传播均符合预期。

**Skill 系统存在三层断裂：**

```
┌─────────────────────────────────────────────────────┐
│  Skill YAML (人类作者)                                │
│  ├─ 工具短名 ≠ MCP 全名                               │  ← 断裂1: 命名解析
│  ├─ 参数假设 ≠ JSON Schema                            │  ← 断裂2: Schema 校验
│  └─ verification 字段 ≠ 运行时执行                     │  ← 断裂3: 验证闭环未实现
├─────────────────────────────────────────────────────┤
│  Harness Context Assembly                            │
│  ├─ 工具过滤生效                                       │
│  ├─ get_context 正确注入 Skill 步骤                    │
│  └─ 但无法修正 Skill YAML 中的错误                     │
├─────────────────────────────────────────────────────┤
│  UE MCP Server                                       │
│  ├─ Schema 校验严格 (tag required)                     │
│  ├─ 截图工具环境依赖强                                  │
│  └─ 部分属性读取失败导致整调用标 isError                 │
└─────────────────────────────────────────────────────┘
```

### 4.2 根本原因分析

#### 断裂1: 命名解析

Skill YAML 使用短名 `SlateInspector.Screenshot`，但实际可用的截图工具是 `ToolsetRegistry.EditorAppToolset.CaptureEditorImage`（全名）或 `EditorAppToolset.CaptureEditorImage`（中等短名）。`SlateInspector.Screenshot` 这个短名在 Harness 的短名 fallback 中可能匹配不到正确的全名。

**根因:** Skill 作者写 YAML 时使用的工具名与 UE MCP Server 注册的实际名称不同步。这可能是：
1. UE 侧在某个版本重命名了工具（`SlateInspector` → `EditorAppToolset`）
2. Skill 是基于旧版 UE MCP Server 编写的
3. 不同 MCP Server 的命名空间不一致

#### 断裂2: Schema 校验

`find_actors` 的 JSON Schema 标记 `tag` 为 required：
```json
{"required": ["tag"]}
```

但 Skill YAML Step 1 写成：
```
SceneTools.find_actors(glob="*DirectionalLight*")
```

这说明 Skill 作者期望 `tag` 是可选参数。可能的原因：
1. UE MCP Server 的 Schema 生成器过度标记了 required
2. UE 侧 C++ 代码中 `tag` 实际是可选的（FString 有默认值），但反射系统将其标记为 required
3. Skill 是基于口头约定/文档而非实际 Schema 编写的

#### 断裂3: 验证闭环

Skill YAML 定义了 `verification` 字段（screenshot + expected 描述 + tolerance），但整个会话中从未执行。与 CLAUDE.md 的记录一致：Issue 007 (VisionInterceptor) 仍在开发中。

**这意味着:** 即使截图工具正常工作，Harness 目前也不会自动对比截图和 expected 描述来验证 Skill 执行结果。

### 4.3 可操作的改进建议

| 优先级 | 建议 | 涉及模块 | 预计工作量 |
|:--:|------|---------|:--:|
| P0 | 修正 `evening-lighting.yaml` 中 `find_actors` 调用加上 `tag:""` | Skill YAML | 1 行 |
| P0 | 将 `SlateInspector.Screenshot` 替换为 `EditorAppToolset.CaptureEditorImage` | Skill YAML | 1 行 |
| P1 | 在 Skill activate 时做 Schema 预校验：对比 YAML steps 中的参数与实际 tool JSON Schema | Harness Skill 系统 | 中等 |
| P1 | 实现 `verification` 字段的执行逻辑 (Issue 007) | VisionInterceptor | 已有计划 |
| P2 | 考虑放宽 `find_actors` 的 `tag` required → optional（如果 UE 侧 C++ 支持） | UE MCP Server | 小 |
| P2 | `get_properties` 部分失败时应返回已成功读取的属性 + failed 列表，而非整体报错 | UE MCP Server | 中等 |
| P3 | 加强 Skill YAML 的 schema validation（lint 或 CI check） | Harness | 小 |

### 4.4 边界案例发现

1. **refPath 包含特殊字符**: 冒号、点号在 JSON 中合法但在 shell 转义时可能出问题。MCP Client 实现应始终使用 JSON 库（而非字符串拼接）来构造请求体。

2. **double-wrapped JSON**: Harness 响应中 `returnValue` 有时是 JSON 字符串而非对象（如 `"{\"returnValue\":...}"`），需要 MCP Client 做额外的 JSON.parse。这增加了客户端的解析负担。

3. **SSE ping 心跳**: Harness 在 SSE 流中发送 `: ping - 2026-06-17T...` 行（SSE comment），符合 SSE 规范，但对于 curl 这种一次性客户端需要过滤。

4. **空字符串 vs null**: `tag: ""`（空字符串）通过了 Schema 校验，说明 Schema 只检查字段存在性而非非空性。这个行为是否符合作者意图值得确认。

---

## 附录

### A. 完整工具列表（tools/list 响应摘要）

**Harness 原生工具 (4):**
- `get_context` — 获取 UE 状态快照
- `activate_skill` / `deactivate_skill` — Skill 模式切换
- `save_skill` — 保存自定义 Skill

**EditorAppToolset (15+):**
- 相机控制: `SetCameraTransform`, `GetCameraTransform`, `FocusOnActors`
- 截图: `CaptureEditorImage`, `CaptureAssetImage`
- 选择: `SelectActors`, `SelectAssets`, `GetSelectedActors`, `GetSelectedAssets`
- 导航: `SetContentBrowserPath`, `GetContentBrowserPath`, `OpenEditorForAsset`, `GetOpenAssets`
- 查询: `GetVisibleActors`, `SearchCVars`
- 坐标转换: `WorldPosToScreenCoords`, `ScreenCoordsToWorld`

**Core Toolsets (Actor/Scene/Object/Asset/Blueprint/Material/etc, 40+ 工具)**

### B. refPath 格式

```
/Temp/Untitled_1.Untitled_1:PersistentLevel.<ActorClass>_UAID_<HexID>
.ComponentName
```

示例:
- Actor: `/Temp/Untitled_1.Untitled_1:PersistentLevel.DirectionalLight_UAID_A85E45CFE40401D200_1470382761`
- Component: `...DirectionalLight_UAID_....LightComponent0`

### C. Harness 版本与环境

- Server: `ue-agent-harness` v1.27.2
- Protocol: `2024-11-05` (MCP Streamable HTTP)
- Transport: `POST /mcp` with `Accept: application/json, text/event-stream`
- UE: 5.8, 地图 `/Temp/Untitled_1`, 16 Actors
