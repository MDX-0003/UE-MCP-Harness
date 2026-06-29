# Python 基础知识点

> 记录 UE-MCP-Harness 项目中涉及的 Python 基础语法问题与解答。
> 以 Q&A 形式组织，延申追问会作为小节追加到对应问题下。

---

## Q1: `sys.exit`、`asyncio.run`、`async def` 是什么？

**来源文件:** `tests/test_l3_e2e.py:28,233`

### `async def` — 定义协程函数

```python
async def main() -> int:  # test_l3_e2e.py:28
```

普通函数用 `def` 定义，而 `async def` 定义的是一个**协程函数**（coroutine function）。调用它不会立刻执行函数体，而是返回一个**协程对象**（coroutine object），必须交给事件循环去调度才能运行。

协程函数内部可以使用 `await` 关键字：

```python
init_result = await session.initialize()   # 等待 MCP 握手完成
tools_result = await session.list_tools()  # 等待工具列表返回
result = await session.call_tool(...)      # 等待远程工具调用结果
```

`await` 的含义是："这里需要等 I/O（网络/文件），但我先让出 CPU，等结果到了再回来继续"。核心价值是**等待 I/O 时不阻塞线程**，适合高并发网络通信场景。

**为什么这个测试文件要用异步？** 因为它通过 SSE（Server-Sent Events）协议与 UE Harness 通信（`http://127.0.0.1:9000/sse`），所有 MCP 调用都是网络请求，异步模型是最自然的选择。

### `asyncio.run()` — 启动事件循环

```python
asyncio.run(main())  # test_l3_e2e.py:233
```

Python 的协程不能自己运行，必须有**事件循环**（event loop）来调度。`asyncio.run()` 做了三件事：

1. 创建一个新的事件循环
2. 把协程丢进去跑
3. 等协程跑完，清理事件循环，返回协程的返回值

它是 Python 3.7+ 推荐的标准入口，一个程序通常只在最外层调用一次。

**类比：** `async def` 是剧本，事件循环是导演，`asyncio.run()` 是 "Action!"——喊开始并把整场戏排完。

### `sys.exit()` — 退出程序并返回退出码

```python
sys.exit(asyncio.run(main()))  # test_l3_e2e.py:233
```

`sys.exit(n)` 立即终止 Python 进程，并向操作系统返回一个**退出码**（exit code）：

- `sys.exit(0)` → 退出码 `0`，约定表示**成功**
- `sys.exit(N)` (N > 0) → 退出码非零，约定表示**失败**

这个测试文件里，`main()` 返回错误数量（上限 255），然后传给 `sys.exit()`：

```python
# test_l3_e2e.py:221
return min(errors, 255)  # 退出码最大 255，超出会 mod 256
```

**退出码的用途：** CI/CD 流水线或外部脚本通过读取退出码判断测试是否通过。`0` → 绿勾 ✅，非零 → 红叉 ❌。

### 三者关系（一条流水线）

```
┌────────────────────────────────────────────────────────┐
│  sys.exit( asyncio.run( main() ) )                     │
│     ▲              ▲           ▲                       │
│     │              │           └─ async def 协程函数     │
│     │              └─ 启动事件循环，跑完返回 int         │
│     └─ 用返回值作为进程退出码，0=成功, 非0=失败          │
└────────────────────────────────────────────────────────┘
```

**一句话总结这个文件：** 用异步方式跑完所有 MCP 端到端测试，把错误数量作为退出码告诉操作系统。

---

## Q2: `async with`、`sse_client`、`ClientSession` 是什么？

**来源文件:** `tests/test_l3_e2e.py:33-34`

```python
async with sse_client(HARNESS_URL, timeout=30) as (read, write):
    async with ClientSession(read, write) as session:
```

### `async with` — 异步上下文管理器

`async with` 是 `with` 的异步版本。

| | 同步 | 异步 |
|---|---|---|
| 语法 | `with obj as x:` | `async with obj as x:` |
| 进入方法 | `obj.__enter__()` | `await obj.__aenter__()` |
| 退出方法 | `obj.__exit__()` | `await obj.__aexit__()` |

关键区别：异步版本的进入和退出**都可以 `await`**——也就是说，建立连接和关闭连接本身可能涉及网络 I/O，不能阻塞线程。

`with` / `async with` 的共同价值：**自动清理资源**。无论代码块正常结束还是抛异常，退出方法一定会被调用（相当于 `try...finally` 的语法糖）。

### `sse_client` — SSE 传输层

```python
from mcp.client.sse import sse_client

async with sse_client(HARNESS_URL, timeout=30) as (read, write):
```

`sse_client` 来自 `mcp` 库，是一个**异步上下文管理器工厂**。它做的事：

1. **进入时 (`__aenter__`)：** 向 `http://127.0.0.1:9000/sse` 发起 HTTP 长连接（SSE = Server-Sent Events），返回一对流对象：
   - `read` — 异步可读流（MemoryObjectReceiveStream），从 Harness 接收消息
   - `write` — 异步可写流（MemoryObjectSendStream），向 Harness 发送消息

2. **退出时 (`__aexit__`)：** 关闭 HTTP 连接，释放网络资源。即使代码抛异常也会执行。

SSE 的本质是一个**单向长连接**：服务端可以持续推送事件给客户端，客户端通过普通 HTTP 请求发送数据。适合 MCP 这种"客户端发请求、服务端推送响应+通知"的模式。

### `ClientSession` — MCP 协议层

```python
from mcp import ClientSession

async with ClientSession(read, write) as session:
```

`ClientSession` 接收底层流对象，在其上封装 **MCP 协议** 的高层 API：

| 底层（sse_client） | 高层（ClientSession） |
|---|---|
| `read` / `write` 原始字节流 | `session.initialize()` — MCP 握手 |
| 你需要自己拼 JSON-RPC | `session.list_tools()` — 获取工具列表 |
| 你需要自己解析响应 | `session.call_tool(name, args)` — 调用工具 |

