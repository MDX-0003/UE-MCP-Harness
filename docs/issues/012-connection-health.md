# 012 — 传输层连接健康检测与自动重连

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

`McpClientSession._connected`（[client.py:119](harness/client.py#L119)）是一个纯本地布尔标志位——只在 `connect()` 成功时翻 `True`，`close()` 时翻 `False`。UE 崩溃、进程被杀、网络断开——这三种场景下 `_connected` 永远停留在 `True`，`_ensure_connected()` 快乐放行，真正的 `httpx` 调用在超时（120s）后才炸。

本 Issue 做三件事：

1. **精准翻旗**：只在能 100% 确定 UE 已断开的异常类型上翻 `_connected = False`
2. **存活确认**：新增 `McpClientSession.ping()` 方法，提供独立的、不与工具调用耦合的 UE 存活检测手段
3. **自动重连**：`_ensure_connected()` 从纯"门卫"升级为"门卫 + 急救员"——发现旗倒了 → ping 一次确认 UE 已恢复 → 完整重连（握手 + session + L3 full_refresh）

## 验收标准

- [ ] `call_tool()` 抛出 `httpx.ConnectError` → `_connected` 翻为 `False`，错误仍上报 LLM
- [ ] `call_tool()` 抛出 `httpx.ReadTimeout` → `_connected` **不翻**，仅 `_cancel_request()` + 上报 LLM
- [ ] `call_tool()` 抛出 `httpx.RemoteProtocolError` → 先用 `ping(2s)` 二次确认，确认断开才翻 `_connected`
- [ ] `McpClientSession.ping(timeout=3.0)` → 返回 `bool`，不依赖现有 session，3 秒内出结果
- [ ] `_ensure_connected()`：`_connected=False` 时自动调 `ping()` → 成功则执行 `reconnect()` + `full_refresh()` → `_connected=True` → 继续执行原调用
- [ ] 主 session 重连成功后，截图专用 session（[capturer.py:42](harness/verification/capturer.py#L42)）同步重建
- [ ] `_cancel_request()` 取消注释，用于 `ReadTimeout` 场景（防止 UE 端继续往已废弃的 SSE 流写结果）
- [ ] 涉及变更：`harness/client.py`、`harness/cli.py`（重连后重建 shot session）、`harness/verification/capturer.py`（暴露 `init_shot_session` 可重复调用）
- [ ] 新增测试：`tests/test_client_health.py` — mock `ConnectError` / `ReadTimeout` / `RemoteProtocolError`，验证翻旗逻辑 + ping 行为

## 阻塞

无。完全自包含于 `harness/client.py`，不依赖任何未完成模块。

## 为什么这是一个独立 Issue（不与 010 合并）

此问题与 Issue 010（[docs/issues/010-error-recovery.md](docs/issues/010-error-recovery.md)）在架构上属于不同层：

| 维度 | Issue 012（本 Issue） | Issue 010（错误恢复） |
|------|----------------------|----------------------|
| **层级** | TCP/HTTP 传输层 | JSON-RPC / MCP 语义层 |
| **实现位置** | `McpClientSession` 自身 | `harness/recovery/` — Interceptor 链 |
| **检测手段** | `httpx` 异常类型 + `ping()` 存活确认 | Regex 匹配工具返回的错误字符串 |
| **处理方式** | 翻连接标志位 + 重连 + 恢复 session | 按错误类别重试 / 上报 LLM |
| **Contract 关系** | 不属于 Interceptor 模式（Contract 1） | 属于 post_call 语义处理 |

**关键反例：`ReadTimeout`。** Issue 010 如果接管了连接健康检测，它的分类器会把所有 `ReadTimeout` 归为 `TIMEOUT` 类 → 自动重试。但 `ReadTimeout` 在 SSE 流式场景下**不等于 UE 断开**——详见下文 §为什么 ReadTimeout 不能翻旗。

两个 Issue 合并会迫使传输层的存活判断逻辑挤进语义分类器的框架里，产生错误的重连决策。

## 设计说明

### 核心原则：没有铁证就不假设 UE 挂了

`call_tool()` 的异常类型矩阵：

| 异常 | 含义 | TCP 状态 | UE 确定不在？ | 翻 `_connected`？ | 后续动作 |
|------|------|----------|:---:|:---:|------|
| `httpx.ConnectError` | TCP 握手失败，端口无人监听 | 未建立 | ✅ 是 | ✅ 翻 False | 上抛，等下次调用触发重连 |
| `httpx.ReadTimeout` | SSE 流在 `sse_read_timeout` 内无数据 | 已建立，可能存活 | ❌ 否 | ❌ **不翻** | `_cancel_request()` + 上报 LLM |
| `httpx.RemoteProtocolError` | TCP RST 或 HTTP 协议违反 | 已断开 | ⚠️ 大概率 | ❌ 先 ping | ping 失败才翻 |
| HTTP 4xx/5xx | UE 可达但服务端报错 | 正常 | ❌ 否 | ❌ 不翻 | 上报 LLM |
| `JsonRpcError` | JSON-RPC 层错误（如 `SSE 流结束但没有 result`） | 正常 | ❌ 否 | ❌ 不翻 | 上报 LLM |

### 为什么 `ReadTimeout` 不能翻旗——反例详解

`_read_sse_stream()`（[client.py:473](harness/client.py#L473)）用 `aiter_lines()` 逐行消费 SSE 流。UE MCP Server 对 `tools/call` 使用 `MultipleWriteStream` + `Connection: keep-alive`——两阶段写入：

1. 第一阶段：立即返回 SSE 流头（空事件）
2. 第二阶段：工具执行完成后，通过回调 lambda 写入 SSE result frame

两阶段之间连接保持活跃，但没有任何数据流动。如果工具执行时间超过 `sse_read_timeout`（默认 120s），`aiter_lines()` 超时抛出 `ReadTimeout`——**此时 TCP 连接仍然健康，UE 进程正在执行工具，只是还没写完结果。**

**已有案例：截图工具超时。** [capturer.py](harness/verification/capturer.py) 的整个 fallback 链路（`CaptureEditorImage` → `CaptureAssetImage` → 文件 fallback）的前提假设就是：超时不等于 UE 挂了。如果 `ReadTimeout` 时自动翻 `_connected = False` 并重连，你会：

1. 杀死一个完全健康的截图专用 session
2. 重连成功（UE 一直活着）
3. 重试截图工具 → 再次超时（根本问题没解决）
4. `_connected` 又翻 False → 死循环

**正确做法：** ReadTimeout 只意味着"这个工具没在时间内完成"，是 Issue 010 的 `TIMEOUT` 分类要处理的场景。`_connected` 保持不动，让上层（LLM 或 Issue 010 的重试引擎）决定是否重试。

### 为什么 `ConnectError` 是唯一的铁证

`ConnectError` 发生在 TCP 三次握手阶段——操作系统返回 `ECONNREFUSED`。这意味着目标端口上**没有进程在监听**。UE MCP Server 进程要么已退出，要么尚未完成初始化。这是唯一可以 100% 推断 UE 不可用的信号。

相比之下，`RemoteProtocolError`（TCP RST）也可能来自网络中间件（负载均衡器超时断开、防火墙重置），所以需要 `ping()` 二次确认。

### 为什么不轮询 ping

**反模式识别：** 后台 `while not ping(): sleep(5)` 循环有三个问题：

1. **没有停止条件。** 谁知道 UE 什么时候重启？30 秒？5 分钟？永远不会？轮询要么过早放弃，要么永远空转。
2. **信息不对称。** 只有用户知道"我刚重启了 UE"。LLM 收到了错误，告诉用户，用户操作，用户告诉 LLM，LLM 重试——这个社会循环已经是天然的"轮询"机制。
3. **LLM 的重试就是轮询间隔。** 用户重启 UE → 告诉 LLM → LLM 调工具 → `_ensure_connected()` 发现旗倒了 → ping → 成功 → 重连 → 执行。没有后台任务，没有定时器，没有资源浪费。

**正确流程：**

```
UE 关闭
  → call_tool() 抛 ConnectError
  → _connected = False
  → 错误上报 LLM："UE 连接被拒绝，请确认编辑器是否仍在运行"
  → LLM 转告用户
  → [用户重启 UE，告诉 LLM "好了"]
  → LLM 重试工具调用
  → _ensure_connected(): _connected=False → ping(2s) → 成功！
  → await reconnect()      ← 完整 MCP 握手
  → await full_refresh()   ← L3 全量刷新 State Cache
  → _connected = True
  → 继续执行原工具调用     ← 不是重试——第一次根本没发出去
```

### `ping()` 设计

```python
async def ping(self, timeout: float = 3.0) -> bool:
    """轻量存活检测——仅确认 UE MCP Server 进程是否在监听端口。

    设计约束：
      - 不依赖 self._http（可能已随 ConnectError 变成不可用状态）
      - 不依赖 self._session_id（UE 重启后旧 session 已失效）
      - 使用独立 httpx 客户端，不干扰主连接的连接池状态
      - 短超时（默认 3s），因为这是"急救"路径而非热路径
    """
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(self._config.ue_base_url)
                return resp.status_code < 500  # 4xx 也算活着（如 404）
    except Exception:
        return False
```

**为什么用 GET 而不是 JSON-RPC ping：** `initialize` 需要完整的 JSON-RPC 握手，那正是 `reconnect()` 的职责。`ping()` 只回答一个问题："端口上有人吗？"——GET 请求足够回答这个问题，且不产生任何服务端副作用。

### `_ensure_connected()` 升级

改为 `async def`——重连逻辑内聚在方法内部，不走异常绕路。所有调用点（`call_tool`、`list_tools`、`preload_all_toolsets`）本身已是 `async def`，只需在每个调用点加 `await`，纯机械替换。

```python
async def _ensure_connected(self) -> None:
    """确认与 UE MCP Server 的连接有效。

    快速路径：_connected=True + _http 存在 → 直接通过（零 I/O）
    急救路径：_connected=False → ping 确认 UE 恢复 → 自动重连 + 钩子
    失败路径：ping 不通 → 抛 RuntimeError
    """
    if self._connected and self._http is not None:
        return  # 快速路径

    # ping 一次确认（独立 httpx 客户端，2s 超时）
    if not await self.ping(timeout=2.0):
        raise RuntimeError(
            "与 UE MCP Server 的连接已断开。"
            "请确认 UE 编辑器正在运行且 MCP Server 已启动，然后重试。"
        )

    # UE 回来了——完整重连 + 通知所有关注者
    await self.reconnect()
```

**为什么必须改为 async：** `connect()` 是 async（两次 HTTP 往返：initialize → initialized），而 `reconnect()` 必须调 `connect()`。没有同步路径，也不想维护第二套同步 HTTP 客户端（会阻塞 event loop）。直接改 async 比引入自定义异常 + try/except 分支更直白。


### `reconnect()` 方法

```python
async def reconnect(self) -> None:
    """重新连接到 UE MCP Server。

    仅在 _connected=False 且 ping 成功后调用。
    步骤：清理旧连接 → 完整握手 → 通知所有关注者（钩子链）。
    旧 session 已随 UE 进程消失，不做 DELETE（UE 重启后旧 session 不存在）。
    """
    # 1. 清理旧连接
    if self._http is not None:
        try:
            await self._http.aclose()
        except Exception:
            pass
        self._http = None

    self._session_id = ""
    self._negotiated_version = ""
    self._request_id = 0

    # 2. 完整握手
    await self.connect()

    # 3. 通知所有关注者（按注册顺序同步执行）
    for hook in self._on_reconnect:
        try:
            await hook()
        except Exception as e:
            logger.warning(
                "重连回调 %s 失败（非致命）: %s",
                getattr(hook, "__name__", hook), e,
            )
```

### 重连回调钩子：`add_reconnect_hook()`

`McpClientSession` 不知道截图、不知道 State Cache——它只知道"有一组回调要在重连后执行"。谁关心重连事件，谁自己注册。

```python
# harness/client.py — McpClientSession.__init__

self._on_reconnect: list[Callable[[], Awaitable[None]]] = []


def add_reconnect_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
    """注册重连成功后的回调。按注册顺序同步执行。

    钩子执行在 reconnect() 的末尾、_ensure_connected() 返回之前。
    单个钩子失败不阻断后续钩子或原工具调用。
    """
    self._on_reconnect.append(hook)
```

**为什么用回调而不是让 `McpClientSession` 直接操作 `capturer`：**
- `client.py` 是传输层，不应知道截图模块的存在
- `cli.py` 是唯一的跨模块接线点——跟拦截器链注册（[cli.py:130-136](harness/cli.py#L130-L136)）风格一致
- 将来加新的重连副作用（重置计数器、通知 LLM、写日志），加一个 hook 即可，不碰 `client.py`

### 截图专用 Session 的协同重建

主 session 重连成功后，shot session（[capturer.py:35](harness/verification/capturer.py#L35) 的 `_shot_client`）大概率也死了——同一个 UE 进程。`cli.py` 负责接线：

```python
# harness/cli.py — cmd_start() 内，在 ue_client 创建之后

# 主 session 重连 → 自动重建截图 session
async def _rebuild_shot_session():
    await close_shot_session()
    await init_shot_session(config)
    logger.info("截图 session 已随主 session 重建")

ue_client.add_reconnect_hook(_rebuild_shot_session)

# 主 session 重连 → L3 全量刷新 State Cache
async def _refresh_cache_on_reconnect():
    await full_refresh(ue_client, _cache)
    logger.info("State Cache 已随重连刷新")

ue_client.add_reconnect_hook(_refresh_cache_on_reconnect)
```

**前提：** `init_shot_session()` 需改为可重复调用（内部先 `close_shot_session()` 再重建），`close_shot_session()` 已经是幂等的。

### `call_tool()` 改造后的异常处理

```python
# call_tool() 的 except 块——当前代码（client.py:300-311）改造为：

except httpx.ConnectError:
    self._connected = False
    if response is not None:
        try: await response.aclose()
        except Exception: pass
    raise

except httpx.ReadTimeout:
    # UE 大概率活着，工具执行超时。不翻 _connected。
    if response is not None:
        try: await response.aclose()
        except Exception: pass
    # 通知 UE 停止执行（取消此前注释掉的 _cancel_request）
    if self._connected and self._session_id:
        try: await self._cancel_request(rid)
        except Exception: pass
    raise

except httpx.RemoteProtocolError:
    # 灰色地带：先 ping 确认
    if response is not None:
        try: await response.aclose()
        except Exception: pass
    if not await self.ping(timeout=2.0):
        self._connected = False
    raise

except Exception:
    # 兜底：不做任何假设
    if response is not None:
        try: await response.aclose()
        except Exception: pass
    raise
```

注意：以上异常处理**不吞任何异常**——所有路径最终都 `raise`。翻旗是副作用，LLM 仍然收到错误。

## 明确不在此 Issue 范围内

| 不做的事 | 理由 | 归属 |
|----------|------|------|
| 工具调用失败后的自动重试 | 属于语义层决策（该不该重试取决于错误类型） | Issue 010 |
| ReadTimeout 时自动重连 | ReadTimeout ≠ UE 断开（见上文反例） | — |
| 后台轮询 ping | LLM 重试天然就是轮询机制 | — |
| 重连失败 N 次后的降级策略 | MVP 阶段直接上报 LLM，由人类决策 | 未来增强 |

## 与其他 Issue 的关系

```
ADR 0005（Session Decoupling）
  │  承诺：MCP 断开 → Harness 保持运行 → 重连 → 恢复 Agent Session
  │
  ├── Issue 012（本 Issue）← 解决"检测断开"+"执行重连"
  │     └── 重连后触发 full_refresh()（L3）→ 恢复 State Cache
  │
  ├── Issue 010（错误恢复）← 解决"工具调用返回的错误怎么分类+重试"
  │     └── 依赖 012：传输层确认活着，才开始语义层分类
  │
  └── Issue 009（任务记忆）← 解决"长任务 context 压缩"
        └── 受益于 012：重连后 Task Memory 仍在，任务可无缝继续
```

## 测试策略

```
tests/test_client_health.py

class TestConnectionFlag:
    async def test_connect_error_flips_flag(self):
        """mock httpx 抛 ConnectError → _connected 翻 False"""
    async def test_read_timeout_does_not_flip_flag(self):
        """mock httpx 抛 ReadTimeout → _connected 保持 True"""
    async def test_remote_protocol_error_ping_success_keeps_flag(self):
        """mock RemoteProtocolError + ping 成功 → _connected 不翻"""
    async def test_remote_protocol_error_ping_fail_flips_flag(self):
        """mock RemoteProtocolError + ping 失败 → _connected 翻 False"""
    async def test_generic_exception_does_not_flip_flag(self):
        """mock 其他异常 → _connected 不动"""
    async def test_ensure_connected_ping_success_triggers_reconnect(self):
        """_connected=False + ping 成功 → 调用 reconnect()"""
    async def test_ensure_connected_ping_fail_raises(self):
        """_connected=False + ping 失败 → RuntimeError"""

class TestPing:
    async def test_ping_returns_true_when_ue_alive(self):
        """mock GET 返回 200 → True"""
    async def test_ping_returns_true_on_404(self):
        """mock GET 返回 404 → True（UE 在但路径不对）"""
    async def test_ping_returns_false_on_timeout(self):
        """mock GET 超时 → False"""
    async def test_ping_returns_false_on_connect_error(self):
        """mock GET 抛 ConnectError → False"""
    async def test_ping_uses_independent_client(self):
        """验证 ping 不依赖 self._http"""

class TestReconnectHooks:
    async def test_hooks_called_after_reconnect(self):
        """reconnect() 成功后 → 所有已注册 hook 被执行"""
    async def test_hooks_executed_in_registration_order(self):
        """hook 按 add_reconnect_hook 的注册顺序执行"""
    async def test_hook_failure_does_not_block_others(self):
        """一个 hook 抛异常 → 后续 hook 仍执行"""
    async def test_hook_failure_does_not_block_caller(self):
        """hook 抛异常 → reconnect() 不抛，原工具调用继续"""
    async def test_no_hooks_does_not_error(self):
        """没有注册任何 hook → reconnect() 正常完成"""
```
