# PLAN_0621 — Harness→UE HTTP 流式传输修复

**创建日期:** 2026-06-21
**状态:** ✅ 已实施 (2026-06-21)

---

## 开发计划摘要

| # | 变更 | 文件 | 预计工作量 |
|---|---|---|---|
| 1 | `call_tool()` 改用 `response.aiter_lines()` 增量读取 SSE，遇 result/error 事件立即返回；移除 `_extract_tool_result` | `harness/client.py` | 核心 |
| 2 | URL 收拢：`ue_base_url` = 完整外部 URL，代码不再追加任何路径 | `harness/config.py` → `harness/client.py` × 6 处 | 小 |
| 3 | `sse_read_timeout` 默认值统一：消除类默认值(120)与 `from_env` 默认值(60)的冲突 | `harness/config.py` | 微小 |
| 4 | 更新 `parse_sse_stream` 单元测试，补充流式 SSE 解析测试 | `tests/test_client.py` | 小 |

---

## Bug 表现

- **症状：** Harness 启用时 `tool_verify_harness_vision.py` 全部 mode（viewport/editor/asset）报 `httpx.ReadTimeout`，60s 超时
- **直接测试 `tool_verify_ue_vision.py`（绕过 Harness）全部通过**
- **日志特征：**

```
15:27:27 调用工具: ToolsetRegistry.EditorAppToolset.CaptureAssetImage (id=28)
15:27:28 HTTP Request: POST http://127.0.0.1:8000/mcp/mcp "HTTP/1.1 200"   ← HTTP 200 立即返回
15:28:28 ReadTimeout                                                       ← 60s 后超时，SSE body 未读完
```

- `CaptureEditorImage` 和 `GetSelectedAssets` 能正常返回（~16s），不会触发超时
- 直接测试使用 `streamablehttp_client(timeout=120, sse_read_timeout=120)` 正常完成

---

## 根因分析

### 根因：`response.content` 阻塞式读取 SSE 流

