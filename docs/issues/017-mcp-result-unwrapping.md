# 017 — MCP 结果解包收敛

**类型：** AFK

## Parent

`docs/plans/2026-07-24-harness-refactor.md`

## What to build

Harness 在收到 UE 的工具调用结果后，需要把结果从 MCP 协议格式里提取出来。这段"拆信封"的逻辑——先拆 MCP 外层（`content[0].text`），再拆 UE 工具注册表内层（`returnValue` 包装）——目前在 13 个不同位置各自手写了一份，而且其中两份完全逐行相同。

本 Issue 把这套逻辑收敛为 5 个公共函数，所有消费点统一调用。对外行为不变：任何一个工具调用的结果文本，提取后与重构前逐字一致。

## Acceptance criteria

- [ ] 任何人拿到一段 UE 工具调用结果，用它提取纯文本的时候，走的是同一条代码路径，不再有 13 份各自手写
- [ ] `mcp_extract_text`：从 MCP content 数组里取出第一段 text
- [ ] `mcp_unwrap_return_value`：从 UE 的 returnValue 包装里取出内层数据
- [ ] `mcp_tool_short_name`：从全限定工具名里取尾段（如 `ToolsetRegistry.SceneTools.find_actors` → `find_actors`）。全仓 6 处各自手写的短名提取全部改为调用同一个函数
- [ ] `state_parse_actor_names`：从 `find_actors` 的返回值解析出 Actor 名称列表。原来有两份各自独立的实现，且 fallback 行为已经漂移（一份有逗号分隔降级、另一份有 actors/result/data 键降级）——取并集
- [ ] 两份逐行相同的 returnValue 解包函数删掉一份（原本在 server.py 和 verification/interceptor.py 各有一份一模一样的代码）
- [ ] Server 内部 JSON-RPC 帧拼装和 HTTP 错误分类的样板代码收敛（原本在同一文件内重复了 3-4 处）
- [ ] `uv run pytest tests/ -v` 全量绿

## Blocked by

None — can start immediately.
