# 工具调用 validation error 日志记录 + Skill 精确调用示例 — 实施计划

> 分析文档: `docs/tmp_issues/0713/analysis.md` — 问题 A

**目标：** (1) 捕获 MCP SDK 层因参数校验失败返回的错误，写入 `tool_errors.jsonl`；(2) 在 match-atmosphere Skill 中提供 `find_actors` 的正确参数示例，减少 LLM 试错。

**技术栈：** Python 3.12+, ASGI middleware, Starlette, MCP SDK

---

## 方法 1 的拦截点分析

### 当前请求链路

```
LLM → HTTP POST /mcp (或 /messages/)
    → _StreamableHttpMiddleware / _MessagesMiddleware  (transport.py)
        → MCP SDK transport.handle_request / handle_post_message
            → SDK 内部: 解析 JSON-RPC → 校验 arguments vs inputSchema
                ├─ 校验失败 → SDK 直接 send() 错误响应 ← 我们的 call_tool 未执行
                └─ 校验成功 → 调用 @server.call_tool() → call_tool(name, arguments)
                    → Harness 工具 / UE 透传
                    → interceptor chain (含 ToolCallLogger)
```

**关键点：** MCP SDK 的参数校验发生在 `StreamableHTTPServerTransport.handle_request()` 或 `SseServerTransport.handle_post_message()` 内部，SDK 直接用 ASGI `send` 返回了错误 JSON-RPC 响应。**我们的 `call_tool(name, arguments)` 根本没有被调用**，所以 ToolCallLogger 看不到这个失败。

### 拦截位置

两个传输的 ASGI 中间件（`transport.py`）：

