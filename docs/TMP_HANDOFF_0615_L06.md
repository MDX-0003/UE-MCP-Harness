# UE Agent Harness 截图 + 多模态 Handoff — 2026-06-15

## 任务概要

实现截图获取 → Vision API 分析 → 结构化返回的端到端管线，为 #007 验证闭环做准备。

管线包含三个独立模块：
- `harness/verification/capturer.py` — 截图获取 + 格式解析 + resize
- `harness/verification/vision_agent.py` — Vision Sub-Agent（独立 LLM API）
- `harness/verification/config.py` — `.vision.env` 独立配置

---

## 完整调用流程

### 当前：CLI 三条路径

```
┌─────────────────────────────────────────────────────────────────┐
│ 命令行入口                                                       │
│                                                                 │
│  A. harness vision check --image <file>                         │
│      本地 PNG → PIL 读取 → resize 1024x768 → base64             │
│                                                                 │
│  B. harness vision check --from-ue                               │
│      connect UE → preload_all_toolsets → CaptureEditorImage     │
│        → 解析嵌套 JSON → 提取 base64 → resize 1024x768          │
│                                                                 │
│  C. harness vision check --image <base64>                        │
│      直接使用 raw base64 字符串                                   │
│                                                                 │
│  三路汇合:                                                       │
│       ↓                                                         │
│  VisionSubAgent.check()                                         │
│    ├─ 描述模式 (无 --expected): VISION_SYSTEM_PROMPT_DESCRIBE    │
│    └─ 验证模式 (有 --expected): VISION_SYSTEM_PROMPT_VERIFY      │
│       ↓                                                         │
│  anthropic.Anthropic(base_url=..., api_key=...)                  │
│    .messages.create(model=..., system=..., messages=[image+text])│
│       ↓                                                         │
│  POST {base_url}/v1/messages → 200 → 提取 text                   │
│       ↓                                                         │
│  _parse_verdict() → stdout JSON                                  │
└─────────────────────────────────────────────────────────────────┘
```

**CLI 路径代码位置**：`harness/cli.py:_cmd_vision()` (line 282-349)

### 未来：外部 Agent 通过 MCP 操控（#007 要做）

```
┌─────────────────────────────────────────────────────────────────┐
│ 外部 LLM (Claude Code 等)                                       │
│   │                                                             │
│   │ MCP tools/call("CaptureEditorImage") 或 Skill 步骤触发      │
│   ▼                                                             │
│ Harness MCP Server (port 9000)                                  │
│   │                                                             │
│   │ server.call_tool() handler                                  │
│   │   ├─ pre interceptor(s)                                     │
│   │   ├─ ue_client.call_tool("CaptureEditorImage")              │
│   │   │   → UE MCP Server (port 8000) → 截图                    │
│   │   ├─ parse SSE result                                       │
│   │   └─ post interceptor(s) ← 在此注入 Vision 分析              │
│   │                                                             │
│   ▼                                                             │
│ [当前缺失] VisionInterceptor (ToolCallInterceptor)               │
│   当 tool_call 是截图类工具时:                                    │
│     1. 从 result 提取 base64（复用 capturer._parse_and_resize）  │
│     2. 调 vision_agent.check() 获取分析结果                      │
│     3. 将分析结果注入 System Context（Tier 1 视觉反馈 slot）     │
│     4. 将原始 base64 从 LLM 可见结果中剥离（只返回文本描述）     │
└─────────────────────────────────────────────────────────────────┘
```

### 两条路径的关键差异对比

| 维度 | CLI `vision check` | 未来 MCP `tools/call` + 拦截器 |
|------|---|---|
| 触发方式 | 命令行手动调用 | LLM 调 MCP 工具自动触发 |
| 截图获取 | `_cmd_vision` 内建 `McpClientSession` | Harness Server 已有的 `ue_client` |
| Vision 分析 | `VisionSubAgent.check()` 同步调用 | `VisionInterceptor.post_call()` 异步注入 |
| 结果流向 | stdout JSON | System Context (Tier 1) 文本注入 |
| 原始 base64 | 发送给 Vision API 后丢弃 | LLM 不可见（被拦截器剥离） |
| 对话历史 | 每次 `vision check` 独立（新进程） | Vision Sub-Agent 保持同一 session（跨轮次） |
| 工具集预加载 | `preload_all_toolsets()` 每次连接时执行 | Harness 启动时已执行 |
| 配置来源 | `.vision.env` + 环境变量 | 同左 |

### #007 落地要做的事（1 天）

1. **新建 `harness/verification/interceptor.py`** — `VisionInterceptor(ToolCallInterceptor)`
   - `post_call`: 检测 `event.name` 是否为 `CaptureEditorImage` / `Screenshot`
   - 是 → 提取 base64 → 调 `vision_agent.check()` → 存入缓存
   - 不是 → 透传（空操作）
2. **扩展 `WorldState`** — 新增字段 `last_vision_verdict: VisionVerdict | None`
3. **修改 `SystemContextProvider.render()`** — 新增"上次视觉反馈"段落
4. **在 `cli.py:cmd_start()` 注册** — `interceptors.append(VisionInterceptor(cache, vision_agent))`

