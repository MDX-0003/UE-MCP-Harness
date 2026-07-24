# 021 — 周边修复：Bug 修复 + 死代码清理 + CLI/Config/Transport 整理

**类型：** AFK

**依赖关系：** 依赖 Issue 018+019+020（主要结构已定）；可部分与 020 并行（不冲突的部分）。

## 要构建什么

前面各 Issue 构造阶段发现的 bugs（B2-B9, B12-B19）修复 + 全仓死代码删除 + `harness/cli.py` 拆分 + `harness/config.py` 整理 + transport/client SSE 解析收敛。是重构的"扫尾"阶段——前面的 Issue 搬完家具，这个 Issue 打扫房间。

## 验收标准

### P0/P1 Bug 修复

- [ ] **B2**：cli.py:204 的 from-import 绑定 bug → `_last_saved_screenshot_path` 从模块可变全局改为 `obs_get_last_screenshot_path()` getter 函数；cli.py 的 lambda 改为 `lambda: obs_get_last_screenshot_path()` → ToolCallLogger 的 screenshot 字段恢复正常
- [ ] **B8**：核实 client.py `_cancel_request`(:440-463) 与 HANDOFF_0624 禁令的关系——若确认是危险的回潮（删 ActiveRequests 导致截图丢失），删除并回归 HANDOFF_0624 的禁止行为；若有意的 5s best-effort 恢复，补注释并更新 HANDOFF_0624（追加修订段说明恢复理由）
- [ ] **B3**：stats.py `_find_log_file` 改为递归 `**/tool_calls.jsonl` glob；`_print_stats` 改为读新 schema 字段（`tool`/`ms`）+ 按 session 子目录分组——`harness stats` 恢复正常可用
- [ ] **B4**：`vision_model` 默认值单源化——确认项目选用的 VLM（当前 plans 一致使用 MiMo "mimo-v2.5-pro"）→ from_env 默认值、类默认值、verification/config.py 常量三者统一一个来源；若不一致则按「项目选用 MiMo」口径修正
- [ ] **B7**：session.py:482-487 与 :854-858 两个**读取从未写入的 pass/reason 键的死代码块**——按 0707 schema 修正确认：从 `last_verdict` 取 `answer`/`confidence`/`observations`（当前 schema 实际写入的键），或删除死块
- [ ] **B9**：`parse_sse_stream` 与 `_read_sse_stream` 对多行 data 的处理行为收敛为单一累加器（合入一个 `_sse_read_tool_result` 函数）；删除 `call_tool_blocking` 死函数（全仓零调用，Issue 012 落地后流式路径为唯一路径）

### 死代码删除

- [ ] `Vector3`（state/models.py:19，定义后无任何字段引用——ActorSnapshot.transform 是 `dict` 非 `Vector3`）
- [ ] `VisionConfig`（verification/config.py:27，全仓零引用）
- [ ] `record_harness_dirty`（state/hard_boundary.py:56，全仓零调用）——**二选一**：在 StateCacheInterceptor.post_call 成功 path 接线调用（完成 dirty-diff 记账），或连同 `_harness_dirty_packages` 集合一并删除并同步 contracts.md 的相关段落
- [ ] `set_reference_image`（observability/snapshotter.py:99，全仓零生产调用方）
- [ ] server.py:50 死导入 `_unwrap_return_value`（前续 Issue 未清理则在此清）
- [ ] logger.py:24 `import re` 全文件未使用
- [ ] capturer.py kernel32 变量定义未使用、SW_SHOW 未使用
- [ ] cli.py skill create 分支 `editor` 变量计算后从未使用
- [ ] build_atmosphere_mapping Step 6 的 noop `getattr(snapshot_recorder, "_snapshot_dir")` 块（注释 `# noqa — defensive` + `pass`）
- [ ] `VisionVerdict.pass_` / `reason` / `adjustment` 三个废弃 property（**复检全仓零引用** → 确认后删除）

### 其他 P2 修复

