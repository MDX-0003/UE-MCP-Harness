# take_screenshot(mode="viewport") 全链路时序

日期：2026-06-30

从测试脚本一句 `session.call_tool("take_screenshot", {"mode": "viewport"})` 开始，按时间顺序追踪每条数据经过的函数和边界，直到 `_try_file_fallback` → `_poll_and_capture` 的文件轮询路径。

**路径约定：** 所有文件路径相对于 `UE-MCP-Harness` 根目录。

---

## 时序追踪

### 0. Harness 启动（前提）

```
cli.py:cmd_start()
  ├─ ue_client = McpClientSession(Config.from_env())
  │     ue_client._config.sse_read_timeout = 120.0  ← 主 session，不用于截图
  │
  ├─ await init_shot_session(config)               ← capturer.py:42
  │     ├─ shot_config = Config(sse_read_timeout=60.0, ...)
  │     ├─ _shot_client = McpClientSession(shot_config)
  │     ├─ await _shot_client.connect()
  │     │     └─ httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=60.0))
  │     │
  │     └─ _ue_screenshot_dir = await _resolve_screenshot_dir(config)
  │           ├─ 优先级 0: HARNESS_UE_SCREENSHOT_DIR 环境变量
  │           ├─ 优先级 1: 进程自动发现（netstat→wmic→.uproject 提取）→ /Saved/Screenshots/WindowsEditor
  │           └─ 优先级 2: HARNESS_UE_PROJECT_ROOT 环境变量 → /Saved/Screenshots/WindowsEditor
  │
  └─ await serve(server, ...)  ← Harness 在 :9000 开始监听
```

### 1. 测试脚本发起调用

```
tool_verify_harness_vision.py:97

session.call_tool("take_screenshot", {"mode": "viewport", "hide_ui": True})
```

`session` = `mcp.ClientSession`（mcp SDK）。它把调用序列化为：

```json
{"jsonrpc":"2.0","id":N,"method":"tools/call",
 "params":{"name":"take_screenshot","arguments":{"mode":"viewport","hide_ui":true}}}
```

通过 `POST http://127.0.0.1:9000/mcp` 发给 Harness。传输层是 Streamable HTTP（`streamablehttp_client`）。

### 2. Harness 接收 → MCP SDK 路由

测试脚本用的是 `streamablehttp_client`（MCP 2025 Streamable HTTP 规范），请求始终发到 `POST http://127.0.0.1:9000/mcp`。

#### 2.1 uvicorn → ASGI 三元组

```
测试脚本
  │  POST http://127.0.0.1:9000/mcp
  │  Content-Type: application/json
  │  Body: {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"take_screenshot",...}}
  ▼
uvicorn (transport.py:201)
  │  监听 :9000，收到 TCP 连接
  │  解析 HTTP: method=POST, path=/mcp, headers={...}, body=<bytes>
  │  构造 ASGI 三元组:
  │    scope   = {"type":"http", "method":"POST", "path":"/mcp", "headers":..., ...}
  │    receive = <awaitable: 读 body>
  │    send    = <awaitable: 写响应>
  │  调用 Starlette app(scope, receive, send)
  ▼
```

#### 2.2 Starlette 中间件链

`create_app()`（`transport.py:106`）注册了两层中间件，按顺序执行：