---

## Bug 记录

### Bug #1 — 截图工具名不匹配

**表现**：
```
SlateInspector.Screenshot → HTTP 400 (Unknown tool)
CaptureEditorImage → HTTP 200 / HTTP 400 (取决于工具集是否加载)
```

**根因**：`capturer.py:43` 硬编码的工具全路径 `ToolsetRegistry.Plugin.SlateInspectorToolset.SlateInspector.Screenshot` 在 UE 端不存在。UE MCP 的 SlateInspector 插件可能注册了不同的工具名，或者该工具在当前 UE 配置中未启用。

**修复前**：
```
capture() → SlateInspector.Screenshot (400) → except → 跳过
```

**修复后**：
```
capture() → SlateInspector.Screenshot (400) → except → 日志 debug → 回退方案
         → CaptureEditorImage (200) → 解析结果
```
未修改工具名（`SlateInspector.Screenshot` 的确切全路径需要对照 UE 插件源码确认）。当前通过 try/except + fallback 机制容错。

---

### Bug #2 — 未预加载工具集导致截图工具不可用

**表现**：
```
CaptureEditorImage → HTTP 400: "Unknown tool: ToolsetRegistry.EditorAppToolset.CaptureEditorImage"
```

**根因**：`_cmd_vision --from-ue` 创建 `McpClientSession` 后直接 `connect()` + `call_tool()`，跳过了 `preload_all_toolsets()`。UE MCP Server 默认启用延迟加载（`ModelContextProtocol.DeferredToolLoading`），在工具集被显式 `load_toolset` 之前，只有 `list_toolsets` / `describe_toolset` / `load_toolset` 三个发现工具可用。

**修复前** (cli.py)：
```python
client = McpClientSession(Config(ue_port=args.ue_port))
await client.connect()
print("已连接，正在截图...")
screenshot = await capture(client, ...)
```

**修复后** (cli.py:319-326)：
```python
client = McpClientSession(Config(ue_port=args.ue_port))
await client.connect()
tool_count = await client.preload_all_toolsets()   # ← 新增：预加载 211 个工具
print(f"已加载 {tool_count} 个工具，正在截图...")
screenshot = await capture(client, ...)
```

---

### Bug #3 — `CaptureEditorImage` 返回格式 ≠ 预期 MCP image block

**表现**：
```
Resize 失败: Invalid base64-encoded string: number of data characters (61) cannot be 1 more than a multiple of 4
base64 前 80 字符: {"content":[{"type":"text","text":"Failedtocaptureanyeditorwindows."}],"isError"
截图完成 (0x0)
```

**根因**：最初假设 `CaptureEditorImage` 返回 MCP 标准图片格式：
```json
{"content": [{"type": "image", "data": "<base64>", "mimeType": "image/png"}]}
```

但 UE MCP Server 实际返回的是**双层 JSON 嵌套**——图片数据嵌在 text content block 的 JSON 字符串内：

```
外层: {"content": [{"type": "text", "text": "<inner JSON>"}]}
内层: {"returnValue": {"mimeType": "image/png", "data": "<actual base64>"}}
```

`_parse_and_resize()` 只查找了 `type == "image"` 的 block，找不到 → `b64_data` 退化为整个外层 JSON 字符串 → `base64.b64decode(json_string)` 报 padding 错 → 返回 0x0 的无效数据 → Vision API 收到垃圾 base64 → 400 "the provided base64 data is not valid"。

**修复前** (_parse_and_resize)：
```python
for item in content:
    # 只处理 image block
    if item.get("type") == "image":
        b64_data = item.get("data", "")
        break
```

**修复后** (capturer.py:114-153)：
```python
for item in content:
    # 格式 1: image block（原始逻辑）
    if item.get("type") == "image":
        b64_data = item.get("data", "")
        break

    # 格式 2: text block —— 新增
    if item.get("type") == "text":
        text = item.get("text", "")

        # 格式 2a: UE 嵌套 JSON {"returnValue":{"data":"<base64>"}}
        if text.lstrip().startswith("{") and "returnValue" in text:
            inner = json.loads(text)
            b64_data = inner["returnValue"]["data"]
            break

        # 格式 2b: data URI data:image/png;base64,...
        m = re.search(r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', text)
        if m: b64_data = m.group(1); break

        # 格式 2c: 纯 base64 字符串
        if _looks_like_base64(text):
            b64_data = text; break
```

---

### Bug #4 — UE 错误响应被当截图解析

**表现**：
```
CaptureEditorImage → 200 OK
Content: {"content":[{"type":"text","text":"Failedtocaptureanyeditorwindows."}],"isError":true}
→ _parse_and_resize 尝试 base64 decode "Failedtocaptureanyeditorwindows."
→ Invalid base64-encoded string (padding error)
→ 原始 JSON 字符串传给 Vision API → 400
```

**根因**：`CaptureEditorImage` 在 UE 编辑器没有可见视口时返回 `isError: true`，但 HTTP 状态码仍然是 200。`_parse_and_resize` 没有检查 `isError` 标志，直接把错误文本当截图处理。

