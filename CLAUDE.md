# UE Agent Harness

MCP 中间层，连接 LLM (Claude/GPT) 与 Unreal Engine 5.8 编辑器。对外是 MCP Server（LLM 连接入口），对内是 MCP Client（连接 UE MCP Server）。提供上下文组装、状态缓存（带指纹校验的观测记录）、验证闭环（L2 读回 + Vision）、轨迹记忆、安全护栏。

## 当前状态 (2026-07-07)

| Issue | 模块 | 状态 |
|:---:|------|:---:|
| 001 | Harness 骨架 (CLI/Config/Server/Transport) | ✅ |
| 002 | 工具发现与透传 (MCP 握手 + SSE + 211 工具) | ✅ |
| 003 | 可观测性 (JSONL 日志 + stats + replay) | ✅ 23 tests |
| 004 | Context Assembly (工具过滤 + 三层 Provider) | ✅ 21 tests |
| 005 | Skill 系统 (CRUD + match + activate) | ✅ 31 tests |
| 006 | Vision Pipeline (capturer + vision_agent) | ✅ 已接入 MCP 循环 |
| 007 | 验证闭环 (VisionInterceptor → MCP 循环) | ✅ 三种截图 mode 成立，file fallback 就绪 |
| 008 | State Cache (L1 write-through + L3 refresh) | ✅ 18 tests |
| 009 | 任务记忆 | ⬜ 作废，重定义为轨迹记忆并入 ADR 0008 |
| 010 | 错误恢复 | ⬜ 跳过（已删除） | — |
| 011 | 安全护栏 | ❌ 待开发 |
| 012 | 连接健康检测 (ping + 自动重连) | ✅ 23 tests | docs/handoff/HANDOFF_0701_ISSUE_012_CONNECTION_HEALTH.md |
| 013 | State Cache 磁盘持久化 | ⬜ 作废 (ADR 0008，文件已删) | — |
| 014 | 闭环验收场景 demo | ⬜ 降级 (2026-07-07)，已删除 | — |
| 015 | Vision 定向提问 (Session 化 vision_ask) | ✅ | docs/issues/015-vision-targeted-questioning.md |
| 016 | L2 读回拦截器 + 参考图对比 | ❌ **当前里程碑** | docs/issues/016-readback-and-reference-image.md |
| 017 | MCP 协议层公共化：结果解包 + 帧构造 | ❌ | docs/issues/017-mcp-result-unwrapping.md |
| 018 | call_tool 注册表化：HarnessTool 分发替代 if 链 | ❌ | docs/issues/018-call-tool-registry.md |
| 019 | match_reference 状态类化 + atmosphere 提取 | ❌ | docs/issues/019-reference-session-extract.md |
| 020 | 命名规整：前缀+功能全面对齐 | ❌ | docs/issues/020-naming-convention-alignment.md |
| 021 | 周边修复：Bug 修复 + 死代码清理 + CLI/Config | ❌ | docs/issues/021-bug-fixes-and-cleanup.md |
| 022 | 文档同步：架构/契约/词汇表回标 | ❌ | docs/issues/022-documentation-sync.md |
| — | LevelPersistenceToolset (fingerprint/dirty/save 五工具) | ✅ 直连验证通过 | UE 侧插件, `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/`, 详见 docs/contracts.md §4 |

**全量测试：331 passed + 4 skipped（2026-07-24，重构前基线）**

**当前方向（2026-07-07 更新）**：项目第一定位是求职/作品集叙事，重心转向参考图机制——用参考图让 Vision 回答“参考图与现状的区别”，由主 LLM 拆解为可执行步骤，替代口头表述的不确定性（Issue 016 Part B）。其前置是 L2 读回验证（ReadbackInterceptor，写后自动读回 diff，ADR 0008 定义的正确性主通道，Issue 016 Part A）。原 Issue 014 demo 降级为素材性工作，若将来录制，数字对比基准为有/无 Harness 对照。UE 是世界状态唯一权威，WorldState 为带指纹校验的观测记录（ADR 0008）。UE 侧配套插件 LevelPersistenceToolset（fingerprint/dirty/save 五工具）位于 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset`，已直连验证通过。

## 架构

```
LLM (Claude Code) ←→ Harness MCP Server (:9000) ←→ UE MCP Server (:8000)
                        │
              Context Assembler (三层 prompt)
              Interceptor Chain (DebugPreCall → Readback → Logger → StateCache → DriftAlert → Vision → SnapshotRecorder)
              State Cache (L1 write-through / L3 hard-boundary refresh)
              Skill Registry (YAML-based, ~/.ue-harness/skills/)
