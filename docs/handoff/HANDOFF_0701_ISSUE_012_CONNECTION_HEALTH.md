# HANDOFF 0701: Issue 012 传输层连接健康检测 — 设计与实现

日期：2026-07-01

基线文档：[docs/issues/012-connection-health.md](issues/012-connection-health.md)（完整设计） + [docs/adr/0005-session-decoupling.md](adr/0005-session-decoupling.md)（架构依据）

状态：代码已实现（23 tests），功能验证待 UE 运行时测试。

---

## 1. 解决了什么问题

`McpClientSession._connected`（[client.py](../harness/client.py)）是一个纯本地布尔标志位——只在 `connect()` 成功时翻 `True`，`close()` 时翻 `False`。UE 崩溃、进程被杀、网络断开——这三种场景下 `_connected` 永远停留在 `True`，`_ensure_connected()` 放行，真正的 `httpx` 调用在超时（120s）后才炸。

**核心改动三件事：**
1. **精准翻旗** — 只在能确认 UE 已断开的异常类型上翻 `_connected = False`
2. **存活确认** — `McpClientSession.ping()` 用独立 httpx 客户端做轻量 GET，2-3 秒超时
3. **自动重连** — `_ensure_connected()` 发现旗倒了 → ping → 成功则重连 + 执行钩子链

---

## 2. 关键讨论：问题与回答

### Q1: Issue 010（错误恢复）能否合并此问题？

**A: 不能。** 这是不同架构层的问题。

| 维度 | Issue 012（连接健康） | Issue 010（错误恢复） |
|------|----------------------|----------------------|
| 层级 | TCP/HTTP 传输层 | JSON-RPC / MCP 语义层 |
| 实现位置 | `McpClientSession` 自身 | `harness/recovery/` — Interceptor 链 |
| 检测手段 | `httpx` 异常类型 + `ping()` 存活确认 | Regex 匹配工具返回的错误字符串 |
| Contract 关系 | 不属于 Interceptor 模式（Contract 1） | 属于 post_call 语义处理 |

**关键反例：`ReadTimeout`。** 如果合并到 010，分类器会把所有 `ReadTimeout` 归为 `TIMEOUT` → 自动重试。但 SSE 流场景下 ReadTimeout ≠ UE 断开——截图工具超时的整个 fallback 链路（`CaptureEditorImage` → `CaptureAssetImage` → 文件 fallback）前提就是"超时不等于 UE 挂了"。

### Q2: httpx 报错一定代表 UE 断开吗？

**A: 不。** 三类异常，含义不同。

| 异常 | TCP 状态 | UE 确定不在？ | 翻旗？ | 理由 |
|------|----------|:---:|:---:|------|
| `ConnectError` | 未建立 | ✅ | ✅ | OS 返回 ECONNREFUSED，端口无人监听 |
| `ReadTimeout` | 已建立 | ❌ | ❌ | UE 可能正在执行工具，SSE 流沉默 |
| `RemoteProtocolError` | 已断开 | ⚠️ | 先 ping | TCP RST 可能来自网络中间件 |
| HTTP 502/503/504 | 已建立（代理） | ✅ | ✅ | 代理/网关报告后端不可达 |
| HTTP 500 | 已建立 | ❌ | ❌ | UE 可达但内部错误 |

### Q3: `_ensure_connected()` 为什么必须是 async？

因为 `connect()` 是 async（两次 HTTP 往返：initialize → initialized），而 `reconnect()` 必须调 `connect()`。没有同步路径可走，也不想维护第二套同步 HTTP 客户端（会阻塞 event loop）。

路径选择：`_ensure_connected()` 改为 `async def`，所有调用点（`call_tool`、`list_tools`、`call_tool_blocking`、`preload_all_toolsets`）加 `await`——它们本身已经是 `async def`，纯机械替换。

### Q4: 主 session 和截图 session 如何协同重连？

用回调钩子 `add_reconnect_hook()`，而不是让 `McpClientSession` 知道截图的存在。

```
cli.py 负责接线:
  ue_client.add_reconnect_hook(_rebuild_shot_session)     ← 截图 session 重建
  ue_client.add_reconnect_hook(_refresh_cache_on_reconnect) ← L3 全量刷新
```

`client.py` 是传输层，不应知道截图模块。`cli.py` 是唯一的跨模块接线点——跟拦截器链注册风格一致。

**shot session 自身的重连：** 移除了 `capture()` 中的早期 `is_connected` 守卫——守卫在 `_ensure_connected()` 急救路径之前就把门关上了。现在只检查 `_shot_client is None`，`_connected` 状态完全交给 `_ensure_connected()` 管理，shot session 可独立自愈。

### Q5: 为什么第一次调用看不到"连接已断开"提示？

代码关键行对照：

```python
# client.py call_tool() 内
if response.status_code != 200:           # 502 ≠ 200 → 进入
    await response.aread()
    if response.status_code in (502, 503, 504):
        self._connected = False           # ← 翻旗（副作用，无日志）
        raise JsonRpcError(
            "与 UE MCP Server 的连接已断开（HTTP 502）。"  # ← LLM 看到这句
            "请确认 UE 编辑器正在运行且 MCP Server 已启动。"
        )
```