```
Starlette(scope, receive, send)
  │
  ├─ Middleware 0: CORSMiddleware (transport.py:131)
  │     allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
  │     → 本地连接，无操作，继续传递
  │
  ├─ Middleware 1: _StreamableHttpMiddleware (transport.py:145)
  │     transport.py:91-103
  │     │
  │     │  scope["type"] == "http"? → 是
  │     │  scope["path"] == "/mcp"? → 是
  │     │
  │     │  调用 self._transport.handle_request(scope, receive, send)
  │     │    │
  │     │    └─ StreamableHTTPServerTransport.handle_request() ← MCP SDK
  │     │          │
  │     │          │  ① 读取 HTTP body:
  │     │          │       body_bytes = await receive() → 拿到完整的 JSON-RPC 字节
  │     │          │
  │     │          │  ② 反序列化 JSON-RPC 消息:
  │     │          │       message = JSONRPCMessage.model_validate_json(body_bytes)
  │     │          │       → JSONRPCMessage(root=ClientRequest(
  │     │          │             root=CallToolRequest(
  │     │          │               params=CallToolRequestParams(
  │     │          │                 name="take_screenshot",
  │     │          │                 arguments={"mode":"viewport","hide_ui":True}
  │     │          │               )
  │     │          │             )
  │     │          │           ))
  │     │          │
  │     │          │  ③ 写入内存流:
  │     │          │       把这个 JSONRPCMessage 对象推入 read_stream → write_stream 对
  │     │          │       （connect() 时创建的内存管道）
  │     │          │
  │     │          │  ④ 等待响应:
  │     │          │       read_stream 的另一端会写回 JSONRPCResponse
  │     │          │       拿到后序列化 → send() 给 uvicorn → uvicorn 写 HTTP 响应回测试脚本
  │     │          │
  │     │          return  ← ASGI handler 结束，响应已发出
  │     │
  │     return  ← _StreamableHttpMiddleware 短路，不继续往下
  │
  └─ (后续中间件不会再执行，_StreamableHttpMiddleware 已短路)
```

#### 2.3 后台 MCP Server 消息循环

`serve()` 在启动时创建了一个后台 `asyncio.Task` 跑 `server.run()`（`transport.py:210-215`）：

```
transport.py:210  async with sh_transport.connect() as (read_stream, write_stream):
                       │
                       │  connect() 创建一对 anyio memory object stream:
                       │    read_stream  ← 服务端读消息
                       │    write_stream ← 服务端写响应
                       │
                       ├─ asyncio.create_task(server.run(read_stream, write_stream, _init_opts))
                       │     │
                       │     │  MCP SDK 内部循环（简化）:
                       │     │    async for message in read_stream:
                       │     │        match message:
                       │     │          case ClientRequest(root=InitializeRequest):
                       │     │            → 自动处理握手（返回 serverInfo、protocolVersion 等）
                       │     │          case ClientRequest(root=ListToolsRequest):
                       │     │            → 调用 server.py 中 @server.list_tools() 注册的 handler
                       │     │          case ClientRequest(root=CallToolRequest):
                       │     │            → 调用 server.py 中 @server.call_tool() 注册的 handler
                       │     │               │
                       │     │               ▼
                       │     │            server.py:232  call_tool(name="take_screenshot", arguments={...})
                       │     │               │
                       │     │               ▼ ── 从这里进入步骤 3 ──
                       │     │
                       │     │        handler 返回 CallToolResult → MCP SDK 序列化为 JSONRPCResponse
                       │     │        → 写入 write_stream
                       │
                       └─ uvicorn_server.serve()  ← 主协程，监听 HTTP
```

**关键：内存流是进程内管道。** `handle_request()` 把 JSON-RPC 消息**写入**写端，`server.run()` 从读端**读出**。两端跑在不同的 asyncio Task 中，但共享同一进程内存空间。没有网络往返——只是 Python 对象在两个协程间传递。

#### 2.4 完整路径打平

```
测试脚本 POST /mcp (JSON-RPC bytes over TCP)
  │
  ▼
uvicorn → 解析 HTTP → ASGI (scope, receive, send)
  │
  ▼
Starlette app
  ├─ CORSMiddleware (无操作)
  └─ _StreamableHttpMiddleware
       │  path="/mcp" 匹配
       ▼
     StreamableHTTPServerTransport.handle_request(scope, receive, send)
       │  ① await receive() 读 HTTP body 字节
       │  ② JSONRPCMessage.model_validate_json(body_bytes)
       │  ③ 写入内存流的写端
       │  ④ 等待内存流读端返回 JSONRPCResponse
       │  ⑤ send(HTTP响应) 回测试脚本
       ▼
     [内存流]  ← 进程内 anyio memory object stream
       │
       ▼
     server.run(read_stream, write_stream, _init_opts)  ← 后台 asyncio Task
       │  async for message in read_stream:
       │    匹配 CallToolRequest(root=CallToolRequest)
       │    → self._handle_request → 查找 registered handler
       │       → server.py 中 @server.call_tool() 注册的 call_tool()
       │          → name="take_screenshot" 匹配 line 340 分支
       │
       ▼ ── 进入步骤 3 ──
     server.py:232 call_tool(name="take_screenshot", arguments={...})
```
### 3. server.py 分发：take_screenshot handler

