# UEMCPHarness → PiAgent 迁移：当前讨论进度存档

**日期**：2026-08-01  
**状态**：暂停 — 等待 PiAgent 架构文档完成后继续

## 当前阶段

已完成：
1. UEMCPHarness 全量代码阅读（通过 Gortex + 直接 Read）
2. PiAgent 全量代码阅读（packages/agent, coding-agent, server, protocol, client, ai, extensions）
3. 两个系统的完整对应关系梳理
4. 初步迁移思路（6 阶段计划）和 5 个待确认问题

## 初步迁移思路

核心策略：UEMCPHarness 实现为 PiAgent 的一个扩展 (Extension)，利用 PiAgent 已有的 Agent 运行时、事件系统、工具注册、Session 管理能力，只补充 UE 特有的能力。

### 两大系统对应关系

| UEMCPHarness | PiAgent 等价物 | 匹配度 |
|---|---|---|
| `ToolCallInterceptor` 链 | `ExtensionAPI.on("tool_call")` + `on("tool_result")` | 高度对应 |
| `HarnessTool` 注册表 | `ExtensionAPI.registerTool()` → `ToolDefinition` | 高度对应 |
| `VisionInterceptor.post_call` | `ExtensionAPI.on("tool_execution_end")` | 高度对应 |
| `ReadbackInterceptor` | `ExtensionAPI.on("tool_result")` after write tools | 高度对应 |
| `StateCacheInterceptor` | Extension 内部状态 + CustomEntry 持久化 | 需设计 |
| `SkillRegistry` (YAML) | `ResourceLoader.getSkills()` (.pi/skills/) | 已有 |
| `_build_instructions()` | `ExtensionAPI.on("before_agent_start")` → 改 systemPrompt | 高度对应 |
| `VisionSubAgent` (独立 API) | PiAgent 已有 `streamSimple` 多 provider | 可复用 |
| `ToolCallLogger` (JSONL) | PiAgent 已有 SessionManager 持久化 | 部分覆盖 |
| `McpClientSession` (连接 UE) | **不存在** — PiAgent 没有 MCP client | 全新需建 |
| `Hard Boundary` | **不存在** — PiAgent 无此概念 | 全新需建 |
| `Harness MCP Server (:9000)` | PiAgent 已有 `packages/server` | 可替换 |

### 分阶段迁移（草案）

1. **Phase 1** — UE MCP Client + 基础工具透传（最小可验证链路）
2. **Phase 2** — Context Assembly（工具过滤、System Prompt、Skill 注入）
3. **Phase 3** — Visual Verification（截图 → Vision 分析闭环）
4. **Phase 4** — State Cache + Hard Boundary
5. **Phase 5** — Reference Matching + Drift Detection
6. **Phase 6** — Observability、Safety Guardrails、Trajectory Memory

## 5 个待用户确认的问题

### Q1: 技术栈 — 全量 TS 重写 vs 混合架构？
- A) 全量 TS 重写（纯 Pi 扩展，零外部依赖）
- B) Python sidecar（PiAgent subprocess 调 Python 服务做 Vision 计算）
- C) 混合（核心逻辑 TS，Vision heavy lifting 保留 Python 微服务）

### Q2: UE 工具集成粒度？
- A) 每个 UE 工具独立注册为 AgentTool（LLM 可见完整 API）
- B) 统一 `call_ue_tool(name, args)` 透传
- C) 高频精选 ~30 个独立 + 其余透传 + 过滤开关

### Q3: Vision 验证的 LLM 调用方式？
- A) 独立 API 调用 — 不经过 Agent 循环，不污染对话历史
- B) 内化到 Agent 循环 — 截图后通过 `sendUserMessage` 注入
- C) 两者都支持

### Q4: MVP 优先级？
- 第一阶段跑通什么？必须哪些功能？可延后哪些？

### Q5: 讨论粒度边界？
- 深挖/不深挖/按需深挖的划分

## 下一步

等待用户完成 PiAgent 架构文档阅读理解后，继续讨论以上 5 个问题，然后编写正式迁移开发计划。
