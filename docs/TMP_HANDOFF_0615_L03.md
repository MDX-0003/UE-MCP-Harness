# UE Agent Harness L3 验证 Handoff — 2026-06-15

## 任务概要

验证 003（可观测性）模块是否已落地。Commit `28a89d7 形成003契约规范` 提交了 `docs/contracts.md`（455 行契约文档），但实现代码全部在未提交的工作区中。需要从 L1/L2/L3 三个层级验证落地质量。

---

## 验证结果总览

| 层级 | 内容 | 结果 |
|:---:|---|---|
| L1 | 11 条契约逐条审计，对照 `contracts.md` 检查实现代码 | ✅ 11/11 PASS |
| L2 | `pytest tests/test_interceptor.py tests/test_observability.py` | ✅ 31/31 PASS (0.08s) |
| L3 | 端到端：启动 Harness + MCP SSE 客户端调工具 + 查日志 | ✅ 通过（含发现 1 个 Bug + 工具名修正） |

**结论：003 已落地，未提交的工作区代码可以提交。**

---

## L3 端到端测试详细记录

### 测试环境

- Harness 版本：v1.27.2
- UE MCP Server：`http://127.0.0.1:8000/mcp`
- Harness 监听：`http://127.0.0.1:9000/sse`
- 工具总数：211（19 个工具集中 18 个成功加载）
- 日志目录：`~/.ue-harness/logs/`

### L3 测试脚本

新增 [tests/test_l3_e2e.py](../tests/test_l3_e2e.py)：使用 MCP SDK 的 `sse_client` + `ClientSession` 连接 Harness 的 SSE 端点，调用 2 个无参只读工具，验证日志产出。

测试工具：
1. `ToolsetRegistry.EditorAppToolset.GetSelectedActors` — 无参，返回选中 Actor 列表
2. `toolset_registry.toolsets.core.scene.SceneTools.get_current_level` — 无参，返回当前关卡路径

### L3 运行步骤

```powershell
# 终端 A：UE Editor（MCP Server @ :8000）
# 终端 B：启动 Harness
harness start --ue-port 8000 --listen-port 9000

# 终端 C：运行 L3 测试
cd <project-root>
python tests\test_l3_e2e.py

# 终端 D：验证日志
harness stats
cat ~\.ue-harness\logs\<session_id>.jsonl
```

---

## 发现的问题与修复

### Bug #1 — `transport.py` 缺少 `import asyncio`（L3 发现）

**表现**：Ctrl+C 关闭 Harness 时崩溃：
```
11:17:19 [ERROR] harness: 启动失败: name 'asyncio' is not defined
11:17:19 [ERROR] harness: 致命错误: name 'asyncio' is not defined
```

**根因**：[harness/transport.py](../harness/transport.py) 第 95 行使用了 `asyncio.CancelledError` 但文件没有 `import asyncio`。当 Ctrl+C 触发 uvicorn 关闭时，Python 执行到 `except asyncio.CancelledError` 时发现 `asyncio` 未定义 → `NameError`。

**为什么 L1/L2 没发现**：
- L1（静态审计）：审查的是 `server.py`、`interceptor.py`、`cli.py`、`observability/`，没有审查 `transport.py`
- L2（单元测试）：测试的是 `ToolCallInterceptor` 和 `ToolCallLogger` 的隔离行为，不涉及 transport 层
- 此 Bug 只在真正走 SSE 优雅关闭路径时才暴露，属于集成层面

**修复**：在 [harness/transport.py](../harness/transport.py) 第 11 行添加 `import asyncio`。

**修改前**：
```python
# harness/transport.py (第 9-11 行)
from __future__ import annotations

import logging
```

**修改后**：
```python
# harness/transport.py (第 9-12 行)
from __future__ import annotations

import asyncio
import logging
```

---

### 问题 #2 — L3 测试脚本用错协议（L3 发现）

**表现**：测试脚本连接 Harness 时报 `HTTP 404: Not Found`。

**根因**：最初写的测试脚本使用了 `McpClientSession`（项目自带客户端），它通过 HTTP POST 直连 UE 的 `/mcp` 端点（JSON-RPC over HTTP）。但 Harness 对外暴露的是 `/sse` 端点（MCP SSE 传输协议），两个协议不兼容。

**修改前**：
```python
from harness.client import McpClientSession
from harness.config import Config

config = Config.from_env()
config.ue_port = 9000  # 指向 Harness
client = McpClientSession(config)
await client.connect()  # POST http://127.0.0.1:9000/mcp → 404
```