Handler 不是"被调的 object"，是"收到特定消息时负责响应它的函数"。
```
    server.py:231
    @server.call_tool()                    # ← "把这个函数注册为 tools/call 的 handler"
    call_tool(name="take_screenshot", arguments={"mode":"viewport","hide_ui":true})

    name="take_screenshot" → 匹配 line 340 的 if 分支

    server.py:345  拆参数:
        mode        = arguments.get("mode", "viewport")      → "viewport"
        asset_path  = arguments.get("asset_path", "")        → ""
        hide_ui     = arguments.get("hide_ui", False)        → True
        max_w, max_h = config.vision_max_size                → 1024, 768

    server.py:349  调入口:
        screenshot = await capturer_capture(
            ue_client, 1024, 768,
            mode="viewport", asset_path="", hide_ui=True,
        )
```

### 4. capturer.py 校验并分发模式

```
capturer.py:90  capture(ue_client, max_width=1024, max_height=768,
                         mode="viewport", asset_path="", hide_ui=True)

    line 110: mode in ("viewport","editor","asset")? → True

    line 112: mode="asset" 且 asset_path 为空?
        → mode="viewport"，不触发 → 通过

    line 118: _shot_client 存在且已连接? → True

    line 123: async with _shot_lock:  ← 获取串行锁

    line 145: mode="viewport" → 进入 viewport 分支:
        return await _capture_asset_image_with_file_fallback(
            asset_path="",
            b_show_ui=False,   # hide_ui=True → b_show_ui=False
            max_width=1024,
            max_height=768,
        )
```

### 5. 核心编排：正常路径 → 双异常分叉

```
capturer.py:304  _capture_asset_image_with_file_fallback(
                     asset_path="", b_show_ui=False,
                     max_width=1024, max_height=768)


    ① 组装参数 ─────────────────────────────────────────
    tool_name = "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
    args      = {"AssetPath": "", "bShowUI": False}
    start_wall = time.time()          ← 打时间戳，后续 fallback 用来过滤旧文件


    ② 正常路径 try ──────────────────────────────────────
    capturer.py:320
    result = await _shot_client.call_tool(tool_name, args)
      │
      └─→ 进入 client.py call_tool()  ────────────────┐
                                                       │
      client.py:246  self._http.stream("POST", ...)     │
        → UE 返回 HTTP 200 + Content-Type: text/event-stream
        → response.aiter_lines() 逐行等 SSE             │
                                                       │
      client.py:471  async for line in response.aiter_lines():  ← 阻塞于此
        │  每行解析 SSE 事件                             │
        │  遇空行 → 组装 data → json.loads              │
        │  遇 "result" key → return json.dumps(result)   │  正常返回
        │                                               │
        │  httpx 底层 socket recv() 受 AsyncClient      │
        │  timeout(read=60.0) 限制                      │
        │                                               │
        ├─ 60s 内收到 final SSE frame:                  │
        │    → return 结果字符串                          │
        │    → capturer.py 回到 try 块                   │
        │    → parse_screenshot(result) → return Screenshot  ← 正常结束
        │                                               │
        └─ 60s 内无新字节:                               │
             → httpx.ReadTimeout                         │  路径A
                                                         │
        ── 或 ──                                         │
                                                         │
        aiter_lines() 迭代完毕（连接关闭）但无 "result":   │
             → JsonRpcError(-32000, "SSE 流结束...")      │  路径B
                                                         │
                                                       ──┘


    ③ 路径A: socket 超时 ────────────────────────────────
    capturer.py:322
    except httpx.ReadTimeout:
        screenshot = await _try_file_fallback(
            tool_name="ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
            asset_path="",
            start_wall=<请求开始时间戳>,
            max_width=1024,
            max_height=768,
        )
        if screenshot is not None:
            return screenshot   ← fallback 成功
        raise                   ← fallback 失败，重抛原异常


    ④ 路径B: SSE 流结束无 result ────────────────────────
    capturer.py:329
    except JsonRpcError as exc:
        if not _is_sse_no_result_error(exc):
            raise               ← 其他 JsonRpcError，不透传
        # code==-32000 且 message 含 "SSE 流结束但未找到工具结果"
        screenshot = await _try_file_fallback(...)  ← 同路径A
        if screenshot is not None:
            return screenshot
        raise
```