它也是一个异步上下文管理器：进入时启动协议协商，退出时优雅关闭 MCP 会话。

### 为什么嵌套两层？

```
┌─ sse_client ──────────────────────────────┐
│  HTTP 长连接 (SSE)                         │
│  提供: read 流, write 流                    │
│                                            │
│  ┌─ ClientSession ────────────────────┐    │
│  │  MCP 协议层                         │    │
│  │  .initialize() / .list_tools()     │    │
│  │  .call_tool()                      │    │
│  │                                    │    │
│  │  (测试代码在这里跟 UE 交互)          │    │
│  │                                    │    │
│  └────────────────────────────────────┘    │
│                                            │
│  退出时: 关闭 MCP 会话 → 断开 HTTP 连接      │
└────────────────────────────────────────────┘
```

这是经典的**分层设计**：

1. **传输层**（sse_client）：只管"怎么传"——HTTP/SSE 连接管理
2. **协议层**（ClientSession）：只管"传什么"——JSON-RPC 消息格式、MCP 握手、工具调用

两层各自独立，可以替换。比如将来换成 WebSocket 传输，只需换掉 `sse_client`，`ClientSession` 不感知。

### 嵌套 `async with` 的清理顺序

```python
async with sse_client(...) as (read, write):  # ① 先进入
    async with ClientSession(read, write) as session:  # ② 后进入
        ...  # ③ 执行测试
    # ④ ClientSession.__aexit__() 先退出（关闭 MCP 会话）
# ⑤ sse_client.__aexit__() 后退出（断开 HTTP 连接）
```

后进先出（LIFO）——确保关闭会话时还能用底层连接发送"再见"消息，然后再断网。

---

## Q3: `mcp` 库怎么理解？`session.list_tools()` 背后发生了什么？