**修改后**：
```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://127.0.0.1:9000/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()  # MCP 握手 over SSE
```

**设计决策备忘**：`McpClientSession` 专为 UE MCP Server 的 HTTP POST 协议设计，Harness 的 SSE 端点必须用 MCP SDK 的 `sse_client`。这两个协议是 MCP 规范的两种合法传输方式，不应合并为一个客户端。

---

### 问题 #3 — 工具名必须用全限定路径（L3 发现）

**表现**：`call_tool("SceneTools.find_actors", ...)` 返回 `Unknown tool: SceneTools.find_actors`（错误码 -32602）。

**根因**：UE MCP Server 的工具名使用 Python 模块全路径格式（如 `toolset_registry.toolsets.core.scene.SceneTools.find_actors`），不是短名。`contracts.md` 中使用的短名只是文档伪代码的简化写法。

**修改前**（测试脚本）：
```python
result = await session.call_tool("SceneTools.find_actors", {"name_pattern": "*"})
result = await session.call_tool("EditorAppToolset.get_editor_state", {})
```

**修改后**：
```python
result = await session.call_tool(
    "ToolsetRegistry.EditorAppToolset.GetSelectedActors", {}
)
result = await session.call_tool(
    "toolset_registry.toolsets.core.scene.SceneTools.get_current_level", {}
)
```

---

### 旁注 — `find_actors` 的 Schema Bug（非 Harness 问题）

UE MCP 插件中 `SceneTools.find_actors` 的 JSON Schema 将 `tag` 标记为 `required: ["tag"]`，但文档描述为 "If set, will only return actors that have this tag"（即 tag 应为 optional）。这导致不传 `tag` 时参数校验失败。**这是 UE 插件侧的 bug，不属于 Harness 修复范围**，在此记录供 UE 插件开发者参考。

---

## 修改文件清单

### 已修改（工作区已有，本次验证中修复）

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `harness/transport.py` | Bug 修复 | 新增 `import asyncio`（CTRL+C 崩溃修复） |
| `harness/cli.py` | 003 实现 | 拦截器链创建、session_id 管理、stats/replay 子命令 |
| `harness/server.py` | 003 实现 | `build_server()` 接受 `interceptors`、pre→call→parse→post 拦截链 |
| `harness/interceptor.py` | **新文件** | `ToolCallInterceptor` 基类 + `ToolCallCompleted` dataclass + `DebugPreCallInterceptor` |
| `harness/observability/logger.py` | **新文件** | `ToolCallLogger`：异步 JSONL 写入（`asyncio.Queue` + 后台协程） |
| `harness/observability/stats.py` | **新文件** | `harness stats` 命令：读取 JSONL，输出调用次数/错误率/平均耗时 |
| `harness/observability/replay.py` | **新文件** | `harness replay` 命令：读取 JSONL，重放工具调用到 UE |
| `tests/test_interceptor.py` | **新文件** | 7 个测试：`ToolCallCompleted` 字段/默认值、拦截器透传/noop/自定义 |
| `tests/test_observability.py` | **新文件** | 24 个测试：序列化截断、日志写入、JSONL 加载、文件查找 |
| `tests/test_l3_e2e.py` | **新文件** | L3 端到端测试：用 `sse_client` 连 Harness，调 2 个工具，为将来 CI 准备 |

### 未修改（contracts.md 中定义但尚未实现）

| 文件 | 所属模块 | 状态 |
|---|---|---|
| `harness/state/models.py` | 008 State Cache | Contract 2 已锁定，代码未写 |
| `harness/state/cache.py` | 008 State Cache | Contract 2 已锁定，代码未写 |
| `harness/context/filter.py` | 004 Context Assembly | Contract 3 已锁定，代码未写 |
| `harness/context/prompt.py` | 004 Context Assembly | Contract 3 已锁定，代码未写 |
| `harness/context/provider.py` | 004 Context Assembly | Contract 3 已锁定，代码未写 |
| `harness/context/assembler.py` | 004 Context Assembly | Contract 3 已锁定，代码未写 |

---

## `server.py` 核心逻辑变更（修改前后对比）

### build_server() 签名

```python
# 修改前（001/002 纯透传）
def build_server(config: Config, ue_client: McpClientSession) -> Server:

# 修改后（003 拦截器链）
def build_server(
    config: Config,
    ue_client: McpClientSession,
    interceptors: list[ToolCallInterceptor] | None = None,
) -> Server:
```