```

**核心模式：**
- **Interceptor 链**: `ToolCallInterceptor` 基类 (`harness/interceptor.py:41`)，子类覆盖 `pre_call`/`post_call`。拦截器间独立，post_call 不能改结果，post_call 异常不阻断主链路。
- **三层 Session 解耦**: MCP Connection（传输层）≠ Agent Session（任务状态，可跨连接存活）≠ Conversation Session（对话轮次）。
- **Contract 1**: `ToolCallCompleted` dataclass (`harness/interceptor.py:23`) — raw_result (JSON-RPC 原始) vs parsed_text (提取 content[0].text)。

## 命令

```bash
# 安装（使用 uv）
uv sync

# 启动 Harness
uv run harness start --ue-port 8000 --listen-port 9000

# 运行全部测试
uv run pytest tests/ -v

# 运行单个模块测试
uv run pytest tests/test_verification.py -v

# L3 端到端测试（需要 UE 运行中）
uv run pytest tests/test_l3_e2e.py -v

# 可观测性
uv run harness stats
uv run harness replay <session_id>
```

## 技术栈与约定

- **语言**: Python 3.12+, async/await 全异步
- **数据模型**: `pydantic` (state/models.py) + `dataclasses` (interceptor.py, config.py)
- **MCP Server 侧**: `mcp` Python SDK
- **MCP Client 侧**: `httpx` + 手写 JSON-RPC 2.0 + SSE 双阶段解析
- **日志**: `structlog` (observability), `logging` (其他模块)
- **测试**: pytest + pytest-asyncio + pytest-mock
- **配置**: `dataclass Config` (`harness/config.py:14`) + `.env` 文件 + CLI 覆盖

**代码风格：**
- 所有文件用 `from __future__ import annotations` 开头
- docstring 写在文件头，说明模块职责和涉及的 Issue 编号
- 拦截器只覆盖需要的方法，不实现空方法
- 测试文件命名: `test_<module>.py`，一个测试文件对应一个 harness 子模块

## 目录

```
harness/
├── cli.py              # CLI 入口 + 拦截器链注册
├── server.py           # MCP Server（面向 LLM）
├── client.py           # MCP Client（面向 UE, SSE 解析）
├── config.py           # Config dataclass
├── interceptor.py      # ToolCallInterceptor 基类 + ToolCallCompleted
├── context/
│   ├── filter.py       # 工具过滤
│   ├── prompt.py       # 三层 Prompt 组装
│   ├── provider.py     # Context Provider 接口
│   └── skill_registry.py  # Skill CRUD
├── verification/
│   ├── capturer.py     # 截图获取
│   ├── vision_agent.py # Vision Sub-Agent (独立 LLM API)
│   └── config.py       # Vision 配置 (.vision.env)
├── state/
│   ├── models.py       # WorldState pydantic 模型
│   ├── interceptor.py  # StateCacheInterceptor (post_call 更新缓存)
│   └── refresher.py    # L3 全量刷新
├── memory/             # 009 任务记忆（待实现）
├── observability/
│   ├── logger.py       # JSONL ToolCallLogger
│   ├── replay.py       # 回放引擎
│   └── stats.py        # 统计面板
├── safety/             # 011 安全护栏（待实现）
└── recovery/           # 010 错误恢复（跳过）
skills/                 # 内置 Skill 示例
tests/                  # 12 个测试文件, 252 tests
docs/
├── architecture.md     # 完整架构文档
├── CONTEXT.md          # 领域语言词汇表
├── contracts.md        # 接口契约
├── adr/                # 架构决策记录 (8 个)
├── issues/             # 开发 Issue
└── MVP_DEV_PLAN_0616.md  # 当前开发计划
```

## Agent Skills

开发本仓库时，以下技能应高频使用：

| 技能 | 触发场景 |
|------|---------|
| `codegen` | **所有代码变更**。遵循项目约定：dataclass/pydantic 模型、Interceptor 模式、async/await、复用现有类型。最小 diff，不加文件除非现有模块无法承载。 |
| `neat-freak` | **每个 Phase 完成后**。同步 docs/ + memory，确保 architecture.md、CONTEXT.md、contracts.md 和 handoff docs 反映最新代码。 |
| `complex-runtime-chain-notes` | **调试 MCP 协议链、拦截器链、SSE 解析**。结构化追踪 "哪个阶段进入/哪个数据变形/哪个边界跨过"。 |
| `grill-with-docs` | **跨模块设计决策前**。对照 CONTEXT.md 词汇表和 adr/ 决策记录，验证新设计与已有架构约束一致。 |
| `to-issues` | **将 Phase 计划转为独立 Issue**。当前 MVP_DEV_PLAN_0616.md 的 Phase 2-5 适合转为可追踪的 Issue。 |
| `code-review` | **Phase 完成后的 diff 审查**。检查拦截器链顺序、pydantic 模型变更、async 正确性。 |
| `diagnose` | **Bug 或静默失败**（Vision 结果未出现、SSE 帧丢失、缓存不一致）。遵循复现→最小化→假设→打点→修复纪律。 |
| `code-to-article` | **向外部解释 Harness 机制时**。比如向 UE 侧同事解释验证闭环，或用"先说做什么再给名字"原则写模块文档。 |

## 开发红线

- **不加文件优先**: 新增功能优先扩展现有模块（如加一个新的 Interceptor 子类到 `harness/verification/`），只有明确架构边界才新建文件/目录。
- **拦截器独立**: 每个 Interceptor 不读其他 Interceptor 的内部状态。post_call 不能改变 tool call 结果，异常不阻断主链路。
- **不引入新依赖**: 依赖列表在 `pyproject.toml:11-21`，仅当新功能确实需要且现有依赖无法覆盖时才加。
- **测试先行**: 每个新模块（如 `verification/interceptor.py`）配对应测试文件（`tests/test_verification_interceptor.py`）。
- **Contract 1 不破坏**: `ToolCallCompleted` 和 `ToolCallInterceptor` 的接口签名已稳定，不要改。
- **文档受众不混**: `docs/` 的读者是外部人（UE 同事、下游开发者）；`CLAUDE.md` 的读者是下次会话的 AI。别在 docs 里写"我记得上次"，别在 CLAUDE.md 里抄 docs 全文。
- **文档禁用绝对路径**: 项目在多台设备上分别开发，所有文档必须使用相对路径。Harness 内部文件相对于 `UE-MCP-Harness` 根目录；UE Engine 源码/插件相对于 UE Engine 安装目录下的 `Engine/` 目录；UE 项目文件相对于项目根目录（记为 `{UE_PROJECT_ROOT}`）。接手项目时自行确认或询问本机路径。详见 memory [[no-absolute-paths-in-docs]]。
- **项目记忆存放在 `.claude/memory/`**: 所有 Agent memory 必须写入项目根目录下的 `.claude/memory/`，不能写到 C 盘用户目录（`~/.claude/projects/...`）。此规则已在 [[config-and-paths]] 中记录。

<!-- gortex:communities:start -->
## Codebase Overview (generated by Gortex)

- **Languages:** python (primary), , contract, dotenv, gitignore, go, image, json, markdown, mcp_config, pdf, text, toml, yaml
- **Entry points:** `branch_mark\score.py`, `harness\cli.py`, `tests\test_l3_e2e.py`, `tests\tool_probe_ue.py`, `tests\tool_verify_asset_screenshot.py`, `tests\tool_verify_harness_passthrough.py`, `tests\tool_verify_harness_vision.py`, `tests\tool_verify_level_persistence.py`, `tests\tool_verify_ue_vision.py`
- **Most-referenced symbols:** `Config` (50 usages), `harness.config.Config` (49 usages), `harness.interceptor.ToolCallCompleted` (46 usages), `ToolCallCompleted` (46 usages), `loads` (42 usages), `object` (34 usages), `WorldState` (31 usages), `harness.state.models.WorldState` (31 usages), `harness.state.normalize.normalize_tool_args` (30 usages), `mcp.types.TextContent` (26 usages)
- **Graph size:** 4176 nodes, 11091 edges
- **Breakdown:** 13 contracts, 511 docs, 232 files, 373 functions, 37 images, 335 imports, 421 methods, 267 modules, 546 params, 4 resources, 1 strings, 136 types, 1300 variables

## MANDATORY: Use Gortex MCP tools instead of Read/Grep/Glob

Gortex is running as an MCP server. You **MUST** prefer graph queries over file reads on every task in this repo — `search_symbols`, `find_usages`, `get_symbol_source`, `get_editing_context`, `smart_context`, `edit_symbol` / `edit_file` / `rename_symbol` / `batch_edit`. PreToolUse hooks deny `Read` / `Grep` / `Glob` against indexed source; the deny message names the right tool. The full per-tool catalog loads via `tools/list` — not restated here.

### Calibration: the graph narrows scope, source confirms behavior

The mandate above stands — but graph queries *narrow scope*, they do not *replace reading the implementation*. The graph tells you **where** the logic lives and **what** connects to it; the source tells you **how** it behaves. For the symbol you are about to change or depend on, read its full body with `get_symbol_source` — do not act on a one-line summary alone.

Be especially deliberate with **behavior-critical code** — database migrations, retry / fallback / error-recovery paths, compatibility shims, concurrency-sensitive sections, and the tests that pin them. For these, call `get_symbol_source` and read the real implementation; never pass `compress_bodies:true`, which elides exactly the branches that carry the risk. Reserve compressed bodies and graph summaries for breadth (surveying many symbols); use full source for the few you are about to commit to.

## Required workflow (every task on this repo)

These are not suggestions — run each step at the trigger.

1. Confirm the daemon is up with `index_health` (cheap liveness + scope). Call `graph_stats` only when you actually need node/edge counts or `per_repo` orientation — it returns a large payload and can block during warmup.
2. If `total_nodes` is 0, **call** `index_repository` with `"."` before anything else.
3. In multi-repo mode, **call** `get_active_project` to check scope; use `set_active_project` to switch.
4. Open a non-trivial task with `smart_context` for orientation. For a single known symbol or file, go straight to `search_symbols` / `get_symbol_source` — don't front-load `smart_context` before every read.
5. Before editing a file, **call** `get_editing_context` on it first.
6. Before changing any function signature, **call** `verify_change` to catch broken callers and interface implementors (cross-repo).
7. For any refactor, **call** `get_edit_plan` then `batch_edit` to apply atomically.
8. Verify with the project's real build/test. Reserve `check_guards` for guard-relevant changes and `get_test_targets` to find the tests covering a substantive change — not mechanically after every edit.

<!-- gortex:skills:start -->
## Community Skills

| Area | Description | Skill |
|------|-------------|-------|
| 2 Dirs Post Call | 105 symbols | `/gortex-2-dirs-post-call` |
| 4 Dirs Harness State Models Worldstate | 93 symbols | `/gortex-4-dirs-harness-state-models-worldstate` |
| 3 Dirs Skillregistry | 83 symbols | `/gortex-3-dirs-skillregistry` |
| 2 Dirs Harness State Normalize Normali | 73 symbols | `/gortex-2-dirs-harness-state-normalize-normali` |
| 4 Dirs Harness Config Config | 71 symbols | `/gortex-4-dirs-harness-config-config` |
| 3 Dirs Build Server | 70 symbols | `/gortex-3-dirs-build-server` |
| 2 Dirs Post Call External Call Dep Harness Observability Snapshotter | 56 symbols | `/gortex-2-dirs-post-call-external-call-dep-harness-observability-snapshotter` |
| 3 Dirs Rpc | 55 symbols | `/gortex-3-dirs-rpc` |
| 2 Dirs Check | 53 symbols | `/gortex-2-dirs-check` |
| 3 Dirs Now | 41 symbols | `/gortex-3-dirs-now` |
| 3 Dirs Parse Screenshot | 39 symbols | `/gortex-3-dirs-parse-screenshot` |
| 4 Dirs Loads | 34 symbols | `/gortex-4-dirs-loads` |
| 4 Dirs Stop | 33 symbols | `/gortex-4-dirs-stop` |
| 2 Dirs Capture | 31 symbols | `/gortex-2-dirs-capture` |
| Tests 2 Dirs | 31 symbols | `/gortex-tests-2-dirs` |
| 1 Dirs Harness Verification Capturer C | 31 symbols | `/gortex-1-dirs-harness-verification-capturer-c` |
| 3 Dirs Cmd Start | 28 symbols | `/gortex-3-dirs-cmd-start` |
| 2 Dirs Main | 25 symbols | `/gortex-2-dirs-main` |
| Harness Observability 1 Dirs | 24 symbols | `/gortex-harness-observability-1-dirs` |
| 1 Dirs Score Jsonl | 24 symbols | `/gortex-1-dirs-score-jsonl` |
<!-- gortex:skills:end -->

<!-- gortex:communities:end -->

