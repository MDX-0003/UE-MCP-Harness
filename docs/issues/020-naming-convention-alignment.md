# 020 — 命名规整：前缀+功能 全面对齐

**类型：** AFK

**依赖关系：** 依赖 Issue 018 + 019（模块边界稳定后命名才有效）；可部分与 Issue 019 并行。

## 要构建什么

全仓公开函数/类/常量的命名按「前缀+功能」约定重命名——前缀标识归属子系统（mcp_/state_/vision_/capture_/skill_/obs_/ctx_/cli_/ref_）。跨模块符号公开化（去掉下划线私有前缀），模块内私有函数保持 `_` 开头。**MCP 工具名（LLM 可见）不在本次范围**（activate_skill / vision_* 等外部契约，单独立项）。

重点修复：`record_write` 回调注入化（切断 state↔verification 反向 import 环，同时收口漏调的 8 个 handler），`capture()` dead ue_client 参数删除，`_is_screenshot_tool` 三份语义分歧收敛，session.py 读取从未写入的 pass/reason 死块删除。

## 验收标准

### P0 — 跨模块共享、表意误导（必须改）

- [ ] `extract_short_name` → `mcp_tool_short_name`（normalize.py 保留别名），6 处克隆全部改为调用（如果 Issue 017 未做则在此做）
- [ ] `_parse_ref_path` → `state_parse_ref_path`（公开化），4 处 import 同步
- [ ] `capture` → `capture_screenshot`（+ 删 dead ue_client 参数），3 个调用点 + 测试同步
- [ ] `init_shot_session` / `close_shot_session` → `capture_init_session` / `capture_close_session`，cli.py 调用点同步
- [ ] `record_write` / `get_recent_writes` → `vision_record_write` / `vision_get_recent_writes`
- [ ] **record_write 注入化**：`StateCacheInterceptor(cache, on_write=...)` 形参（cli.py 接线 `vision_record_write`），删 `from harness.verification.session import record_write`（断 state→verification 反向 import）；post_call 成功分支单点调用 `on_write(event)`——自动覆盖全部 handler，不再依赖 7/15 个 handler 各自手动调（**修 B6**）
- [ ] `_is_screenshot_tool` ×3 → `capture_is_screenshot_tool`（一份，取 verif 版关键词匹配语义，**修 B5** logger 漏判）
- [ ] `full_refresh` → `state_full_refresh`，hard_boundary.py import 同步

### P1 — 公开但过泛

- [ ] `apply_filter` → `ctx_filter_tools`；`is_escape_hatch` / `ESCAPE_HATCH_TOOLS` → `ctx_is_always_visible_tool` / `CTX_ALWAYS_VISIBLE_TOOLS`；`assemble_system_prompt` → `ctx_assemble_prompt`
- [ ] `_pie_str` → `_format_pie_status`
- [ ] `_serialize_args` → `obs_sanitize_args`；`_timestamp` → `obs_utc_timestamp`
- [ ] `_format_output` / `_summarize_verbose_output` / `_truncate` → `obs_format_tool_output` / `obs_summarize_tool_output` / `obs_truncate_text`
- [ ] `validate_skill` → `skill_validate_yaml`；`_normalize_list` → `skill_normalize_str_list`
- [ ] `init` / `enabled` / `log_exception` (debug.py) → `vision_debug_init` / `vision_debug_enabled` / `vision_log_exception`；cli.py:131 别名同步
- [ ] `VisionSubAgent.check` → `analyze_screenshot`；`VisionSubAgent.classify` → `classify_properties`
- [ ] `build_scene_context` → `vision_build_scene_context`；`build_full_prompt_context` → `vision_build_prompt_context`；`_cap_context` → `_truncate_context_blocks`
- [ ] `_confirm_cache` → `_state_confirm_readback`
- [ ] VisionInterceptor/ReadbackInterceptor 的 `cache` 形参 → `world_state`（与 VisionSessionManager 统一）
- [ ] `cmd_start` / `_cmd_stats` / `_cmd_replay` / `_cmd_vision` / `_cmd_skill` → `cli_cmd_start` / `cli_cmd_stats` …（风格统一）
- [ ] `_setup_logging` → `cli_setup_logging`；`_verify_level_persistence_tools` → `state_verify_persistence_tools`；`EXPECTED_LEVEL_TOOLS` → `LEVEL_PERSISTENCE_EXPECTED_TOOLS`
- [ ] `_build_mimo_prompt` → `_build_classify_prompt`；`_resolve_mimo_indices` → `_resolve_classified_indices`；`_build_trend_summary` → `_render_metrics_trend`；`_render_mapping_markdown` → `_render_mapping_md`
- [ ] `_item_to_name` → `_actor_ref_name`；`_extract_property_names` → `_parse_property_names`
- [ ] `_rebuild_shot_session` → `_restore_capture_session`；`_refresh_cache_on_reconnect` → `_state_refresh_on_reconnect`；嵌套 `run` ×2 → `_run_server` / `_run_vision_check`
- [ ] `_try_file_fallback` → `_capture_via_file_fallback`；`_poll_and_capture` → `_capture_poll_screenshot_dir`；`_capture_asset_image_with_file_fallback` → `_capture_call_asset_image`
- [ ] `VisionSession.touch` → `mark_active`；`VisionSessionManager.start` → `start_session`；`VisionSessionManager.reset` → `reset_session`
- [ ] `compute_match_metrics` → `ref_compute_metrics`（低优先，模块名已是 metrics）

### 兼容与清理

- [ ] 测试文件 import 全部更新（test_verification.py 导入私有函数的点、test_skill.py `_normalize_list`、test_observability.py `_serialize_args`/`_truncate` 等）
- [ ] server.py 的 re-export shim（从 Phase 2-3 遗留）删除——测试改为从真模块 import
- [ ] 旧别名在 normalize.py / 其他模块保留 `_old = new` 即可（Python 重命名 = 删旧名 + 加新名，同 commit 更新所有调用点）

### 回归验证

- [ ] `uv run pytest tests/ -v` 全量绿
- [ ] `uv run harness start --help` 子命令名可读
- [ ] skill YAML 中的工具名不受影响（MCP 工具名未改）

## 设计说明

**为什么 MCP 工具名不改？** `activate_skill` / `vision_*` 等是 LLM 可见的外部契约——改工具名 = 所有 skill YAML 的 tools_allowlist 同步 + instructions 文本同步 + LLM 行为重测试。代价/收益不成比例，单独立项。内部 handler 函数/类改名自由（无外部耦合）。

**record_write 注入化的优势**：① 切断 state↔verification 反向 import（现有环因 Python 延迟绑定未爆，但移动任一符号即触雷）；② 收口到单点（post_call 成功分支）→ 不再依赖 15 个 handler 各自手动调 → 新增 write handler 自动覆盖；③ 保持 015 决策（buffer 仍在 verification/session.py）。

**`_is_screenshot_tool` 收敛**：取 verification 版（关键词子串 + 大小写不敏感），删除 logger 版精确名单（会漏 UE 原生的 SlateInspector.Screenshot 等），删除 snapshotter 内联特判。

## 涉及文件

- 全仓 ~40 个函数的改名 + tests 全部更新（rename_symbol 批量或手工 grep 查找替换）
- 重点文件：server.py / client.py / cli.py / state/* / verification/* / observability/* / context/*
- 测试文件：test_*.py 全量（import + assert 文本中的引用）