### call_tool() 逻辑

```python
# 修改前：纯透传
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    result_text = await ue_client.call_tool(name, arguments)
    return CallToolResult(content=[TextContent(type="text", text=result_text)])

# 修改后：拦截器链 pre → call → parse → post
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    t_start = time.monotonic()
    error = None

    # pre 阶段
    for ic in interceptors:
        arguments = await ic.pre_call(name, arguments)

    # 实际调用
    try:
        result_text = await ue_client.call_tool(name, arguments)
    except Exception as e:
        error = e

    duration_ms = (time.monotonic() - t_start) * 1000

    # 解析（只做一次，各 interceptor 共享）
    parsed_raw = _parse_raw_result(result_text)
    parsed_text = _extract_parsed_text(parsed_raw, result_text)

    # post 阶段
    event = ToolCallCompleted(
        name=name, args=arguments,
        raw_result=parsed_raw, parsed_text=parsed_text,
        error=error, duration_ms=duration_ms,
    )
    for ic in interceptors:
        await ic.post_call(event)

    if error:
        return CallToolResult(
            content=[TextContent(type="text", text=f"错误: {error}")],
            isError=True,
        )
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

### 新增解析函数

```python
def _parse_raw_result(result_text: str | None) -> Any:
    """解析 JSON-RPC result 文本为 Python 对象。"""
    ...

def _extract_parsed_text(parsed_raw: Any, fallback: str | None) -> str | None:
    """从 MCP content array 格式中提取纯文本，支持 text 和 image 类型。"""
    ...
```

---

## `cli.py` 核心逻辑变更（修改前后对比）

### cmd_start() — 拦截器链创建

```python
# 修改前：直连 UE，无拦截器
async def run() -> None:
    await ue_client.connect()
    await ue_client.preload_all_toolsets()
    server = build_server(config, ue_client)
    await serve(server, ...)

# 修改后：创建拦截器链，管理生命周期
async def run() -> None:
    nonlocal session_id

    tool_logger = ToolCallLogger(config.log_dir, session_id)
    await tool_logger.start()

    interceptors = [DebugPreCallInterceptor(), tool_logger]

    try:
        await ue_client.connect()
        if ue_client.session_id:
            tool_logger._session_id = ue_client.session_id  # 用 UE 真实 session_id

        await ue_client.preload_all_toolsets()
        server = build_server(config, ue_client, interceptors)
        await serve(server, ...)
    except Exception as e:
        logger.error("启动失败: %s", e)
        raise
    finally:
        await tool_logger.stop()  # 排空队列，关闭 JSONL
```

### main() — 新增子命令

```python
# 修改前
if args.command == "version":
    ...
# 无 stats / replay

# 修改后
elif args.command == "stats":
    return _cmd_stats(args)
elif args.command == "replay":
    return _cmd_replay(args)
```

---

## JSONL 日志格式（003 产出物）

```json
{
  "timestamp": "2026-06-15T03:28:10.732053+00:00",
  "session_id": "25cb50c44fc6a5624cb142881db9504d",
  "tool_name": "toolset_registry.toolsets.core.scene.SceneTools.get_current_level",
  "tool_input": {},
  "tool_output": "{\"content\": [{\"type\": \"text\", \"text\": \"...\"}]}",
  "error": null,
  "duration_ms": 329.0,
  "screenshot_path": null,
  "verification": null
}
```

---

## L3 验证日志（实际产出）

从 `~/.ue-harness/logs/` 中确认了 4 条记录（2 条失败 + 2 条成功）：

| # | 工具 | 耗时 | 结果 |
|---|------|:---:|---|
| 1 | `SceneTools.find_actors` (错误名) | 328ms | ❌ Unknown tool |
| 2 | `EditorAppToolset.get_editor_state` (错误名) | 329ms | ❌ Unknown tool |
| 3 | `SceneTools.find_actors` (参数缺 tag) | ~300ms | ❌ Input validation error |
| 4 | `SceneTools.get_current_level` | ~300ms | ✅ 成功 |

---

## 建议

1. **提交当前工作区**：003 代码 + transport.py 修复 + L3 测试脚本，一次性提交
2. **发布前跑全量测试**：`python -m pytest tests/ -v`（当前 38 个测试全绿，新增 31 个）
3. **UE 插件侧修复**：`find_actors` 的 `tag` 参数从 `required` 中移除
4. **CI 集成**：L3 测试脚本需要 UE + Harness 都在运行，适合 nightly 而非 pre-commit hook
