# preload_all_toolsets() SSE 阻塞修复

日期：2026-07-01

Harness 启动后 `full_refresh()` 调用 `find_actors` 报 `Unknown tool`。根因是 `preload_all_toolsets()` 使用阻塞式 `_rpc()` 调用 SSE 协议的 `tools/call`，无法正确处理 UE MCP Server 的 `MultipleWriteStream` + `keep-alive` 连接，导致工具集预加载静默失败。

**路径约定：** 所有文件路径相对于 `UE-MCP-Harness` 根目录。

---

## 问题链路

```
cli.py:cmd_start()
  ├─ await ue_client.preload_all_toolsets()   ← 预加载所有工具集
  │     │
  │     ├─ _rpc("tools/call", {"name": "list_toolsets", ...})
  │     │     └─ httpx.post() → response.content (阻塞读 SSE)
  │     │           ↑ UE Server 用 MultipleWriteStream 分块写入
  │     │           ↑ Connection: keep-alive, 连接不关闭
  │     │           ↑ httpx 等待 → 超时/数据不完整 → 静默失败
  │     │
  │     └─ _rpc("tools/call", {"name": "load_toolset", ...})  ← 同样路径
  │           └─ 同上，工具集内工具从未注册到 MCP 模块
  │
  ├─ await full_refresh(ue_client, _cache)
  │     └─ ue_client.call_tool("toolset_registry...SceneTools.find_actors", ...)
  │           └─ UE MCP Server: "Unknown tool" (HTTP 400, code -32602)
  │
  └─ L3 刷新失败，State Cache 为空
```

---

## 根因

### UE MCP Server 的 SSE 响应机制

UE MCP Server 对所有 `tools/call` 请求使用 SSE event-stream 响应（`Content-Type: text/event-stream`），分两阶段写入：

1. **第一阶段**：立即返回 SSE 流头（`MultipleWriteStream | HasAdditionalWrites`），`Connection: keep-alive`
2. **第二阶段**：工具执行完成后，通过回调 lambda 写入 SSE result frame（`MultipleWriteStream | SkipHeaderWrite`）

两阶段之间连接保持活跃（keep-alive + chunked transfer encoding），无 Content-Length。

### 阻塞式 `_rpc()` 的问题

[`_rpc()`](harness/client.py:360) 用 `httpx.post()` 加 `response.content` 做阻塞读：

```
_rpc("tools/call", ...)
  ├─ httpx.post(url, json=payload)
  │     └─ 等 Transfer-Encoding: chunked 的终结 chunk
  ├─ response.content  ← 阻塞，必须等所有 chunk 到达或连接关闭
  └─ parse_sse_stream(response.content)
```

`httpx.post()` 返回后，整个响应体必须已经就绪。但 UE 的 `MultipleWriteStream` 在第一阶段写 SSE 头后不关闭连接，httpx 的阻塞读要等到第二阶段的 result frame 写入并发送终结 chunk 后才返回。若服务器端的回调延迟或连接保持开放，httpx 在 `sse_read_timeout`（120s）内等不到终结 chunk → 超时 → `_rpc()` 抛异常或返回不完整数据。

### 为什么 `call_tool()` 没问题

[`call_tool()`](harness/client.py:240) 用 `httpx.stream()` 而非 `httpx.post()`：

```
call_tool(name, args)
  ├─ _http.stream("POST", url, ...)  ← 返回后立即拿到响应头
  ├─ _read_sse_stream(rid, response)
  │     ├─ async for line in response.aiter_lines()  ← 逐行读 SSE
  │     │     收到空行 → 检查累计 data 是否含 "result"
  │     │     含 "result" → return result
  │     │     含 "error"  → raise JsonRpcError
  │     └─ 遇 result 立即返回，不等待连接关闭
  └─ response.aclose()  ← 主动关闭连接
```

`_read_sse_stream()` 逐行增量消费 SSE 流，在收到 result event 后立即返回，不等待 HTTP 连接关闭。随后 `aclose()` 主动关闭底层 TCP 连接，避免残留。

---

## 修复

### 变更点 1：`preload_all_toolsets()` 改用 `call_tool()`

[harness/client.py:549](harness/client.py#L549)

**旧代码** — 用 `_rpc()` 阻塞 POST：

```python
resp, _ = await self._rpc("tools/call", {
    "name": "list_toolsets", "arguments": {},
})
if resp.error:
    return 0
catalog_text = resp.result["content"][0]["text"]
```

**新代码** — 用 `call_tool()` SSE 流式读取：

```python
result_str = await self.call_tool("list_toolsets", {})
catalog_text = _extract_text_from_result(result_str)
```

`load_toolset` 调用同样改为 `call_tool()`，并增加了 `isError` 检测——`load_toolset` 失败时返回 `isError: true`，此前代码未处理此情况。

### 变更点 2：新增 `_extract_text_from_result()` 辅助函数

[harness/client.py:65](harness/client.py#L65)

`call_tool()` 返回的是 JSON-RPC `result` 对象的 JSON 字符串（如 `'{"content":[{"type":"text","text":"..."}]}'`），需要从中提取文本：

```
_extract_text_from_result(result_str):
    尝试 JSON 解析 result_str
        解析失败 → 原样返回 result_str（可能已是纯文本）
    如果 result 是 dict:
        遍历 result["content"]，找 type=="text" 的项
        如果没找到 → 尝试 result["text"]、result["structuredContent"]
        都没找到 → 返回 ""
```

### 变更点 3：新增 6 个单元测试

[tests/test_client.py:85](tests/test_client.py#L85)

| 测试 | 场景 |
|------|------|
| `test_extract_text_from_result_normal` | 标准 MCP text content 格式 |
| `test_extract_text_from_result_multiline_text` | 多行文本（如 list_toolsets 目录） |
| `test_extract_text_from_result_iserror` | isError=true 仍能提取文本 |
| `test_extract_text_from_result_plain_string` | 非 JSON 字符串原样返回 |
| `test_extract_text_from_result_empty_content` | 空 content 数组 |
| `test_extract_text_from_result_no_text_content` | content 中无 text 类型（如图片） |

---

## 调用关系

```
cmd_start()                         ← CLI 入口
  └─ preload_all_toolsets()         ← 修复点：预加载所有工具集
       ├─ call_tool("list_toolsets")    ← SSE 流式读取，代替 _rpc()
       │    └─ _read_sse_stream()       ← 逐行消费 SSE，遇 result 立即返回
       │         └─ response.aclose()   ← 主动关闭 TCP 连接
       │
       ├─ _extract_text_from_result()   ← 新增：从 JSON 提取文本
       │
       └─ call_tool("load_toolset")     ← 同上，SSE 流式
            └─ _read_sse_stream()

full_refresh()                      ← L3 刷新，依赖预加载完成
  └─ call_tool("...find_actors")    ← 需 toolset 已注册到 MCP 模块
```

**核心对比：**

```
修复前: _rpc("tools/call") → httpx.post() → response.content (阻塞)
                                                    ↑
                                         UE MultipleWriteStream + keep-alive
                                         → 连接不关闭 → 超时/静默失败

修复后: call_tool(name, args) → httpx.stream() → aiter_lines() (增量)
                                                    ↑
                                         收到 result → 立即 return → aclose()
```
