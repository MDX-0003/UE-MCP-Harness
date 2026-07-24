# 018 — call_tool 注册表化：HarnessTool 分发替代 if 链

**类型：** AFK

**依赖关系：** 依赖 Issue 017（`mcp_*` 解包族可用）；被 Issue 019（match_reference 状态类化）依赖——019 会在本 Issue 铺设好的 handler 分发机制上重构 match_reference。

## 要构建什么

将 server.py `call_tool` 的 ~900 行 12 分支 if 链改造为「HarnessTool 注册表 + ToolContext 依赖注入 + handler 分模块归位」。每个 Harness 自有工具 = `HarnessTool(name, description, input_schema, handler)`——spec 与 handler 同址定义，从根上消灭 spec/handler 漂移（如 `save_skill` 的 `overwrite` 参数 handler 已支持但 inputSchema 未声明）。

server.py 瘦身为 `build_server` 装配 + `list_tools` 聚合 + `call_tool` 分发 + UE 透传（~400 行）。

## 验收标准

### harness/tools.py（新增）

- [ ] `HarnessTool` dataclass：name / description / input_schema / handler
- [ ] `ToolContext` dataclass：依赖注入容器（config, ue_client, world_state, skill_registry, skill_ref, snapshot_recorder, pending_screenshot_ref, vision_session_manager, reference_session, stop_summary, tool_logger, post_interceptors）
- [ ] `tool_ok(text) -> CallToolResult` / `tool_fail(text) -> CallToolResult`：消灭 25+ 处 `CallToolResult(content=[TextContent(...)])` 构造重复
- [ ] `LocalToolCall` 上下文管理器：`async with LocalToolCall(ctx, name, args) as call: ... call.finish(text)` —— 进入记 t0，finish 自算 duration_ms 并写 ToolCallCompleted 给 tool_logger。替代 8 处 `t0/duration_ms/_log_harness_call` 样板
- [ ] `async emit_local_event(ctx, event)`：对 post_interceptors 全链 post_call（仅 vision_screenshot 用它——VisionInterceptor/SnapshotRecorder 必须看到截图事件）
- [ ] `require_vision_manager(ctx) -> VisionSessionManager | CallToolResult`：替代 4 处相同的 `vision_session_manager is None` 守卫
- [ ] **关键**：`LocalToolCall` 只写 tool_logger；`emit_local_event` 走全链——两条通道显式区分，替代当前的 `type(ic).__name__ == "ToolCallLogger"` 字符串匹配（B13 修复）

### handler 分模块归位

- [ ] `harness/context/skill_tools.py`（新增）：activate_skill / save_skill / deactivate_skill / get_context 四个 handler；输出文本逐字保留（行为控制面契约）
- [ ] `harness/verification/vision_tools.py`（新增）：vision_screenshot / vision_ask / vision_tell / vision_reset / vision_status 五个 handler；vision_screenshot 走 `emit_local_event`，其余走 `LocalToolCall`
- [ ] match_reference 与 build_atmosphere_mapping handler **暂时留在 server.py**，加 `# TODO Issue 019` 注释——由下一 Issue 提取

### server.py 改造

- [ ] `build_server` 装配 ToolContext + 构建工具注册表（`build_tool_registry()` 从 handler 模块收集）
- [ ] `list_tools`：UE 过滤工具 + 遍历注册表生成 Harness 工具 spec（替代 ~200 行内联 `Tool(...)` 构造）
- [ ] `call_tool`：查注册表 → 命中走 handler；未命中走 `_forward_to_ue(name, arguments)`（~60 行，Contract 1 调用规范的纯净实现：pre 链→UE 调用→解析一次→post 链→parsed_text 回同步→Hard Boundary 检查→返回）
- [ ] `_ue_filtered_tools`：原 `_rebuild_tool_reference` 改名（无 rebuild 语义，是带缓存获取）
- [ ] 替 B13：删除 `type(ic).__name__ == "ToolCallLogger"` 字符串匹配 → ToolContext.tool_logger 直接引用

### 兼容策略

- [ ] server.py 保留 `_build_property_index` / `_build_mimo_prompt` / `_resolve_mimo_indices` / `_render_mapping_markdown` / `build_server` 的 re-export shim（`# noqa: F401 兼容 shim, Issue 020 删除`）——测试从 server 直接 import 这些符号
- [ ] `build_server` 签名不变（cli.py 调用点不需要改）

### 回归验证

- [ ] `uv run pytest tests/ -v` 全量绿
- [ ] test_build_atmosphere_mapping / test_stop_limit / test_context / test_skill / test_vision_session / test_verification_interceptor / test_interceptor
- [ ] LLM 可见工具列表不变（`list_tools` 输出与重构前置身完全一致）
- [ ] 自有工具输出文本逐字不变（diff 审查：每个 handler 的输出文本与旧 server.py if 分支逐行比对）

## 设计说明

**两条事件通道的区分**：`LocalToolCall`（只写日志器）与 `emit_local_event`（全链 post_call）——当前代码正是这个区分（server.py:373 字符串找 logger vs :1087-1093 for 循环全链），新设计显式化并文档化。只有 vision_screenshot 需要全链（VisionInterceptor/SnapshotRecorder 消费），其他自有工具只需日志（走全链会误触 StateCache 的 dirty_toolsets 标记）。

**handler 输出文本是行为契约**：0712 教训——match_reference 的无条件提示 "请调 build_atmosphere_mapping" 曾把 LLM 送入死循环。移动 handler 时输出文本必须逐字搬运，**不带任何"顺便改进"**。

**`_forward_to_ue` 的 pre 阶段异常语义**：当前 server.py 吞异常但 `break + error = e` 阻断调用、isError 返回——与 contracts.md 示例代码（吞异常继续）不同，但与 Issue 011 Safety 的 DENY 需求（pre_call 抛→阻断调用）一致。本 Issue 保持此语义并在代码中注释说明。

## 涉及文件

- `harness/tools.py`：新增（~200 行）
- `harness/context/skill_tools.py`：新增（~120 行）
- `harness/verification/vision_tools.py`：新增（~140 行）
- `harness/server.py`：1763 → ~400 行（list_tools 部分 ~100 行、call_tool 分发 ~120 行、UE 透传 ~60 行、装配 ~80 行、其他 ~40 行）+ shim 行
- `harness/cli.py`：ToolContext 装配替换 build_server 的 11 参闭包捕获（不影响 build_server 签名）
- 测试：test_interceptor / test_context / test_skill / test_vision_session
