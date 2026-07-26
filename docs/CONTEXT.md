# UE Agent Harness — 领域语言词汇表

## Harness 侧术语

**Harness（执行框架）：** 位于 LLM 和 UE 之间的外部 Python 进程。拥有：上下文组装、状态缓存、验证循环、记忆压缩、安全护栏、可观测性。不拥有：推理（LLM）、工具实现（UE）、编辑器状态（UE）。是整个系统的唯一入口——LLM 永远不知道 UE MCP Server 的存在。

**Harness Skill：** `~/.ue-harness/skills/` 中的 YAML 文件，定义多步任务工作流：triggers（匹配用户意图）、steps（有序指令）、tools_allowlist（允许 LLM 使用的 UE 工具）、verification（基于截图的品质标准）。任务级而非工具级。

**上下文组装（Context Assembly）：** Harness 在每个 LLM 轮次前构建 prompt 的过程。三层：System（身份 + State Cache 快照）、Task（Skill 步骤 + 记忆）、Tool Reference（延迟加载的 UE 工具文档）。

**State Cache（状态缓存）：** Harness 对 UE 编辑器世界状态的带指纹校验的观测记录（ADR 0008）。UE 编辑器是唯一权威源——WorldState 从"权威镜像"降级为带 provenance 的观测，每条快照携带时间戳与关卡指纹校验结果。采用 Write-Through 策略——通过拦截 write tool call 参数即时更新。Hard Boundary 事件时触发指纹比对：匹配则信任既有观测，失配则降级为历史记录并告知 LLM。

**Write-Through Cache（写穿透缓存）：** State Cache 的核心策略。Harness 拦截所有 tool call，对每个写操作（`set_actor_transform`、`set_properties`、`add_to_scene_*`、`remove_from_scene`）在转发给 UE 前从参数中提取变更语义，即时更新本地缓存。无需额外的轮询 MCP 调用。

**Verification Loop（验证循环）：** 观察→执行→截图→Vision 分析→调整的循环。Harness 独有能力（Claude Code CLI 无法做到）。使用独立 Vision Sub-Agent（Claude Vision / GPT-4V），不占用主 Agent 上下文。

**Vision Sub-Agent（视觉子代理）：** 独立于主 Agent 的 Vision 模型实例。拥有独立的上下文窗口和状态。接收截图 + 预期描述，返回 `{pass, reason, adjustment}`。可向主 Agent 追问额外信息。同一任务内保持状态（追踪渐变改进）。

**Task Memory / 轨迹记忆（Trajectory Memory）：** 记录任务意图、已完成/待完成步骤、错误、关键资产引用——这些是只有 Harness 知道、UE 侧不存在的信息，不因 Harness 外改动而失真（ADR 0008）。替代长任务（>20 tool call）的原始对话历史。原 Issue 009 已并入本 ADR，原磁盘持久化方案（Issue 013）已作废。

**Observability（可观测性）：** 每个 tool call 的结构化日志（request/response/duration/screenshot/verification）。支持调试回放。

**Session Decoupling（会话解耦）：** 三种独立的 Session 生命周期——MCP Connection Session（传输层，`mcp` SDK 管理）、Agent Session（任务执行状态，Harness 管理，跨 MCP 连接存活）、Conversation Session（对话状态，Harness + LLM 共管）。MCP 断开不丢失 Agent Session。

**Passthrough Mode（透传模式）：** P0 行为——Harness 无修改地转发 `tools/list` 和 `tools/call`。零增值但建立了端到端链路。

**Hard Boundary Event（硬边界事件）：** 触发 State Cache L3 全量刷新的四种事件：Harness 首次连接、`load_level()` 调用、Harness-UE 重连、LLM 显式 `cache_refresh`。

## UE 侧术语（现有代码库）

**ModelContextProtocol 插件：** UE5.8 内建 MCP Server。JSON-RPC 2.0 over HTTP POST。绑定端口 8000 的 `/mcp`（可配置）。支持 session 管理、协议版本协商、SSE event-stream 响应。源码：`Engine/Plugins/Experimental/ModelContextProtocol/`。

**ToolsetRegistry（工具注册表）：** 中央工具调度系统。`FToolsetRegistry` 持有 `TMap<FString, TSharedPtr<FToolset>>`。将 `tools/call` 路由至正确的工具集。源码：`Engine/Plugins/Experimental/ToolsetRegistry/`。

**ToolsetDefinition（`UToolsetDefinition`）：** 工具集插件的基类。标记为 `meta=(AICallable)` 的静态 UFunctions 成为 MCP 可调用工具。源码：`ToolsetRegistry/Public/ToolsetRegistry/ToolsetDefinition.h`。

**FToolset：** 运行时工具集的非 UObject C++ 接口（MCPClientToolset 使用）。方法：`ExecuteTool()`、`GetToolsetJsonSchema()`、`GetToolsetName()`。源码：`ToolsetRegistry/Public/ToolsetRegistry/Toolset.h`。

**Python 工具集：** ToolsetRegistry 的内置 Python 工具集，位于 `ToolsetRegistry/Content/Python/toolset_registry/toolsets/core/`。包含 SceneTools（世界操作）、ActorTools（Actor 变换/组件）、ObjectTools（属性读写）等 16 个模块。提供 Harness 所需的全部基础 Actor/World 操作能力。

**延迟工具加载（Deferred Tool Loading）：** UE MCP Server 的默认模式。不暴露全部 ~157 工具，而是暴露 `list_toolsets`、`describe_toolset`、`load_toolset` 三个发现工具。LLM 必须显式请求工具加载。由 CVar `ModelContextProtocol.DeferredToolLoading` 控制。

**UAgentSkill：** 生成使用特定工具集的 prompt 的 UE DataAsset。继承自 UObject，支持 `GeneratePrompt()`，通过 `TSoftClassPtr<UToolsetDefinition>` 引用工具集类。工具级文档，非任务级工作流。源码：`ToolsetRegistry/Public/ToolsetRegistry/AgentSkill.h`。

**MCPClientToolset（`FMCPClientToolset`）：** 将 UE 连接到外部 MCP Server 的 `FToolset` 子类。Harness **不使用**——Harness 直接连接到 ModelContextProtocol MCP Server。源码：`Toolsets/MCPClientToolset/`。

## 缩写

- **MCP：** Model Context Protocol。Anthropic 定义的基于 JSON-RPC 2.0 的 AI-工具通信协议。
- **SSE：** Server-Sent Events。MCP 用于长连接响应的 HTTP 流式协议。
- **JSON-RPC 2.0：** MCP 底层的 RPC 协议。每条消息包含 `jsonrpc`、`id`、`method`、`params`。
- **LLM：** 大语言模型（Claude、GPT、Gemini）。推理引擎。
- **PIE：** Play In Editor。UE 的编辑器模式游戏模拟。
- **MVP：** 最小可行产品——实施计划中的 P0-P4（约 2 周）。

## 关系

- 一个 **Harness** 连接一个 **UE MCP Server** 并服务一个 **LLM**
- 一个 **Harness Skill** 通过其 `tools_allowlist` 引用零个或多个 **UE 工具集**
- 一个 **UE 工具集** 包含一个或多个 **工具**（标记为 AICallable 的 UFunctions）
- **上下文组装** 使用 **State Cache** + **Task Memory** + **Skill** 构建 prompt
- **Verification Loop** 在活跃 **Skill** 定义了 verification 标准时，在每次 tool call 后运行
- **MCP Connection Session** 断开不影响 **Agent Session**——Harness 保持运行并等待重连
