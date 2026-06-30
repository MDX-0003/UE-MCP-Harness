# UE Agent Harness

MCP 中间层，连接 LLM (Claude/GPT) 与 Unreal Engine 5.8 编辑器。对外是 MCP Server（LLM 连接入口），对内是 MCP Client（连接 UE MCP Server）。提供上下文组装、状态缓存、视觉验证闭环、任务记忆压缩、安全护栏。

## 当前状态 (2026-06-30)

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
| 009 | 任务记忆 (压缩 + 注入) | ❌ 待开发 |
| 010 | 错误恢复 | ⬜ 跳过 |
| 011 | 安全护栏 | ❌ 待开发 |

**全量测试：223 unit tests (137→223) + L3 e2e 7/7 passed**

## 架构

```
LLM (Claude Code) ←→ Harness MCP Server (:9000) ←→ UE MCP Server (:8000)
                        │
              Context Assembler (三层 prompt)
              Interceptor Chain (DebugPreCall → Logger → StateCache → [Vision])
              State Cache (L1 write-through / L3 hard-boundary refresh)
              Skill Registry (YAML-based, ~/.ue-harness/skills/)
```

**核心模式：**
- **Interceptor 链**: `ToolCallInterceptor` 基类 (`harness/interceptor.py:41`)，子类覆盖 `pre_call`/`post_call`。拦截器间独立，post_call 不能改结果，post_call 异常不阻断主链路。
- **三层 Session 解耦**: MCP Connection（传输层）≠ Agent Session（任务状态，可跨连接存活）≠ Conversation Session（对话轮次）。
- **Contract 1**: `ToolCallCompleted` dataclass (`harness/interceptor.py:23`) — raw_result (JSON-RPC 原始) vs parsed_text (提取 content[0].text)。

## 命令

```bash
# 安装
pip install -e ".[dev]"

# 启动 Harness
harness start --ue-port 8000 --listen-port 9000

# 运行全部测试
pytest tests/ -v

# 运行单个模块测试
pytest tests/test_verification.py -v

# L3 端到端测试（需要 UE 运行中）
pytest tests/test_l3_e2e.py -v

# 可观测性
harness stats
harness replay <session_id>
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
tests/                  # 9 个测试文件, 137 tests
docs/
├── architecture.md     # 完整架构文档
├── CONTEXT.md          # 领域语言词汇表
├── contracts.md        # 接口契约
├── adr/                # 架构决策记录 (6 个)
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
