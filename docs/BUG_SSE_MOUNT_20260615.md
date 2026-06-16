# Bug: SSE Transport `Mount` → `TypeError: 'NoneType' object is not callable` — 2026-06-15

## 最终结论：✅ 已修复，L3 测试通过

**根因**：Starlette 1.0 对所有带 `request` 参数的 `Route` 端点进行 `request_response` 包装，期望返回 `Response` 对象。MCP SDK 的 `handle_post_message` 和 Harness 的 `handle_sse` 都是原生 ASGI app（通过 `send()` 直接操作，返回 `None`），被包装后 `None` 被当作 Response 调用 → `TypeError`。

**最终方案**：用纯 ASGI 中间件 `_MessagesMiddleware` 在 Starlette 路由层之前拦截 `/sse` 和 `/messages/*`，直接以原始 ASGI 方式调用 MCP SDK handler。Starlette 的 `Route`/`Mount` 完全不参与这两个端点的处理。

**最终代码**：`harness/transport.py`（142 行），见下方"完整有效路径"节。

**验证**：`pytest tests/` 137 passed；L3 端到端测试 7 个验证点全部通过。

---

## 完整有效路径（transport.py 最终架构）

```
HTTP Request
  │
  ▼
uvicorn (ASGI server)
  │
  ▼
CORSMiddleware (Starlette)
  │  allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
  ▼
_MessagesMiddleware (自建纯 ASGI 中间件)
  │
  ├── POST /messages/* → sse.handle_post_message(child_scope, receive, send)
  │     child_scope 中模拟 Mount 路径剥离: path 去掉 /messages 前缀, root_path += "/messages"
  │
  ├── GET  /sse        → _handle_sse(scope, receive, send)
  │     sse.connect_sse(scope, receive, send) → server.run(streams, init_opts)
  │
  └── 其他请求          → Starlette Router（当前为空，仅作为 ASGI 协议适配层）
```

关键点：
- `_MessagesMiddleware` 是 `starlette.middleware.Middleware` 包装的纯 ASGI 中间件，接收原始 `(scope, receive, send)`
- POST /messages/\* 的 `child_scope` 模拟了 Starlette `Mount` 的路径剥离行为，因为 MCP SDK 的 `handle_post_message` 通过解析 URL path 提取 session_id
- GET /sse 直接以 ASGI 方式运行 `server.run()`，不在 Starlette 路由中注册（避免 `request_response` 包装）
- `instructions` 通过 `server.create_initialization_options()` 正常注入，不受中间件影响

---

## 调试过程（保留供后续排错参考）

### 尝试 1：怀疑 `instructions` 改动

现象：`save_skill` 之后崩溃。

操作：回滚 `_init_opts.instructions` 设置。结果：问题仍存在，排除。

### 尝试 2：`Mount` → `Route` 替换

操作：将 `Mount("/messages/", app=sse.handle_post_message)` 替换为 `Route("/messages/{path:path}", ...)`。

结果：新错误 `500 Internal Server Error`——`handle_post_message` 依赖 `Mount` 的路径剥离来提取 session_id，纯 `Route` 不剥离路径。

### 尝试 3：纯 ASGI 中间件（最终方案）

操作：
1. 创建 `_MessagesMiddleware` 类，实现 `__call__(scope, receive, send)` ASGI 接口
2. POST `/messages/*` 手动模拟路径剥离后调 `handle_post_message`
3. GET `/sse` 直接以 ASGI 运行 `server.run()`
4. 移除所有 Starlette `Route`，路由表设为空 `[]`

结果：✅ L3 测试通过，Harness 端无异常。

### 为什么最终方案生效

Starlette 的 `Route` 和 `Mount` 都在内部使用 `request_response()` 来包装端点函数。`request_response` 的判断逻辑是：
- 如果端点函数接受 `request` 参数 → 使用 HTTP request/response 模式（期望返回 Response）
- 否则 → 使用 ASGI 模式（透传 scope/receive/send）

`handle_sse(request)` 和 `handle_post_message(scope, receive, send)` 的签名导致 Starlette 走了 HTTP 模式——前者有 `request` 参数，后者因 MCP SDK 方法签名而被误判。

绕过 Starlette 路由层后，这些函数直接以原始 ASGI 方式调用，不再被 `request_response` 包装，`None` 返回值不再触发错误。

### 失败的尝试（已回滚）

| 尝试 | 改动 | 结果 |
|:---:|---|---|
| 回滚 instructions | 移除 `_init_opts.instructions` | ❌ 问题仍存在 |
| Mount → Route | `Route("/messages/{path:path}", ...)` | ❌ 500——路径剥离丢失 |
| Route + middleware stubs | 多种组合 | ❌ 均不可用 |
| **纯 ASGI 中间件** | `_MessagesMiddleware` | ✅ **通过** |