[`harness/client.py:217`](d:\Programs\2024-2\ue-agent-harness\harness\client.py#L217) — `McpClientSession.call_tool()`：

```python
if "text/event-stream" in content_type:
    return self._extract_tool_result(rid, response.content)  # ← 阻塞
```

`response.content` 是 httpx 的**同步汇聚**方法——它会读取整个响应体直到连接关闭才返回。UE MCP Server 的 `tools/call` 返回 SSE event-stream：服务器发送 HTTP 200 头后，通过 chunked transfer 逐步发送 SSE 事件，渲染耗时较长的工具（`CaptureAssetImage`）在处理期间会保持连接打开。`response.content` 因此阻塞，直到超时。

`streamablehttp_client`（MCP Python SDK）使用的是**增量式** SSE 解析：逐行读取 `response.aiter_lines()`，遇到 `data: {"result": ...}` 事件立即返回，不等待连接关闭。这才是正确的 SSE 消费方式。

### 为什么 `CaptureEditorImage` 和 `GetSelectedAssets` 能工作

推测这些工具的 UE 端执行较快（~16s），且 UE MCP Server 在发送 result 事件后主动关闭了连接。`CaptureAssetImage` 的视口渲染耗时更长，期间 SSE 连接保持打开。

### 次要 Bug：URL 重复 `/mcp/mcp`

[`harness/config.py:70`](d:\Programs\2024-2\ue-agent-harness\harness\config.py#L70) — `ue_base_url` 已包含 `/mcp`：

```python
return f"http://{self.ue_host}:{self.ue_port}{self.ue_url_path}"  # → ...8000/mcp
```

[`harness/client.py:205`](d:\Programs\2024-2\ue-agent-harness\harness\client.py#L205) — 每次 POST 再追加 `ue_url_path`：

```python
self._http.post(self._config.ue_url_path, ...)  # 又追加 /mcp → /mcp/mcp
```

`ue_base_url` 的 4 处引用 + `ue_url_path` 的 4 处 POST 全受影响。虽然 UE MCP Server 当前对所有 `/mcp*` 路径一视同仁（所以并非超时的直接原因），但这是脆弱的——未来 Server 路由变更会直接导致全线故障。

### 次要 Bug：`sse_read_timeout` 默认值不一致

| 代码路径 | 默认值 | 位置 |
|---|---|---|
| `Config()` 类构造 | **120s** | [config.py:32](d:\Programs\2024-2\ue-agent-harness\harness\config.py#L32) |
| `Config.from_env()` 生产路径 | **60s** | [config.py:86](d:\Programs\2024-2\ue-agent-harness\harness\config.py#L86) |

注释明确说 `CaptureAssetImage(viewport) 返回 1MB+ 需 60-70 秒`，但生产默认值 60s 刚好卡在边界。流式修复后这个差异不再致命（增量读取会提前返回），但仍应收拢以避免类似冲突。

---

## 详细实施计划

### 变更 1：`call_tool()` 流式 SSE 读取

**文件:** `harness/client.py`

**改动：** 重写 `call_tool()`：
1. 用 `self._http.stream("POST", ...)` 替代 `self._http.post()` — `post()` 在非流式模式下会预读整个响应体才返回，导致长时间无数据到达时 ReadTimeout
2. SSE 分支用 `_read_sse_stream()` 增量解析替代 `_extract_tool_result()` 的阻塞式 `response.content`
3. JSON 分支显式调用 `await response.aread()` 后再 `.json()`

**关键洞察：** httpx 的 `post()` 默认在返回前读取完整响应体。即使换成 `aiter_lines()`，只要 `post()` 先执行，流式读取永远不会被调用。`stream()` 在收到 HTTP 响应头后立即返回，body 延迟消费 — 这才是正确的 SSE 消费方式。

**伪代码：**

```python
# call_tool() 中替换第 214-229 行
content_type = response.headers.get("content-type", "")

if "text/event-stream" in content_type:
    return await self._read_sse_stream(rid, response)
elif "application/json" in content_type:
    data = response.json()
    ...

# 新增私有方法
async def _read_sse_stream(self, request_id: int, response) -> str:
    """增量读取 SSE 事件流，遇 result/error 立即返回。"""
    current_event = None
    current_data = None
    async for line in response.aiter_lines():
        if line == "":
            # 空行 = 事件分隔符
            if current_data is not None:
                try:
                    data = json.loads(current_data)
                    if "result" in data:
                        result = data["result"]
                        return json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
                    if "error" in data:
                        err = data["error"]
                        raise JsonRpcError(err.get("code", -1), err.get("message", "Unknown error"), err.get("data"))
                except json.JSONDecodeError:
                    pass
            current_event = None
            current_data = None
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data = line[5:].strip()
        # 忽略注释行 (: 开头) 和其他字段

    raise JsonRpcError(-32000, f"SSE 流结束但未找到工具结果 (request_id={request_id})")
```

**同时移除：**
- `_extract_tool_result()` 方法（第 363-389 行）——仅被 `call_tool()` 调用，变为死代码
- `parse_sse_stream()` **保留**——仍被 `_rpc()` 和单元测试使用

**要点：**
- 不设置新的超时——httpx 的 `sse_read_timeout` 在流式读取时作用于每个 `aiter_lines()` 迭代之间的间隔，而非整体时长。这意味着即使渲染耗时 90s，只要数据在持续到达就不会超时。
- 保持与现有 `JsonRpcError` 异常体系的兼容
- `_rpc()` 本次不动，留到后续计划统一改造

---

### 变更 2：URL 收拢

**核心理念：** `ue_base_url` 是用户提供的完整外部 URL，Harness 代码不对其追加任何路径。

**文件:** `harness/config.py`

```python
# 修改前
ue_url_path: str = "/mcp"

@property
def ue_base_url(self) -> str:
    return f"http://{self.ue_host}:{self.ue_port}{self.ue_url_path}"  # 已是完整 URL

# 修改后
# ue_url_path 字段移除
# ue_base_url 改为用户直接提供的完整 URL

ue_base_url: str = "http://127.0.0.1:8000/mcp"  # 直接存储完整 URL

@classmethod
def from_env(cls) -> Config:
    return cls(
        ue_port=int(os.getenv("HARNESS_UE_PORT", "8000")),
        ue_host=os.getenv("HARNESS_UE_HOST", "127.0.0.1"),
        ue_base_url=os.getenv("HARNESS_UE_BASE_URL",
            f"http://{os.getenv('HARNESS_UE_HOST', '127.0.0.1')}:{os.getenv('HARNESS_UE_PORT', '8000')}/mcp"),
        # ...
    )
```

等等——这样改大了。更小侵入的方式：

**方案（推荐）：** 保留 `ue_port`/`ue_host`，让 `ue_base_url` 成为最终完整 URL，移除 `ue_url_path`。

```python
# config.py
ue_port: int = 8000
ue_host: str = "127.0.0.1"
# ue_url_path: str = "/mcp"         ← 删除

@property
def ue_base_url(self) -> str:
    """UE MCP Server 完整 base URL。所有请求直接使用此 URL，不追加路径。"""
    return f"http://{self.ue_host}:{self.ue_port}/mcp"
```

**文件:** `harness/client.py` — 所有 `.post(ue_url_path, ...)` → `.post("", ...)` 或不传路径参数。

受影响的调用点（共 6 处）：

| 行号 | 方法 | 当前代码 | 修改为 |
|---|---|---|---|
| 153 | `close()` | `.delete(ue_url_path, ...)` | `.delete("", ...)` |
| 205 | `call_tool()` | `.post(ue_url_path, ...)` | → 流式重写，直接传 URL |
| 280 | `_rpc()` | `.post(ue_url_path, ...)` | `.post("", ...)` |
| 预计 ~340 | `_read_sse_stream()` | 新方法 | N/A |

**文件:** `harness/config.py` — `merge_cli_overrides` 中移除 `ue_url_path` 字段。

**文件:** `harness/cli.py` — 如果有引用 `ue_url_path` 的地方也需更新。

---

### 变更 3：`sse_read_timeout` 默认值统一

**文件:** `harness/config.py`

**策略：** 类默认值作为唯一真实来源，`from_env` 在未设置环境变量时使用 `None`，fallback 到类默认值。

```python
# 修改前
@dataclass
class Config:
    sse_read_timeout: float = 120.0  # 类默认值

@classmethod
def from_env(cls) -> Config:
    return cls(
        sse_read_timeout=float(os.getenv("HARNESS_SSE_READ_TIMEOUT", "60.0")),  # env 默认值冲突！
    )

# 修改后
@dataclass
class Config:
    sse_read_timeout: float = 120.0  # 唯一默认值

@classmethod
def from_env(cls) -> Config:
    sse_read_timeout_str = os.getenv("HARNESS_SSE_READ_TIMEOUT")
    sse_read_timeout = float(sse_read_timeout_str) if sse_read_timeout_str else 120.0
    return cls(
        sse_read_timeout=sse_read_timeout,
    )
```

或者更优雅的方式——只在 `from_env` 中传 env 值，不传时依赖 `dataclass` 默认值。但这要求 `from_env` 动态构建 kwargs，改动略大。上面的显式写法更直观。

---

### 变更 4：单元测试更新

**文件:** `tests/test_client.py`

1. `parse_sse_stream` 的现有测试保留（`_rpc` 仍在使用）
2. 新增针对 `_read_sse_stream` 的测试：
   - SSE 流含 `result` 事件 → 正确返回
   - SSE 流含 `error` 事件 → 正确抛出 `JsonRpcError`
   - SSE 流含 progress 通知后跟 result → progress 被忽略，result 正确返回
   - SSE 流在 result 前异常断开 → 抛出正确的错误
   - 多行 data（单事件跨多行）→ 正确拼接
3. URL 相关：如果 `ue_url_path` 被移除，检查 CLI 测试是否有引用

---

## 不在本次范围内

- `_rpc()` 的流式改造 → 后续计划
- `preload_all_toolsets` 的 `_rpc` 调用 → 后续计划
- `_rpc` 与 `call_tool` 的代码合并 → 后续计划（两处有大量重复的 SSE 处理逻辑）
