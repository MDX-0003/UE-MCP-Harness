# 017 — MCP 协议层公共化：结果解包收敛 + 客户端帧构造

**类型：** AFK（无需人工交互，可独立完成并验证）

**依赖关系：** 无上游阻塞（纯重构，不改行为）；是 Issue 018（注册表化）的前置——先有 `mcp_*` 公共工具，handler 抽取才能直接复用。

## 要构建什么

server.py / state / verification 中散落 13+ 处的 MCP 结果双层解包（content[0].text + returnValue），和 6 处的工具短名提取，收敛为 `harness/client.py` 的公开 `mcp_*` 函数族 + `harness/state/normalize.py` 的公开 `state_parse_*` 函数——单一实现，所有消费点统一调用。

同步：client.py 内部 JSON-RPC 帧构造/错误解析/HTTP 状态分类/响应清理板的提取（同一文件内的样版重复，不含 SSE 解析器合并——SSE 收敛归 Issue 021）。

## 验收标准

### MCP 结果解包（client.py 公开 `mcp_*` 族）

- [ ] `mcp_parse_result(raw: str|Any) -> Any`：json.loads 容错（JSONDecodeError 原样返回），替代 server.py `_parse_raw_result` 与全仓 35 处防御性 json.loads 的标准入口
- [ ] `mcp_extract_text(raw) -> str | None`：MCP content[0].text 提取（含 image 标记），吸收 client.py:68 `_extract_text_from_result`、server.py:1746 `_extract_parsed_text`、hard_boundary.py:169 `_unwrap_tool_result`、verification/interceptor.py:311 `_unwrap_mcp_text`
- [ ] `mcp_unwrap_return_value(text: str) -> dict | None`：returnValue JSON 包装解包。吸收 server.py:1435 `_try_unwrap_return_value` 与 verification/interceptor.py:331 `_unwrap_return_value`（删除逐行相同的 Type-1 克隆）
- [ ] `mcp_unwrap_return_text(text: str) -> str`：returnValue → 内层字符串。吸收 server.py:1414 `_unwrap_return_value_text`
- [ ] `mcp_tool_short_name(full_name: str) -> str`：全限定工具名取尾段。吸收 normalize.extract_short_name + 5 处克隆（logger/snapshotter/replay/state.interceptor×2/verification.interceptor）。normalize.py 保留 `state_parse_ref_path = mcp_tool_short_name` 别名过渡。
- [ ] 旧函数全部删除或 thin-wrapper 指向前款；server.py:50 死导入 `_unwrap_return_value` 删除

### UE 语义解析（state/normalize.py）

- [ ] `state_parse_actor_names(result) -> list[str]`：合并 refresher.py `_parse_actor_list` 与 server.py `_extract_actor_names`，fallback 行为取**并集**（两份实现已漂移——一份有逗号分隔降级、另一份有 actors/result/data 键降级）
- [ ] `state_parse_ref_path`：将 `_parse_ref_path` 私有名公开化（已被 2 模块 4 处跨用）
- [ ] 删除上述两份旧实现，4 处跨模块 import 更新

### Client 内部帧工具（client.py 私有，本文件内样版收敛）

- [ ] `_mcp_build_request_frame(method, params, request_id=None) -> dict`：取代 call_tool / _rpc / _cancel_request 三处各自手写 `{"jsonrpc":"2.0",...}` 字典
- [ ] `_mcp_parse_error_frame(data: dict) -> JsonRpcError`：取代 call_tool / _rpc SSE / _rpc JSON / _read_sse_stream 四处各自解析 error 帧
- [ ] `_mcp_classify_http_error(status, body) -> JsonRpcError`：取代 call_tool 与 _rpc 两处 502/503/504 分类（翻 `_connected` 的逻辑统一）
- [ ] `async _safe_ac_close(response)`：取代 call_tool 四个 except 分支各写一遍的 `try: await response.aclose(); except: pass`
- [ ] `_mcp_dump_result(result)`：统一 json.dumps(ensure_ascii=False) 序列化，三处调用的 `isinstance(dict) dumps else str` 收敛

### 回归验证

- [ ] `uv run pytest tests/ -v` 全量绿（331+4）
- [ ] 特别确认：test_client / test_client_health / test_normalize / test_state / test_verification_interceptor / test_build_atmosphere_mapping
- [ ] client.py 公开 `mcp_*` 函数被 server / verification / state 正常 import（依赖方向：全方向已 import client，无新增环风险）

## 设计说明

**为什么放 client.py？** MCP 协议是该模块的生产者（它拼帧、发请求、解析 SSE），放这里所有消费方（server/verification/state）无循环依赖。另一候选 state/normalize.py（0706 先例）也可，但 client → normalize 引入 transport→state 新边，不如不动。

**`mcp_extract_text` 与 `mcp_unwrap_return_value` 的边界**：前者剥 MCP 协议层（content array），后者剥 ToolsetRegistry 层（returnValue JSON 包装）。两层分别独立可复用——如 LevelPersistenceToolset 返回值有 returnValue 包装但 caller 自己做了 content 解包时只调后者即可。

**`state_parse_actor_names` 的行为并集策略**：取两份旧实现的 fallback 集合并集（逗号分隔 + actors/result/data 键 + 逐行降级），以覆盖充分性优先——后续单 Issue 精简化。

**短名提取不搬 client.py**：`mcp_tool_short_name` 是语义规范化，不是协议操作。放 normalize.py（已是被 client 无环引用的共享模块），client.py 自身不需要短名提取。

## 涉及文件

- `harness/client.py`：+5 公开函数，+5 私有帧工具
- `harness/state/normalize.py`：+2 公开函数，公开化 `_parse_ref_path`
- `harness/server.py`：5 处旧函数删除 / thin-wrapper + 1 处死导入删除
- `harness/state/hard_boundary.py` / `refresher.py` / `interceptor.py`：import 更新
- `harness/verification/interceptor.py`：删除 Type-1 克隆（_unwrap_return_value）
- `harness/observability/logger.py` / `snapshotter.py` / `replay.py`：短名调用统一
- 测试：test_normalize / test_state / test_verification_interceptor / test_client