**来源文件:** `tests/test_l3_e2e.py:39,54`  
**关联:** [[#Q2 `async with` `sse_client` `ClientSession` 是什么]]

### 你对 main 框架的理解是否正确？

你描述的流程：

> `sse_client` 通过 `HARNESS_URL` 建立了连接（即 read+write 流），新建一个 `ClientSession` 实例去持有这两个流，通过 `HARNESS_URL` 这个本地端口来发送+接收信息。

**完全正确。** 更精确的梳理：

```
main() 启动
  │
  ├─ sse_client("http://127.0.0.1:9000/sse")  ──→ 建立 HTTP 长连接，拿到 read/write 流
  │
  ├─ ClientSession(read, write)               ──→ 把流交给协议层，拿到 session 对象
  │
  ├─ session.initialize()                     ──→ MCP 握手（交换协议版本、服务端信息）
  ├─ session.list_tools()                     ──→ 问 Harness "你有多少工具？"
  ├─ session.call_tool("GetSelectedActors")   ──→ 远程执行 UE 工具
  ├─ session.call_tool("get_context")         ──→ 获取系统上下文
  ├─ session.call_tool("activate_skill")      ──→ 激活 Skill
  └─ ...
```

### 为什么 `session` 支持 `list_tools` 这类方法？

`session` 是 `ClientSession` 类的实例。这个类实现了 **MCP 客户端协议** —— MCP 规范定义了约 10 种标准操作，`ClientSession` 把每一种都实现成了方法：

| MCP 协议操作 | ClientSession 方法 | 用途 |
|---|---|---|
| `initialize` | `await session.initialize()` | 握手，交换协议版本 |
| `tools/list` | `await session.list_tools()` | 获取服务端工具清单 |
| `tools/call` | `await session.call_tool(name, args)` | 调用某个具体工具 |
| `resources/list` | `await session.list_resources()` | 获取资源列表 |
| `prompts/list` | `await session.list_prompts()` | 获取提示模板列表 |

它的本质：**一个封装了 JSON-RPC 通信的类**。你调用方法 → 它帮你拼 JSON、发请求、等响应、解析返回 —— 你不用管底层的字节流。

### `list_tools()` 的作用是什么？

**作用：询问 MCP 服务端"你能提供哪些工具？"，拿回一个工具清单。**

从 mcp 库源码看，它内部做了这件事（`mcp/client/session.py:507-536`）：

```python
async def list_tools(self, cursor=None, *, params=None) -> types.ListToolsResult:
    # 1. 构造一个 "tools/list" 的 JSON-RPC 请求
    result = await self.send_request(
        types.ClientRequest(
            types.ListToolsRequest(params=request_params)
        ),
        types.ListToolsResult,
    )
    # 2. 返回的结果里包含 tool 列表
    return result
```

`send_request` 背后做的事：

```
session.list_tools()
  └─ send_request(ListToolsRequest)
       ├─ 把请求对象序列化成 JSON-RPC:
       │    {"jsonrpc":"2.0", "method":"tools/list", "id":1, "params":{}}
       ├─ 通过 write 流发送给 Harness (→ localhost:9000)
       ├─ 等待 read 流收到响应:
       │    {"jsonrpc":"2.0", "id":1, "result": {"tools": [...]}}
       ├─ 反序列化成 ListToolsResult 对象
       └─ 返回给调用方
```

### 在这个测试里怎么用它？

```python
# test_l3_e2e.py:54-56
tools_result = await session.list_tools()
tool_names = [t.name for t in tools_result.tools]
print(f"   工具总数: {len(tool_names)}")
```

`tools_result.tools` 是一个列表，每个元素是一个 `Tool` 对象，包含：
- `.name` — 工具名，如 `"ToolsetRegistry.EditorAppToolset.GetSelectedActors"`
- `.description` — 工具用途说明
- `.inputSchema` — 参数格式（JSON Schema）

然后这段测试代码用它来**验证 004 过滤器**是否生效：
- 如果 UE 原本有 211 个工具，但 Harness 的过滤器应该只暴露 ~20 个
- 所以 `len(tool_names) > 100` 就报错 —— 说明过滤器没起作用

### 一句话总结

`mcp` 库 = **JSON-RPC over streams 的封装**。`ClientSession` 把"拼 JSON → 发流 → 收流 → 解 JSON"全包了，暴露成 `initialize()`、`list_tools()`、`call_tool()` 这种一看就懂的方法，让你像调本地函数一样跟远程 UE 服务端对话。

---

## Q4: Harness 整体架构 — 它是怎么处理 MCP 请求的？

**来源文件:** `harness/cli.py`, `harness/transport.py`, `harness/server.py`, `harness/client.py`, `harness/interceptor.py`

### 先决知识：什么是 MCP 协议？

在深入 Harness 之前，理解 MCP 协议本身只需要三句话：

1. **MCP = Model Context Protocol**，是 Anthropic 定义的一套标准，让 LLM 和服务端之间通过结构化 JSON 消息交互
2. **传输层是 HTTP + SSE**：客户端通过 SSE 长连接接收推送，通过 HTTP POST 发送请求
3. **消息格式是 JSON-RPC 2.0**：所有请求/响应都是这种格式

```
请求:  {"jsonrpc":"2.0", "id":1, "method":"tools/list", "params":{}}
响应:  {"jsonrpc":"2.0", "id":1, "result": {"tools": [...]}}
```

> **不需要深入理解 HTTP/SSE/JSON-RPC 的细节。** 你只需要知道：客户端发一个带 `method` 字段的 JSON 过去，服务端返回一个带 `result` 的 JSON 回来。Harness 就是在中间处理这些 JSON 消息的程序。

### 全局视角：三条链路，两层代理

把整个系统画出来：

```
┌──────────┐  SSE (JSON-RPC)   ┌──────────────────┐  HTTP POST (JSON-RPC)  ┌─────────────────┐
│ 测试脚本   │ ◄──────────────► │  Harness (:9000)  │ ◄───────────────────► │  UE MCP (:8000)  │
│ (LLM/CLI) │                  │  中间层代理        │                       │  UE Editor 内部   │
└──────────┘                  └──────────────────┘                       └─────────────────┘
     │                              │                                          │
     │  session.list_tools()        │  ① 收到 "tools/list"                      │  UE 内部注册了
     │  session.call_tool(...)      │  ② 先问 UE 拿原始工具列表                   │  211 个 MCP 工具
     │                              │  ③ 过滤 → 追加自有工具 → 返回               │
     │                              │                                          │
     │                              │  session.call_tool("GetSelectedActors")    │
     │                              │  ① 收到 "tools/call"                      │
     │                              │  ② 判断：是 Harness 自有还是 UE 工具？        │
     │                              │  ③ 自有 → 本地处理                         │
     │                              │  ④ UE   → 走拦截器链 → 转发给 UE            │
```

**Harness 不是简单的"消息转发器"。** 它是一个有自己逻辑的中间层——拦截、过滤、缓存、注入自有工具。

### 启动流程（`harness start` 命令背后）

来自 `harness/cli.py:cmd_start()`，一共 6 步：

```
① 加载配置 (Config.from_env())         → 确定端口、日志目录等
② 连接 UE (ue_client.connect())       → Harness 作为"客户端"连到 UE :8000
③ 预加载工具集 (preload_all_toolsets)   → UE 延迟加载模式下，主动加载所有工具集
④ 刷新 State Cache (full_refresh)     → 首次拉取 WorldState（地图、Actor 等）
⑤ 构建 MCP Server (build_server)      → 注册 tools/list + tools/call 处理器
⑥ 启动 HTTP Server (transport.serve)  → 在 :9000 监听，等待 LLM/测试脚本连接
```

### 三层代码的职责划分

| 文件 | 角色 | 一句话 |
|------|------|--------|
| `harness/transport.py` | **门卫** | 管理 HTTP 连接——GET /sse 建立长连接，POST /messages/ 接收 JSON-RPC 消息 |
| `harness/server.py` | **大脑** | 注册 MCP 方法的处理函数——`list_tools()` 过滤工具列表，`call_tool()` 路由调用 |
| `harness/client.py` | **手脚** | Harness 自己作为客户端去调 UE——用 httpx 发 HTTP POST 到 UE :8000 |

### 以 `list_tools` 为例，跟踪一次完整请求

```
测试脚本调用: await session.list_tools()
     │
     │  JSON-RPC: {"method":"tools/list", ...}
     ▼
┌─ transport.py ────────────────────────────────────────────────────┐
│  POST /messages/ 收到请求                                          │
│  → MCP SDK 解析 JSON-RPC，识别 method = "tools/list"               │
│  → 调用 server.py 中注册的 @server.list_tools() 处理函数            │
└───────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ server.py: list_tools() ─────────────────────────────────────────┐
│  ① _rebuild_tool_reference()                                      │
│     → 调用 ue_client.list_tools() 向 UE :8000 要原始工具列表        │
│     → apply_filter(原始列表, allowlist) 过滤到 ~20 个               │
│  ② 把过滤后的工具包装成 Tool 对象                                    │
│  ③ 追加 4 个 Harness 自有工具:                                      │
│     - activate_skill                                               │
│     - save_skill                                                   │
│     - get_context                                                  │
│     - deactivate_skill                                             │
│  ④ return [Tool, Tool, ...]  ← 约 24 个                            │
└───────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ transport.py ────────────────────────────────────────────────────┐
│  MCP SDK 把返回值序列化为 JSON-RPC 响应                             │
│  → {"result": {"tools": [...]}} 通过 SSE 发回测试脚本               │
└───────────────────────────────────────────────────────────────────┘
     │
     ▼
测试脚本收到: tools_result.tools  →  [Tool(name="..."), ...]
```

### 以 `call_tool` 为例，分两条路径

**路径 A：Harness 自有工具（如 `activate_skill`）**

```
call_tool("activate_skill", {"name_or_desc": "黄昏"})
  → server.py call_tool() 一看 name == "activate_skill"
  → 本地处理：SkillRegistry.match_skill("黄昏") → 设置 _active_skill
  → 直接返回结果（不经过 UE）
```

**路径 B：UE 工具透传（如 `GetSelectedActors`）**

```
call_tool("ToolsetRegistry.EditorAppToolset.GetSelectedActors", {})
  → server.py call_tool() 一看 name 不属于 Harness 自有
  → 走拦截器链:
       pre_call  (DebugPreCallInterceptor)  ← 可在此修改 args
       pre_call  (ToolCallLogger)           ← 记录开始时间
       pre_call  (StateCacheInterceptor)    ← 检查缓存
  → ue_client.call_tool(name, args)         ← 实际发 HTTP POST 到 UE :8000
       UE 返回 SSE: {"result": {"content": [...]}}
  → 解析结果 → 构建 ToolCallCompleted 事件
  → post_call (StateCacheInterceptor)       ← 根据结果更新缓存
  → post_call (ToolCallLogger)              ← 写 JSONL 日志
  → post_call (DebugPreCallInterceptor)     ← 无操作
  → 返回结果给测试脚本
```

### 拦截器链（Interceptor Chain）是什么？

来自 `harness/interceptor.py`。这是一个**责任链模式**——每个拦截器可以：

- `pre_call(name, args) → args`：调用 UE 前修改参数（或抛异常拒绝调用）
- `post_call(event)`：调用 UE 后做副作用（日志、缓存更新等），不修改结果

```
         pre                    post
     ┌────────┐            ┌────────┐
     │ Debug  │            │ Debug  │  (空操作，占位)
     └───┬────┘            └───▲────┘
         ▼                     │
     ┌────────┐            ┌───┴────┐
     │ Logger │            │ Logger │  (写 JSONL 日志)
     └───┬────┘            └───▲────┘
         ▼                     │
     ┌────────┐            ┌───┴────┐
     │ Cache  │            │ Cache  │  (更新 WorldState 缓存)
     └───┬────┘            └───▲────┘
         ▼                     │
    ┌──────────┐               │
    │ UE 调用   │──────────────┘
    └──────────┘
```

与你的理解对照：**你说的完全正确** —— `session.list_tools()` 本质就是构造 JSON-RPC 请求发给 9000 端口，Harness 处理后回复。只不过 Harness 不只是"回复"，它在中间做了过滤、追加、日志、缓存等增值操作。

### 关键文件速查

| 想了解什么 | 看哪个文件 |
|---|---|
| Harness 怎么启动的 | `harness/cli.py:cmd_start()` |
| HTTP 请求怎么进来的 | `harness/transport.py:_MessagesMiddleware` |
| tools/list 怎么处理的 | `harness/server.py:list_tools()` (第 129 行) |
| tools/call 怎么处理的 | `harness/server.py:call_tool()` (第 189 行) |
| Harness 怎么调 UE 的 | `harness/client.py:McpClientSession.call_tool()` (第 187 行) |
| 拦截器怎么工作的 | `harness/interceptor.py` |
| 测试脚本怎么连 Harness | `tests/test_l3_e2e.py:33-34` |

---

## Q6: transport.py 逐行追踪 — `list_tools` 从 HTTP 请求到响应

**来源文件:** `harness/transport.py`（全部 142 行）  
**关联:** [[#Q4 Harness 整体架构]]

### transport.py 的三个函数

| 函数 | 行号 | 作用 | 谁调用 |
|---|---|---|---|
| `create_app()` | `transport.py:79` | 创建 Starlette ASGI 应用，把 `mcp.server.Server` + `SseServerTransport` 挂到 HTTP 路由上 | `serve()` 调用它 |
| `serve()` | `transport.py:108` | 用 uvicorn 在 9000 端口启动 HTTP 服务 | `cli.py:129` 调用它 |
| `_MessagesMiddleware.__call__()` | `transport.py:43` | **核心路由**：每个 HTTP 请求都经过它，按路径和 method 分发 | Starlette 中间件链自动调用 |

### `create_app()` 做了什么 — `transport.py:79-105`

```python
def create_app(server: Server, instructions: str = "") -> Starlette:
    sse = SseServerTransport("/messages/")           # ① 创建 SSE 传输管理器
    _init_opts = server.create_initialization_options()  # ② 初始化选项
    if instructions:
        _init_opts.instructions = instructions

    app = Starlette(
        routes=[],                                   # ③ 路由表为空！
        middleware=[
            Middleware(CORSMiddleware, ...),
            Middleware(_MessagesMiddleware,           # ④ 所有路由走这个中间件
                       sse=sse, server=server, init_opts=_init_opts),
        ],
    )
    return app
```

**关键设计决策：** Starlette 的 `routes=[]` 是空的。所有 HTTP 请求由 `_MessagesMiddleware` 拦截处理，不经过常规路由。原因是 Starlette 1.0 的 `Mount` 对 MCP SDK 的原生 ASGI handler 有兼容问题（见 `transport.py:28-35` 的注释）。

### `serve()` 做了什么 — `transport.py:108-141`

```python
async def serve(server, host="127.0.0.1", port=9000, instructions=""):
    app = create_app(server, instructions)    # ① 构建 Starlette 应用
    config = uvicorn.Config(app, host=host, port=port, ...)  # ② 配置 uvicorn
    uvicorn_server = uvicorn.Server(config)   # ③ 创建 uvicorn 实例
    await uvicorn_server.serve()              # ④ 启动 HTTP 监听（阻塞）
```

调用链：`cli.py:129` → `serve(server, ...)` → uvicorn 开始在 `:9000` 监听。

### 完整追踪：`list_tools` 一次请求的 10 步

下面是带**精确代码位置**的完整链路：

uvicorn = 网卡驱动，监听端口、解析 HTTP

Starlette = 操作系统，提供路由/中间件框架

_MessagesMiddleware = 手写的路由器，GET /sse → 建连接，POST /messages/ → 投递消息
```
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ①  客户端发起请求                                                     │
│ test_l3_e2e.py:54   await session.list_tools()                           │
│                                                                          │
│ mcp/client/session.py:529   result = await self.send_request(              │
│     types.ClientRequest(                                                  │
│         types.ListToolsRequest(params=None)   ← 构造请求对象               │
│     ),                                                                    │
│     types.ListToolsResult,                                                │
│ )                                                                         │
│                                                                          │
│ ClientSession 内部:                                                       │
│   → 序列化为 JSON: {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}│
│   → 通过 HTTP POST 发送到                                                 │
│     http://127.0.0.1:9000/messages/?session_id=<uuid>                    │
└─────────────────────────────────────────────────────────────────────────┘
     │
     │  HTTP POST /messages/?session_id=xxx
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ②  uvicorn 收到 HTTP 请求                                             │
│ uvicorn 把原始 HTTP 包装成 ASGI (scope, receive, send) 三元组，            │
│ 交给 Starlette 应用处理。                                                  │
└─────────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ③  _MessagesMiddleware.__call__() 路由                               │
│ transport.py:43-67                                                       │
│                                                                          │
│ async def __call__(self, scope, receive, send):                          │
│     path = scope["path"]              # "/messages/"                     │
│     method = scope["method"]          # "POST"                          │
│                                                                          │
│     # 匹配 POST /messages/*                                               │
│     if scope["method"] == "POST" and path.startswith("/messages/"):      │
│         # transport.py:51-58                                             │
│         child_scope = dict(scope)                                        │
│         child_scope["path"] = "/"  或 "/session_id"                      │
│         child_scope["root_path"] = ... + "/messages"                     │
│         await self._sse.handle_post_message(child_scope, receive, send)  │
│         return                      ← 命中，不再继续                      │
│                                                                          │
│     # 匹配 GET /sse                                                       │
│     if scope["method"] == "GET" and path == "/sse":                      │
│         await self._handle_sse(scope, receive, send)                     │
│         return                                                           │
│                                                                          │
│     # 其他路径 → Starlette 默认处理                                        │
│     await self.app(scope, receive, send)                                 │
└─────────────────────────────────────────────────────────────────────────┘
     │
     │  命中 POST /messages/ → handle_post_message()
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ④  SseServerTransport.handle_post_message() 解析 JSON                 │
│ mcp/server/sse.py:201-249                                                │
│                                                                          │
│ async def handle_post_message(self, scope, receive, send):               │
│     request = Request(scope, receive)                                    │
│                                                                          │
│     # 从 ?session_id=xxx 提取会话 ID                                      │
│     session_id_param = request.query_params.get("session_id")   #:211    │
│     session_id = UUID(hex=session_id_param)                     #:217    │
│                                                                          │
│     # 根据 session_id 找到对应的内存流写入端                                │
│     writer = self._read_stream_writers.get(session_id)          #:224    │
│                                                                          │
│     # 读取 HTTP body → 解析为 JSON-RPC 消息对象                            │
│     body = await request.body()                                 #:230    │
│     message = types.JSONRPCMessage.model_validate_json(body)    #:234    │
│     # message = JSONRPCMessage(root=ClientRequest(root=ListToolsRequest)) │
│                                                                          │
│     # 把消息写入该 session 的内存流（回到 server.run 的循环）                │
│     session_message = SessionMessage(message, metadata=...)     #:245    │
│     await response(scope, receive, send)  ← 返回 202 Accepted   #:247-248│
│     await writer.send(session_message)                           #:249   │
└─────────────────────────────────────────────────────────────────────────┘
     │
     │  消息通过内存流 (anyio memory object stream) 传递
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑤  server.run() 的消息循环                                            │
│ mcp/server/lowlevel/server.py:640-679                                    │
│                                                                          │
│ async def run(self, read_stream, write_stream, init_opts):               │
│     session = ServerSession(read_stream, write_stream, init_opts) #:658  │
│                                                                          │
│     async for message in session.incoming_messages:             #:675    │
│         # message = RequestResponder(                                     │
│         #   request=ClientRequest(root=ListToolsRequest(...)),            │
│         #   ...                                                          │
│         # )                                                              │
│         tg.start_soon(                                          #:678    │
│             self._handle_message, message, session, ...         #:679    │
│         )                                                                 │
└─────────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑥  _handle_message() 模式匹配                                        │
│ mcp/server/lowlevel/server.py:692-714                                    │
│                                                                          │
│ async def _handle_message(self, message, session, ...):                  │
│     match message:                                                       │
│         case RequestResponder(request=ClientRequest(root=req)): #:701   │
│             # req = ListToolsRequest(params=None)                        │
│             await self._handle_request(message, req, session, ...)#:703  │
└─────────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑦  _handle_request() 查找注册的 handler                               │
│ mcp/server/lowlevel/server.py:719-770                                    │
│                                                                          │
│ async def _handle_request(self, message, req, session, ...):             │
│     # req 的类型是 ListToolsRequest                                       │
│     # 从 request_handlers 字典中查找 type(req) 对应的 handler              │
│     if handler := self.request_handlers.get(type(req)):         #:729    │
│         # handler 就是 Harness 在 server.py 中注册的那个函数               │
│         response = await handler(req)                           #:770    │
│         # response = ServerResult(ListToolsResult(tools=[...]))          │
│         # 通过 message.respond() 发回去                                   │
└─────────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑧  Harness 注册的 list_tools handler 执行                             │
│ server.py:129-184                                                        │
│                                                                          │
│ @server.list_tools()                                                     │
│ async def list_tools() -> list[Tool]:                                    │
│     filtered = await _rebuild_tool_reference()  # 调 UE 拿 211 个 → 过滤  │
│     result = [Tool(name=..., description=..., ...) for t in filtered]    │
│     result.append(Tool(name="activate_skill", ...))  # 追加自有工具       │
│     result.append(Tool(name="save_skill", ...))                          │
│     result.append(Tool(name="get_context", ...))                         │
│     result.append(Tool(name="deactivate_skill", ...))                    │
│     return result   # ~24 个 Tool 对象                                    │
└─────────────────────────────────────────────────────────────────────────┘
     │
     │  返回值交给 MCP SDK 的 handler wrapper (server.py:443-453 in mcp SDK)
     │  包装成 ServerResult → message.respond() → 写入 write_stream
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑨  响应通过 SSE 流返回                                                │
│ mcp/server/sse.py:163-178  (sse_writer 协程)                              │
│                                                                          │
│ async def sse_writer():                                                  │
│     async for session_message in write_stream_reader:           #:171    │
│         await sse_stream_writer.send({                          #:173    │
│             "event": "message",                                          │
│             "data": session_message.message.model_dump_json(),           │
│         })                                                               │
│     # EventSourceResponse 把 dict 序列化为 SSE 格式:                      │
│     #   event: message                                                   │
│     #   data: {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}          │
│     # 通过 HTTP 响应流推送给客户端                                         │
└─────────────────────────────────────────────────────────────────────────┘
     │
     │  SSE event-stream 到达客户端
     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 ⑩  客户端收到响应                                                      │
│ mcp/client/sse.py 的 SSE 读取循环解析 event-stream                         │
│   → 提取 "data" 字段 → JSON.parse → JSONRPCResponse                      │
│   → send_request() 返回 ListToolsResult                                  │
│                                                                          │
│ test_l3_e2e.py:54-56                                                     │
│ tools_result = await session.list_tools()                                │
│ tool_names = [t.name for t in tools_result.tools]                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 关键数据结构：内存流（Memory Object Stream）

上面步骤 ④→⑤ 之间有一个容易被忽略的关键设计：

```
handle_post_message()                    server.run()
     │                                       │
     │  writer.send(message)                 │  async for message in
     │       │                               │    session.incoming_messages:
     │       ▼                               │       ▲
     │  ┌─────────────┐                      │       │
     │  │ 内存流 (anyio │══════════════════════╪═══════╝
     │  │ memory obj   │   进程内管道           │
     │  │ stream)      │                      │
     │  └─────────────┘                      │
```

`SseServerTransport.connect_sse()` 在建立 SSE 连接时（`transport.py:71` 调用），创建了一对 `anyio.create_memory_object_stream()`（`sse.py:141-142`）：

- `read_stream_writer` — 写入端 → `handle_post_message` 用它把收到的 JSON-RPC 消息**塞进去**
- `read_stream` — 读取端 → `server.run()` 通过 `session.incoming_messages` 从它**读出来**

**这是进程内的零拷贝管道**——不是网络通信，只是 Python 对象在两个协程之间传递。

### SSE 连接建立（GET /sse）

上面追踪的是 POST 请求。那 SSE 长连接是怎么建立的？在 `transport.py:69-76`：

```python
# transport.py:63-65 — 匹配 GET /sse
if scope["method"] == "GET" and path == "/sse":
    await self._handle_sse(scope, receive, send)  # transport.py:64

# transport.py:69-76
async def _handle_sse(self, scope, receive, send):
    async with self._sse.connect_sse(scope, receive, send) as streams:
        # streams = (read_stream, write_stream)  ← 来自内存流
        await self._server.run(
            streams[0],      # read_stream → 读客户端发来的 JSON-RPC
            streams[1],      # write_stream → 写响应推给客户端
            self._init_opts,
        )
```

`connect_sse()` 内部（`sse.py:122-199`）：

1. 创建内存流对
2. 分配 session_id（UUID）
3. 启动 SSE writer 协程（把 `write_stream` 里的 `SessionMessage` 序列化为 SSE event-stream 推给客户端）
4. EventSourceResponse 保持 HTTP 连接不关闭（这就是"长连接"）
5. `yield (read_stream, write_stream)` → 交给 `server.run()` 消费

### 启动时的连接（`serve()` 是怎么被调用的）

```python
# cli.py:129
await serve(server, host=config.listen_host, port=config.listen_port,
           instructions=instructions)

# transport.py:108
async def serve(server, host, port, instructions):
    app = create_app(server, instructions)    # 构建 Starlette 应用
    config = uvicorn.Config(app, host=host, port=port)
    uvicorn_server = uvicorn.Server(config)
    await uvicorn_server.serve()              # 阻塞，监听直到进程终止
```

### 一句话总结 transport.py 的职责

**transport.py 是"插线板"：** 把 MCP 协议处理器（`mcp.server.Server`）和 HTTP 网络（uvicorn）连接起来。每条 HTTP 请求经过 `_MessagesMiddleware` 分拣——`GET /sse` 建长连接，`POST /messages/` 投递 JSON-RPC 消息到内存流——然后 `server.run()` 从内存流读消息、调注册的 handler、把响应写回内存流、SSE writer 推给客户端。

---

## Q7: transport.py 涉及的基础概念 — SSE、ASGI、Starlette、Routes

**关联:** [[#Q6 transport.py 逐行追踪]]

### SSE（Server-Sent Events）是什么？

SSE 是 HTTP 协议之上的一种**服务端推送**机制。它只做一件事：**服务端持续向客户端推送事件，客户端通过普通的 HTTP POST 另发请求。**

```
时间 →

客户端 ──POST /messages──→ 服务端  (客户端发 JSON-RPC 请求)
客户端 ←──SSE event─────── 服务端  (服务端推送响应)
客户端 ←──SSE event─────── 服务端  (服务端推送通知)
客户端 ←──SSE event─────── 服务端  (服务端推送另一个响应)
```

| | SSE | WebSocket | 普通 HTTP |
|---|---|---|---|
| 方向 | 单向（服→客） | 双向 | 请求-响应 |
| 连接 | 长连接 | 长连接 | 短连接 |
| 协议 | HTTP | 升级后的 TCP | HTTP |

MCP 协议选择 SSE 的原因：客户端请求 → 服务端响应 这个模式天然是单向的，不需要 WebSocket 的全双工能力，SSE 更简单。

在 Harness 中，`SseServerTransport`（`mcp/server/sse.py`）就是管理 SSE 连接的：

- `connect_sse()`：建立 SSE 长连接，创建内存流对，分配 session_id
- `handle_post_message()`：解析客户端 POST 过来的 JSON-RPC 消息，投递到对应 session 的内存流

```
transport.py:89   sse = SseServerTransport("/messages/")
                          │
                          ├─ connect_sse()        ← 处理 GET /sse，建立长连接
                          └─ handle_post_message() ← 处理 POST /messages/，接受 JSON-RPC
```

### ASGI 是什么？

ASGI = **Asynchronous Server Gateway Interface**。它是 Python Web 世界里服务器和应用之间的**标准接口规范**。

**类比：** USB 接口规范。不管你插的是鼠标还是键盘（应用），只要符合 USB 规范，任何电脑（服务器）都能用。ASGI 就是 Python 异步 Web 的 "USB 标准"。

这套规范定义了一个函数签名：

```python
async def application(scope, receive, send):
    ...
```

三个参数：

| 参数 | 含义 | 类比 |
|---|---|---|
| `scope` | 连接信息（类型、路径、method、headers...） | 快递单上的收件地址 |
| `receive` | 异步可调用对象，用来读取客户端发来的数据 | 收快递 |
| `send` | 异步可调用对象，用来向客户端发送数据 | 发快递 |

任何符合这个签名的可调用对象，都是一个 **ASGI 应用**。

### Starlette 是什么？为什么叫它 "app"？

Starlette 是一个**轻量级异步 Web 框架**（可以理解为 async 版的 Flask）。

```python
# transport.py:96-102
app = Starlette(
    routes=[],
    middleware=[...],
)
```

`Starlette(...)` 返回的对象就是一个 **ASGI 应用**——它内部实现了 `async def __call__(self, scope, receive, send)`，所以可以被 uvicorn 驱动。因此习惯上叫它 `app`。

Starlette 提供的核心能力：

| 能力 | 用途 |
|---|---|
| **路由（routes）** | URL pattern → handler 函数 |
| **中间件（middleware）** | 在请求到达 handler 之前/之后做处理 |
| **请求/响应对象** | 方便地读 headers、body、写响应 |

**uvicorn 和 Starlette 的关系：**

```
┌─────────┐      ASGI 规范       ┌──────────────┐
│ uvicorn  │ ◄────────────────► │  Starlette    │
│ (服务器)  │   (scope,receive,  │  (app/框架)   │
│          │    send)           │              │
└─────────┘                    └──────────────┘
  监听端口                        路由、中间件、
  解析 HTTP                       请求处理
```

- **uvicorn**：负责网络层——监听端口、接收 TCP 连接、解析 HTTP 协议、把请求转为 ASGI 三元组
- **Starlette**：负责应用层——收到 ASGI 三元组后，走路由表、中间件链，找到最终处理函数

### Routes（路由）是什么？

路由 = **URL pattern → handler 函数** 的映射表。

```python
# 典型 Starlette 用法（Harness 没有这样做，但这是标准模式）：
app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse, methods=["GET"]),
    Route("/messages/{session_id}", endpoint=handle_message, methods=["POST"]),
])
# 含义：
#   GET /sse              → handle_sse()
#   POST /messages/abc123 → handle_message()
```

**Harness 的特殊之处：routes 是空的！**

```python
# transport.py:96-98
app = Starlette(
    routes=[],   # ← 空列表！不注册任何常规路由
    middleware=[
        Middleware(_MessagesMiddleware, sse=sse, server=server, ...),
    ],
)
```

为什么？因为 Starlette 1.0 的常规路由在处理 MCP SDK 的原生 ASGI handler 时有兼容性问题（`transport.py:28-35` 注释解释了这个 bug）。所以 Harness 把路由逻辑全部移到了 `_MessagesMiddleware` 中间件里，在中间件层面手工做 if-else 分拣：

```python
# transport.py:44-66 — 手工路由
if method == "POST" and path.startswith("/messages/"):
    await self._sse.handle_post_message(...)    # → MCP 消息处理
elif method == "GET" and path == "/sse":
    await self._handle_sse(...)                 # → SSE 连接建立
else:
    await self.app(scope, receive, send)         # → 其他路径，Starlette 默认
```

### 把这些概念串在一起看 transport.py

```
uvicorn (网络服务器)
  │  监听 :9000，收到 HTTP 请求 → 转为 ASGI (scope, receive, send)
  ▼
Starlette app (ASGI 应用)
  │  transport.py:96  app = Starlette(routes=[], middleware=[...])
  │
  ├─ CORSMiddleware        ← 先过 CORS（允许跨域）
  │
  ├─ _MessagesMiddleware   ← 再过路由中间件
  │     │
  │     ├─ POST /messages/* → sse.handle_post_message()
  │     │   解析 JSON-RPC → writer.send() → 内存流 → server.run() 消费
  │     │
  │     ├─ GET /sse         → _handle_sse()
  │     │   sse.connect_sse() → 建内存流 → server.run() 阻塞消费
  │     │
  │     └─ 其他              → Starlette 默认处理
  │
  └─ (routes=[] — 没有注册任何路由，所以其他路径通常 404)
```

### 一句话总结

| 概念 | 一句话 |
|---|---|
| SSE | HTTP 服务端推送机制，单向（服→客），用于 MCP 响应和通知 |
| ASGI | Python 异步 Web 的标准接口规范 = `async def app(scope, receive, send)` |
| Starlette | 轻量异步 Web 框架，实现 ASGI 规范，提供路由/中间件 |
| app | 符合 ASGI 规范的可调用对象，Starlette() 创建的实例 |
| routes | URL pattern → handler 的映射表。Harness 里故意为空，路由全在中间件里手工做 |

---

## Q5: 两个 Server 的区别 — UE MCP Server vs Harness MCP Server

**关联:** [[#Q4 Harness 整体架构]]

### 先纠正一个可能的混淆

你在代码里看到两处 "Server"：

```python
# server.py:23
from mcp.server import Server        # ← 这是 mcp SDK 提供的"空框架"

# server.py:66
server = Server("ue-agent-harness")   # ← 创建一个 MCP 协议处理器实例
```

**这个 `Server` 不是"一个正在监听的服务器进程"。** 它只是 `mcp` SDK 提供的一个**空壳类**——它知道怎么解析 JSON-RPC、怎么把返回值序列化回去，但它**不做任何网络监听**。它的核心用途是让你用装饰器注册处理函数：

```python
@server.list_tools()       # 注册：当收到 "tools/list" 时调这个函数
async def list_tools(): ...

@server.call_tool()        # 注册：当收到 "tools/call" 时调这个函数
async def call_tool(): ...
```

真正在网络上监听的是 `transport.py` 里的 **uvicorn + Starlette**，它负责接收 HTTP 请求，然后把 JSON-RPC 消息交给 `mcp.server.Server` 实例去处理。

### 真正需要区分的"两个 Server"：Harness vs UE

从网络拓扑看，整个系统有两个**监听端口的服务器进程**：

```
                    Harness MCP Server                    UE MCP Server
                    (端口 9000)                           (端口 8000)
┌──────────┐       ┌──────────────────┐                 ┌─────────────────┐
│ 测试脚本   │ ────→ │  transport.py     │                 │  UE Editor 内部  │
│ (LLM/CLI) │ ←──── │  (uvicorn+SSE)   │ ────→ ────→    │  (UE MCP Plugin) │
└──────────┘       │                  │ ←──── ←────    │                 │
                   │  server.py       │                 │  211 个原始工具   │
                   │  (mcp SDK Server) │                 │                 │
                   └──────────────────┘                 └─────────────────┘
```

| 对比维度 | Harness MCP Server | UE MCP Server |
|---|---|---|
| **谁写的** | **Harness 项目自己写的** | UE MCP Plugin（UE 插件，别人写的） |
| **端口** | `9000`（可配） | `8000`（可配） |
| **实现语言** | Python | C++/Blueprint (UE 插件) |
| **工具数量** | ~24 个（过滤后 + 自有） | ~211 个（全部 UE 工具） |
| **角色** | **中间层代理**——过滤、日志、缓存、Skill | **源头**——直接操作 UE Editor |
| **面向谁** | LLM / AI Agent / 测试脚本 | Harness（Harness 作为客户端连它） |
| **依赖关系** | 依赖 UE Server 才能工作 | 独立运行（随 UE Editor 启动） |

### 功能差异

**UE MCP Server（别人写的，Harness 拿来用）：**
- 把 UE Editor 的 211 个工具暴露为 MCP 接口
- 工具包括：选择 Actor、移动对象、获取场景信息、截图……
- 只做"翻译"——把 UE 内部 API 翻译成 MCP 协议
- 不做任何过滤、不做日志、不知道 LLM 的存在

**Harness MCP Server（Harness 项目自己写的）：**
- 代理 UE Server，在中间加增值逻辑：
  - **004 过滤**：211 个工具 → 过滤到 ~20 个（防止 LLM 被淹没）
  - **003 日志**：每个工具调用写 JSONL，用于调试和回放
  - **005 Skill**：激活 Skill 后进一步缩小工具范围
  - **008 缓存**：维护 WorldState 快照，避免重复查询 UE
  - **自有工具**：注入 `activate_skill`、`get_context` 等 Harness 专属工具
- 面向 LLM——工具描述、系统 prompt、上下文组装都是为 AI 优化的

### Harness 内部的 Server 对象 vs UE 连接对象

再看启动代码里这两个变量，容易混：

```python
# cli.py:66
ue_client = McpClientSession(config)    # Harness 连接 UE Server 的"客户端"

# cli.py:113 (→ server.py:66)
server = build_server(...)             # Harness 自己的 MCP 协议处理器
```

| 变量 | 类型 | 方向 | 含义 |
|---|---|---|---|
| `ue_client` | `McpClientSession` | Harness → UE :8000 | Harness 作为**客户端**去调 UE 的工具 |
| `server` | `mcp.server.Server` | LLM → Harness :9000 | Harness 作为**服务端**接收 LLM 的请求 |

**Harness 同时扮演两个角色：** 对 LLM 它是 Server，对 UE 它是 Client。这正是"中间层代理"的含义——左手接 LLM 的 MCP 请求，右手发 HTTP 去调 UE。

### 为什么需要 Harness 这个中间层？直接连 UE 不行吗？

**技术上可以**，测试脚本直接 `sse_client("http://127.0.0.1:8000/sse")` 也能连 UE。但会有这些问题：

1. **工具太多**：211 个工具丢给 LLM，超出上下文窗口，且选择困难
2. **无日志**：工具调用没有记录，出问题无法回放排查
3. **无 Skill 模式**：无法按任务缩小工具范围
4. **无缓存**：每次查询都去 UE 拿一遍，慢且浪费
5. **无安全护栏**：LLM 可能误调危险工具（如删除资源）

Harness 把这些横切关注点从 UE 里抽出来，单独做了一层。
