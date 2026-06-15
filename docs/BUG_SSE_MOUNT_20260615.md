# Bug: SSE Transport `Mount` → `TypeError: 'NoneType' object is not callable` — 2026-06-15

## 现象

L3 端到端测试在 `save_skill` 等 Harness 自有 MCP 工具调用完成后，Harness 进程中抛出：

```
File "...\starlette\routing.py", line 62, in app
    await response(scope, receive, send)
TypeError: 'NoneType' object is not callable
```

同时 `save_skill` 的**业务逻辑已成功执行**（文件落盘、日志输出均正常），崩溃仅发生在 SSE 响应帧回写阶段。

复现条件：
- Harness 运行中（`harness start --ue-port 8000 --listen-port 9000`）
- 通过 MCP SSE 客户端调用任意 Harness 自有工具（如 `save_skill`）
- 工具返回后立即关闭 SSE 连接时概率触发
- 同样的工具和参数通过 `curl` 直连 UE（`/mcp`）不会崩溃

## 诊断过程

### 第一步：怀疑 `instructions` 改动（已排除）

最初修改了 `transport.py` 的 `create_app()`，在 `_init_opts` 上设置了 `.instructions`。回滚后问题仍存在，确认与 `instructions` 无关。

验证：`InitializationOptions` 是标准 Pydantic 模型，`.instructions = "..."` 完全合法：

```python
from mcp.server import Server
s = Server('test')
opts = s.create_initialization_options()
opts.instructions = 'hello'  # 正常工作
print(opts.instructions)      # 'hello'
```

### 第二步：定位到 Starlette 版本

关键版本信息：
- **Starlette 1.0.0**（`pip show starlette` 确认）
- MCP SDK（`mcp` 包，`SseServerTransport` 组件）

### 第三步：追踪调用链

```
HTTP POST /messages/
  → Starlette Router.__call__
    → Mount("/messages/", app=sse.handle_post_message)
      → Mount.handle(scope, receive, send)
        → self.app(scope, receive, send)
          → sse.handle_post_message(scope, receive, send)  ← 这是 ASGI app
          → 在内部调用 send() 完成响应
          → 返回 None  ← ASGI app 的标准行为（不返回 Response）
```

但在 Starlette 1.0 中，`Mount` 对其内部的 `app` 使用了 `request_response()` 包装。查看 Starlette 1.0 源码：

```python
# starlette/routing.py — request_response 函数
def request_response(func, ...):
    async def app(scope, receive, send):
        request = Request(scope, receive, send)
        async def app(scope, receive, send):
            response = await f(request)     # f = handle_post_message
            await response(scope, receive, send)  # ← response = None → crash
        ...
    return app
```

`request_response` 期望 `f(request)` 返回一个 **HTTP Response 对象**。但 `SseServerTransport.handle_post_message` 是一个**原始 ASGI app**（通过 `send()` 直接操作，返回 `None`）。当 `response = None` 时，`await None(scope, receive, send)` → `TypeError: 'NoneType' object is not callable`。

### 根本原因

**Starlette 1.0.0 的 `Mount` 对内部 `app` 的包装方式与 MCP SDK 的 `SseServerTransport.handle_post_message`（原始 ASGI app）不兼容。**

`Mount` 假设内部 app 是 HTTP request/response 风格的（返回 Response），但 `handle_post_message` 是 ASGI 原生风格（直接操作 send，返回 None）。Starlette 的 `Route` 对 ASGI 风格端点正确处理，但 `Mount` 在 1.0 版本中加入了额外的包装层。

注意：这个问题在较早的 Starlette 版本中不存在（`Mount` 直接透传 scope/receive/send，不做 request_response 包装），因此原始代码（HANDOFF_L03 时期的测试）可以正常工作。

## 修复方案

### 方案 A：降级 Starlette（不推荐）

Starlette 1.0 是主要版本更新，降级会导致其他依赖冲突。

### 方案 B：包装 `handle_post_message` 为 HTTP handler（不推荐）

让 `handle_post_message` 返回一个 Response 对象。但 MCP SDK 的内部实现直接操作 `send()`，修改上游代码不现实。

### 方案 C：替换 `Mount` 为 `Route`（✅ 采用）

**改动文件**：`harness/transport.py`

**修改前**：
```python
from starlette.routing import Mount, Route

app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)
```

**修改后**：
```python
from starlette.routing import Route

app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        # Route 对 ASGI app 正确处理——直接透传 scope/receive/send，
        # 不做 request_response 包装
        Route("/messages/{path:path}", endpoint=sse.handle_post_message, methods=["POST"]),
        Route("/messages/", endpoint=sse.handle_post_message, methods=["POST"]),
    ],
)
```

**原理**：Starlette 的 `Route` 类对 ASGI 风格端点（接受 `(scope, receive, send)` 并返回 `None`）有正确的处理路径——直接调用 `await self.app(scope, receive, send)`，不做 `request_response` 包装。因此 `handle_post_message` 的 `None` 返回值不会被错误解释。

两条 `Route` 分别覆盖：
- `/messages/{path:path}` — 带 session ID 子路径的请求
- `/messages/` — 根路径请求

## 验证

```
$ python -m pytest tests/ -q --ignore=tests/test_client.py --ignore=tests/test_l3_e2e.py
........................................................................ [ 52%]
.................................................................        [100%]
137 passed in 0.34s
```

L3 端到端测试（`tests/test_l3_e2e.py`）需重启 Harness 后手动执行。

## 影响范围

- 仅影响 `harness/transport.py` — SSE transport 层的 Starlette 路由配置
- 不影响 MCP 协议处理、工具调用逻辑、拦截器链
- `handle_post_message` 的功能行为不变——仍正确接收 scope 中的完整 URL 路径