### 6. _try_file_fallback：三道门检查

```
capturer.py:243  _try_file_fallback(tool_name, asset_path, start_wall, 1024, 768)

    门1: _should_use_file_fallback(tool_name, asset_path)
        → tool_name == "ToolsetRegistry.EditorAppToolset.CaptureAssetImage" ✓
        → asset_path == "" ✓
        → 通过

    门2: _ue_screenshot_dir is None?
        → 否（init_shot_session 时自动发现成功）→ 通过

    门3: _ue_screenshot_dir.is_dir()?
        → 是 → 通过

    return await _poll_and_capture(
        directory=_ue_screenshot_dir,
        start_wall=<请求开始时间戳>,
        max_width=1024,
        max_height=768,
    )
```

### 7. _poll_and_capture：轮询等文件

```
capturer.py:276  _poll_and_capture(directory, start_wall, 1024, 768)

    deadline = time.time() + 180     ← _FALLBACK_POLL_SECONDS

    while time.time() < deadline:

        latest = _find_latest_screenshot(directory, since=start_wall - 2.0)
            │
            ├─ 扫 directory/*.png
            ├─ 过滤: st_size > 0
            ├─ 过滤: mtime >= since (start_wall - 2.0)
            ├─ 过滤: mtime <= now + 2.0
            └─ 返回 mtime 最新的那张（或 None）

        如果 latest 不空:
            if _looks_like_png(latest):         ← 检查 magic bytes
                try:
                    return capture_from_file(latest, 1024, 768)
                except Exception:
                    return None                 ← 文件读坏了

        没找到 → await asyncio.sleep(3)          ← _FALLBACK_POLL_INTERVAL
                → 下一轮

    循环退出:
        logger.info("文件 fallback 轮询超时 (180s)...")
        return None                             ← 彻底放弃
```

### 8. 回到 server.py：post 拦截器链

```
server.py:370  成功拿到 screenshot 后:

    result_text = "Screenshot 已获取: 1024x630 image/png (mode=viewport)"

    event = ToolCallCompleted(
        name="take_screenshot",
        args={"mode":"viewport",...},
        raw_result={"content":[{"type":"text","text":result_text}]},
        parsed_text=result_text,
    )

    for ic in interceptors:        ← 手动触发，因为 take_screenshot 不走 UE 透传路径
        await ic.post_call(event)
          │
          ├─ VisionInterceptor   → 发截图给 Vision API 分析
          └─ SnapshotRecorder    → 存档截图到会话目录

    return CallToolResult(content=[TextContent(text=result_text)])
```

### 9. 回到测试脚本

```
tool_verify_harness_vision.py:97  r  = session.call_tool(...)
    r.isError → False
    text     → "Screenshot 已获取: 1024x630 image/png (mode=viewport)"

    line 107: await asyncio.sleep(5)        ← 等 Vision API 完成
    line 110: ctx = await session.call_tool("get_context", {})
    line 111: if "上次视觉验证" in ctx:      ← Vision 闭环确认
                print("✅ VisionInterceptor 已触发")
```

---

## 硬编码常量汇总

### A. 超时类

| 值 | 位置 | 说明 |
|------|------|------|
| `60.0` | `capturer.py:57` | 截图专用 session 的 SSE 读取超时（秒）。超过后触发文件 fallback |
| `120.0` | `config.py:37` | 主 session 的 SSE 读取超时默认值（秒）。截图 session 不使用此值 |
| `30.0` | `config.py:36` | 普通 HTTP 请求超时默认值（秒） |
| `180` | `capturer.py:272` `_FALLBACK_POLL_SECONDS` | 文件 fallback 轮询上限（秒） |
| `3` | `capturer.py:273` `_FALLBACK_POLL_INTERVAL` | 文件 fallback 轮询间隔（秒） |
| `5` | `capturer.py:358` | `_list_listening_pids()` 中 netstat 子进程超时（秒） |
| `5` | `capturer.py:392` | `_get_process_command_line()` 中 wmic 子进程超时（秒） |
| `10` | `capturer.py:421` | `_get_process_command_line_ps()` 中 PowerShell 回退超时（秒） |
| `5` | `server.py` → `tool_verify_harness_vision.py:107` | 测试脚本中 `await asyncio.sleep(5)` 等 Vision API |