翻旗发生在 `raise` 之前，异常消息字符串也在同一次调用中构造。修复后 502/503/504 走独立分支，直接输出中文提示。

`_ensure_connected()` 中的"连接已断开"提示只在**第二次**调用出现——因为此时 `_connected=False`，快速路径失效，进入急救路径 → ping 失败 → 抛 RuntimeError。

### Q6: 提示能传到 LLM 吗？

能。两条路径都包装了异常：

**路径 A — `take_screenshot`：**
```
server.py:349 capturer_capture() → 异常
server.py:359 except → log_exception() → Harness 控制台日志（你看到的）
server.py:364 return CallToolResult(isError=True,
              text="截图失败: JsonRpcError: 与 UE MCP Server 的连接已断开...")
              → MCP SDK → SSE → LLM 收到
```

**路径 B — UE 工具透传（如 `find_actors`）：**
```
server.py:406 ue_client.call_tool() → 异常
server.py:407 error = e
server.py:439 return CallToolResult(isError=True, text=f"错误: {error}")
              → MCP SDK → SSE → LLM 收到
```

Harness 控制台日志（`[ERROR] harness.verification.debug`）和 LLM 收到的 tool result 是两条独立通道。

---

## 3. 改动文件

| 文件 | 变更 | 行数 |
|------|------|:---:|
| [harness/client.py](../harness/client.py) | `ping()`, `reconnect()`, `add_reconnect_hook()`, async `_ensure_connected()`, 异常类型分离处理, HTTP 502/503/504 检测 | +85 |
| [harness/cli.py](../harness/cli.py) | 注册两个重连钩子（shot session 重建 + L3 刷新） | +12 |
| [harness/verification/capturer.py](../harness/verification/capturer.py) | `init_shot_session()` 幂等化；移除 `is_connected` 早期守卫 | +6/-2 |
| [tests/test_client_health.py](../tests/test_client_health.py) | 新增 23 tests（4 class） | +325 |

**测试：252 passed, 4 skipped（L3 integration）, 0 failed.**

---

## 4. 关键链路

### 4.1 UE 关闭 → 第一次工具调用（HTTP 502）

```
LLM 调工具
  → server.py 路由到 call_tool handler
  → ue_client.call_tool()
      → _ensure_connected(): _connected=True → 快速路径放行
      → _http.stream("POST", ...) → response.status_code = 502
      → self._connected = False                ← 翻旗
      → raise JsonRpcError("连接已断开（HTTP 502）...")
  → server.py except: error = e
  → return CallToolResult(isError=True, text="错误: 连接已断开（HTTP 502）...")
  → LLM 看到错误提示
```

### 4.2 UE 仍关闭 → 第二次工具调用

```
LLM 重试工具
  → ue_client.call_tool()
      → _ensure_connected(): _connected=False → 快速路径失效
      → ping(2s) → 失败 → raise RuntimeError("连接已断开。请确认 UE 编辑器...")
  → server.py except: error = e
  → return CallToolResult(isError=True, text="错误: 连接已断开。请确认...")
  → LLM 看到提示，转告用户
```

### 4.3 UE 重启 → 自动恢复

```
用户重启 UE → 告诉 LLM → LLM 重试工具
  → ue_client.call_tool()
      → _ensure_connected(): _connected=False → ping(2s) → 成功！
      → await reconnect()
          → 清理旧 _http → connect() 握手 → _connected=True
          → 遍历 _on_reconnect 钩子:
              → _rebuild_shot_session()    ← 关闭旧 shot session，创建新的
              → _refresh_cache_on_reconnect() ← L3 full_refresh
      → 原工具调用继续执行
```

### 4.4 take_screenshot 的 shot session 自愈

```
LLM 调 take_screenshot
  → capturer.capture()
      → if _shot_client is None: raise    ← 守卫（仅检查 None）
      → _shot_client.call_tool()
          → _ensure_connected(): _connected=False → ping → reconnect
          → 截图工具执行
  → 成功返回截图
```

---

## 5. 异常矩阵速查

| 信号 | `_connected` | 行为 |
|------|:---:|------|
| `httpx.ConnectError` | True→False | 翻旗，上抛原始异常 |
| `httpx.ReadTimeout` | 不动 | `_cancel_request()` + 上抛 |
| `httpx.RemoteProtocolError` | ping 失败才翻 | 二次确认 |
| HTTP 502/503/504 | True→False | 上抛中文提示 |
| HTTP 4xx/500 | 不动 | 上抛原始状态码 |
| `JsonRpcError`（业务错误） | 不动 | 上抛 |

---

## 6. 待验证（需要 UE 运行）

- [ ] UE 正常 → 关 UE → LLM 调工具 → 看到"连接已断开" → 重启 UE → LLM 重试 → 自动恢复
- [ ] shot session 独立自愈：UE 关了又开 → `take_screenshot` 自动重连并正常截图
- [ ] 主 session 重连后钩子链正确执行（shot session 重建 + L3 刷新 + 日志无异常）