| 传输 | 中间件类 | 拦截行 | SDK 调用 |
|------|------|:--:|------|
| Streamable HTTP | `_StreamableHttpMiddleware` | [transport.py:100](harness/transport.py#L100) | `self._transport.handle_request(scope, receive, send)` |
| SSE | `_MessagesMiddleware` | [transport.py:60](harness/transport.py#L60) | `self._sse.handle_post_message(child_scope, receive, send)` |

**拦截策略：** 在 `send` 被传入 MCP SDK handler 之前，用 `send_wrapper` 包裹它。wrapper 检查响应 body 中是否包含 JSON-RPC error 对象，有则写入 `tool_errors.jsonl`，然后放行原始 `send`。

---

## 涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `harness/transport.py` | 新增 `_ErrorLoggingSendWrapper` + 在两个中间件中使用 |
| 修改 | `skills/match-atmosphere.yaml` | Step 1 追加 `find_actors` 正确调用示例 |

---

### Task 1: 实现 validation error 拦截器

**文件：**
- 修改: `harness/transport.py`

- [ ] **Step 1: 新增 `_ErrorLoggingSendWrapper` 类**

在 `harness/transport.py` 的 `_StreamableHttpMiddleware` 之前新增：

```python
import json as _json
from pathlib import Path as _Path

# 错误日志路径: 与 tool_calls.jsonl 同目录
_error_log_path: _Path | None = None


def set_error_log_path(path: _Path) -> None:
    """由 cli.py 在启动前设置错误日志路径。"""
    global _error_log_path
    _error_log_path = path


class _ErrorLoggingSendWrapper:
    """ASGI send 包装器：拦截 MCP SDK 层参数校验失败并写入 tool_errors.jsonl。

    **两种工具调用错误的本质区别：**

    1. SDK 层校验失败（本包装器拦截目标）：
       Harness MCP SDK 根据 list_tools 时注册的 inputSchema 校验 arguments。
       校验失败时 SDK 在 call_tool handler 之前直接返回 error。
       → call_tool() 从未被调用，interceptor 链被跳过。
       → 唯一捕获点: 此 ASGI wrapper。

    2. UE 返回的 error（已被 ToolCallLogger 正常记录）：
       请求通过 SDK 校验，正常进入 call_tool() → ue_client.call_tool() →
       UE 执行并返回 error → interceptor 链 → ToolCallLogger 写入 JSONL。
       → 已有一条完整的日志链路，无需额外拦截。

    此包装器专门解决第一种情况——请求在到达应用代码前就被 SDK 终止的情况。
    """

    def __init__(self, send):
        self._send = send
        self._body_chunks: list[bytes] = []

    async def __call__(self, message):
        if message["type"] == "http.response.body":
            self._body_chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
            if not more:
                full_body = b"".join(self._body_chunks)
                self._try_log_error(full_body)
        await self._send(message)

    def _try_log_error(self, body: bytes) -> None:
        """检查响应体是否为 JSON-RPC error，是则写入日志。"""
        global _error_log_path
        if _error_log_path is None:
            return
        try:
            data = _json.loads(body)
        except Exception:
            return
        # JSON-RPC error 响应: {"jsonrpc":"2.0","id":...,"error":{...}}
        if not isinstance(data, dict):
            return
        error = data.get("error")
        if error is None:
            return
        error_code = error.get("code", 0)
        error_msg = error.get("message", "")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "error_code": error_code,
            "message": error_msg,
            "request_id": data.get("id"),
        }
        try:
            _error_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_error_log_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日志写入失败不应影响主链路
```

- [ ] **Step 2: 在 `_StreamableHttpMiddleware` 中使用 wrapper**

将 `transport.py:100` 处的：
```python
            await self._transport.handle_request(scope, receive, send)
```
改为：
```python
            await self._transport.handle_request(
                scope, receive, _ErrorLoggingSendWrapper(send),
            )
```

- [ ] **Step 3: 在 `_MessagesMiddleware` 的 POST /messages 路径中使用 wrapper**

将 `transport.py:60` 处的：
```python
            await self._sse.handle_post_message(child_scope, receive, send)
```
改为：
```python
            await self._sse.handle_post_message(
                child_scope, receive, _ErrorLoggingSendWrapper(send),
            )
```

- [ ] **Step 4: 在 `serve()` 函数中初始化错误日志路径**

在 `transport.py` 的 `serve()` 中，通过参数或通过 `set_error_log_path()` 设置路径。或者在 `cli.py` 的启动流程中设置。最简单的方案是在 `serve()` 接受一个可选的 `error_log_dir` 参数：

```python
async def serve(
    server: Server,
    host: str = "127.0.0.1",
    port: int = 9000,
    enable_sse: bool = True,
    enable_streamable_http: bool = True,
    error_log_path: str | None = None,  # 新增
) -> None:
    if error_log_path:
        set_error_log_path(_Path(error_log_path))
```

- [ ] **Step 5: 在 `cli.py` 启动时传入路径**

在 `cli.py` 中 `serve()` 调用处传入 error_log_path（与 tool_calls.jsonl 同目录，文件名为 `tool_errors.jsonl`）。

- [ ] **Step 6: 验证语法并运行测试**

```bash
uv run python -c "import ast; ast.parse(open('harness/transport.py', encoding='utf-8').read()); print('OK')"
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

---

### Task 2: 在 match-atmosphere Skill 中追加 find_actors 示例

**文件：**
- 修改: `skills/match-atmosphere.yaml`

- [ ] **Step 1: 在 Step 1 末尾追加 find_actors 使用说明**

在 `### Step 1 — 生成参数映射` 段落后追加：

```yaml
  ### Step 1 — 生成参数映射
  调 build_atmosphere_mapping()，Harness 自动扫描 5 类氛围组件的可用属性，
  通过 MiMo 筛选氛围相关属性并按 9 维度（亮度/对比度/色温/色调偏移/
  饱和度/大气密度/阴影方向/天空/视角方向）分类。映射以 Markdown 表格形式直接返回。

  **映射表返回的 refPath 已可直接用于 get_properties / set_properties。**
  无需再手动调 find_actors 查找组件。如需手动查找（如扫描遗漏的组件）：
    find_actors({glob: "*Light*", tag: ""}) → 名称包含 Light 的 Actor
    find_actors({glob: "*Fog*", tag: ""})   → 名称包含 Fog 的 Actor
    注意: tag 参数为必填项，传空字符串 "" 即可匹配所有 Tag。
          glob 参数做名称模糊匹配（非类名匹配）。
```

---

## 自审清单

1. **需求覆盖：**
   - [x] validation error 拦截（Task 1, transport.py ASGI 层）
   - [x] Skill find_actors 示例（Task 2）

2. **影响分析：**
   - `_ErrorLoggingSendWrapper` 不做任何过滤/修改，只做读取+日志，对主链路零影响
   - 日志写入失败被静默吞掉，不阻断请求
   - Skill 追加内容不改变现有流程，仅增加参考信息