### B. 尺寸/图像类

| 值 | 位置 | 说明 |
|------|------|------|
| `1024, 768` | `config.py:60` `vision_max_size` | 截图 resize 最大尺寸。通过 `server.py:348` 传入 `capture()` |
| `1024, 768` | `capturer.py:91-92` | `capture()` 参数默认值（与 config 一致） |
| `1024, 768` | `capturer.py:161` | `capture_from_file()` 参数默认值 |
| `8` | `capturer.py:210` | PNG magic bytes 校验长度 `b"\x89PNG\r\n\x1a\n"` |

### C. 字符串匹配类

| 值 | 位置 | 说明 |
|------|------|------|
| `"ToolsetRegistry.EditorAppToolset.CaptureAssetImage"` | `capturer.py:193` `_should_use_file_fallback` | 判断是否 viewport 分支的工具全限定名 |
| `""` | `capturer.py:194` `_should_use_file_fallback` | 空 asset_path = viewport 语义 |
| `-32000` | `capturer.py:201` `_is_sse_no_result_error` | 匹配 `JsonRpcError` 的 code |
| `"SSE 流结束但未找到工具结果"` | `capturer.py:202` `_is_sse_no_result_error` | 匹配 `JsonRpcError` 的 message |
| `-32000` | `client.py:522-525` | 抛出 `JsonRpcError` 时的 code 和 message 模板 |
| `"viewport", "editor", "asset"` | `capturer.py:110` | `capture()` mode 参数的合法值枚举 |
| `"viewport"` | `capturer.py:95` | `capture()` mode 参数默认值 |
| `"take_screenshot"` | `server.py:191` | Harness 自有工具名。`server.py:340` 的路由分支也用此字符串匹配 |

### D. 路径/地址类

| 值 | 位置 | 说明 |
|------|------|------|
| `8000` | `config.py:19` | UE MCP Server 默认端口 |
| `9000` | `config.py:30` | Harness MCP Server 默认监听端口 |
| `"127.0.0.1"` | `config.py:20` `ue_host` | UE MCP Server 默认地址 |
| `"127.0.0.1"` | `config.py:29` `listen_host` | Harness 监听默认地址 |
| `"2025-11-25"` | `config.py:33` `mcp_protocol_version` | MCP 协议版本 |
| `"Saved/Screenshots/WindowsEditor"` | `capturer.py:490` `_resolve_screenshot_dir` | 截图目录相对路径拼接 |
| `"http://127.0.0.1:9000/mcp"` | `tool_verify_harness_vision.py:20` | 测试脚本连接地址 |
| `120` | `tool_verify_harness_vision.py:27` | 测试脚本的 `streamablehttp_client` 超时 |

### E. 其他

| 值 | 位置 | 说明 |
|------|------|------|
| `asyncio.Lock()` | `capturer.py:36` `_shot_lock` | 截图调用串行锁 |
| `None` | `capturer.py:39` `_ue_screenshot_dir` | 截图目录初始值，`init_shot_session` 时赋值 |
| `False` | `client.py:112` `http2=False` | httpx AsyncClient 禁用 HTTP/2 |
| `False` | `capturer.py:58` `preload_all_toolsets=False` | 截图 session 不预加载工具集 |
| `2.0` | `capturer.py:224` `_find_latest_screenshot` | mtime 窗口左边界余量 `since = start_wall - 2.0` |
| `2.0` | `capturer.py:235` `_find_latest_screenshot` | mtime 窗口右边界余量 `now + 2.0` |
| `True` | `config.py:40` `preload_all_toolsets` | 主 session 默认预加载工具集 |
| `"Mcp-Session-Id"` | `client.py:309` | HTTP header 名 |
| `"Mcp-Protocol-Version"` | `client.py:311` | HTTP header 名 |