**修复前**：
```python
parsed = json.loads(raw)
content = parsed.get("content", [])
# 直接遍历 content 找 image
```

**修复后** (capturer.py:104-112)：
```python
parsed = json.loads(raw)
# 先检查 isError
if parsed.get("isError"):
    err_text = ""
    for item in parsed.get("content", []):
        if item.get("type") == "text":
            err_text = item.get("text", "")
            break
    raise ValueError(f"UE 截图失败: {err_text}")   # ← 明确报错，不再误解析
```

---

### Bug #5 — `--expected` 参数强制必填

**表现**：不加 `--expected` 时报 argparse 错误。但用户经常只是想让 Vision model 自由描述截图，不需要预期描述。

**修复前** (cli.py)：
```python
p_vision_check.add_argument("--expected", required=True, help="预期场景描述")
```

**修复后** (cli.py:214)：
```python
p_vision_check.add_argument("--expected", default="描述截图内容",
    help="预期场景描述（可选，留空则自由描述）")
```

配套修改 `vision_agent.py`：
- 系统 prompt 拆分为 `VISION_SYSTEM_PROMPT_DESCRIBE`（自由描述）和 `VISION_SYSTEM_PROMPT_VERIFY`（验证比对）
- `check()` 方法根据 `expected` 是否为 `None` / `"描述截图内容"` 自动切换模式
- 描述模式：全文作为 `reason` 返回，`pass=True`
- 验证模式：解析 JSON 获取 `pass/reason/adjustment`

---

### Bug #6 — Vision API base_url 路径拼接错误

**表现**：
```
POST https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages → 404
POST https://token-plan-cn.xiaomimimo.com/anthropic/v1/v1/messages → 404
```

**根因**：Anthropic SDK 内部逻辑为 `实际 URL = base_url + '/v1/messages'`。初始默认值 `https://token-plan-cn.xiaomimimo.com/anthropic` 拼出 `.../anthropic/v1/messages`，代理不识别。改为 `.../anthropic/v1` 后拼出 `.../anthropic/v1/v1/messages`（双重 v1）。

**最终解决方案**：用户自行调整为代理正确的 base_url（`https://token-plan-cn.xiaomimimo.com`），SDK 拼出 `.../v1/messages` 后正常连通。

**配置文件**：`.vision.env` — Vision Sub-Agent 唯一的 LLM API 配置入口。

---

### 旁注 — 非 Harness 问题

1. **`/mcp/mcp` 双重路径**：`Config.ue_base_url` 已包含 `/mcp`，`_rpc()` 又追加 `ue_url_path="/mcp"` → 最终请求 `http://127.0.0.1:8000/mcp/mcp`。UE MCP Server 似乎两个路径都接受（`/mcp` 和 `/mcp/mcp`），所以没有功能性影响。这是历史代码遗留问题，不影响当前功能，可择机修复。

2. **`SlateInspector.Screenshot` 工具不可用**：返回 HTTP 400 "Unknown tool"。工具名需要在 UE 插件源码中确认确切的全限定路径。当前通过 fallback 到 `CaptureEditorImage` 规避。

---

## 涉及文件

| 文件 | 角色 |
|------|------|
| `harness/verification/capturer.py` | 截图获取 + 6 种格式解析 + PIL resize |
| `harness/verification/vision_agent.py` | Vision Sub-Agent：双模式系统 prompt + Anthropic API 调用 + JSON 解析 |
| `harness/verification/config.py` | `.vision.env` 加载 + `VisionConfig` dataclass + 模板创建 |
| `harness/config.py` | `vision_api_key` / `vision_api_base_url` / `vision_model` / `vision_max_size` |
| `harness/cli.py` | `_cmd_vision`：三条截图路径 + `--from-ue` / `--image` / `--expected` |
| `.vision.env` | Vision Sub-Agent 独立配置文件（API key + endpoint + model） |
| `tests/test_verification.py` | 24 个测试：配置字段、.vision.env 加载、判决解析、Sub-Agent 状态管理 |

---

## #007 落地时的复用清单

| 已实现能力 | 位置 | #007 如何复用 |
|---|---|---|
| 截图获取 + 格式解析 | `capturer.capture()` / `_parse_and_resize()` | `VisionInterceptor` 直接调用 |
| Vision API 分析 | `vision_agent.check()` / `continue_with_info()` | `VisionInterceptor` 持有 `VisionSubAgent` 实例 |
| 双模式 prompt | `VISION_SYSTEM_PROMPT_DESCRIBE` / `VERIFY` | Skill YAML 的 `verification.expected` 决定模式 |
| base64 resize | `capturer._parse_and_resize()` 内建 PIL | 已有 |
| 追问机制 | `vision_agent.continue_with_info()` | 从 Skill 步骤上下文提取信息后调用 |
| 对话历史 | `VisionSubAgent._history` | 跨轮次保持（同一 Agent Session） |
| .vision.env 配置 | `harness/verification/config.py` | 无需改变 |
| System Context 模板 | `SystemContextProvider.render()` | 新增"视觉反馈"段落 |
| 拦截器链 | `harness/interceptor.py` + `server.py` | 新增 `VisionInterceptor` 插入 post 链 |