- [ ] **B12 续**：`_build_handlers` 170 键展开与短名 fallback 冗余（两者留其一——短名 fallback 更简单覆盖更全，去掉 170 键展开）
- [ ] **B16**：skill_registry.py REQUIRED_FIELDS 补充 `tools_allowlist`（对齐 validate_skill 实际行为）；或在 validate 中去掉 tools_allowlist 非空强制并统一文档
- [ ] **B17**：skill_registry.py:273 循环变量 `field` 改名（遮蔽 `dataclasses.field`）
- [ ] `type(ic).__name__ == "ToolCallLogger"` 字符串耦合若前续 Issue 未清理则在此清
- [ ] `merge_cli_overrides` 改为 `dataclasses.fields(self)` 驱动（替代曾致事故的 19 字段手工 dict）

### CLI/Config 整理

- [ ] **cli.py 拆分**：~55 行的 instructions 巨型中文字符串从 `run()` 中移出 → `harness/context/instructions.py` 或模块级常量 `CLI_INSTRUCTIONS`（独立可编辑，不再嵌在启动逻辑里）
- [ ] `cmd_start` 337 行拆出独立装配函数：`cli_assemble_interceptors(...)`（8 拦截器装配 + 顺序文档注释）、`cli_bootstrap_config(args)` 合并 4 处重复的 "load_vision_env → from_env → _setup_logging" 三件套
- [ ] `_cmd_vision` 内联的 `_Screenshot` dataclass 删除，改用 capturer.Screenshot
- [ ] 资源清理双路径收敛（shutdown 与外层 finally 各关一次的 close_shot_session + ue_client.close 统一为 finally 清理，shutdown 仅调 loop.stop）
- [ ] skill create/update 的 os.startfile 样板统一为 `skill_open_in_editor(path)`

### Config 整理

- [ ] `config_load_dotenv_file(candidates)` 公共函数（合并 config._load_dotenv ≡ verification/config._parse_and_set 克隆），两处改为调用同一函数
- [ ] `vision_model` 默认值单源化（与 B4 同批）
- [ ] merge_cli_overrides 改 fields 驱动

### Transport 整理

- [ ] `mcp_run_uvicorn_gracefully` 合并 serve() 两分支的启动/关停重复序列
- [ ] `_set_error_log_path` 降级私有 + 修正 docstring（声称由 cli.py 调用，实际仅 serve 内部）
- [ ] `create_app` 降级私有（全仓无外部调用点）
- [ ] `_MessagesMiddleware` → `_SseLegacyMiddleware`（与 `_StreamableHttpMiddleware` 对偶性）

### 回归验证

- [ ] `uv run pytest tests/ -v` 全量绿
- [ ] `uv run harness start --help` 正常；`uv run harness stats` 正常输出（B3 修复后）
- [ ] `uv run harness replay <session>` 正常
- [ ] (可选) `uv run pytest tests/test_l3_e2e.py -v` 连 UE 验证

## 设计说明

**B8 核实优先级**：HANDOFF_0624 的核心根因链 #1 就是 `_cancel_request` 误删 UE ActiveRequests 条目——如果它被回潮，截图文件 fallback 的可靠性会有随机性回归。如果确认是有意恢复（带 5s 超时/尽最大努力标记），至少需要代码注释说明"已知风险：可能导致异步截图结果丢失，5s 内约 10% 概率，文件 fallback 兜底"。

**record_harness_dirty**：contracts.md §4 的 dirty-diff 漂移检测依赖它——如果确认"不会接入"，就明确跳过 dirty-diff 并注释说明（Harness 外改动检测仅靠指纹比对，不含包级 drift）。

**`_build_handlers` 170 键展开 vs 短名 fallback**：两种机制并存维护成本高（新增 write 工具要同时在 handler dict 里展开所有全限定名变体 + 确保短名 fallback 也不冲突）。短名 fallback 天然覆盖 Python/C++ 全限定名差异，推荐只保留短名 fallback（删除 _build_handlers 展开）。

## 涉及文件

- harness/cli.py / harness/config.py / harness/transport.py / harness/client.py
- harness/verification/session.py / capturer.py / vision_agent.py / config.py / debug.py
- harness/state/hard_boundary.py / interceptor.py / models.py
- harness/observability/logger.py / snapshotter.py / replay.py / stats.py
- harness/context/skill_registry.py / prompt.py
- harness/context/instructions.py（新增）
- 测试：test_verification.py / test_observability.py / test_config.py / test_stop_limit.py 等
