# 0007 — Grill Assessment 2026-06-20：项目必要性、实现差距与改进方向

**背景：** 在 MCP 代理链路、Skill 系统、State Cache、Vision 管线基本可运行的状态下，通过 `grill-with-docs` 对项目整体必要性、设计假设、实现差距做了一次系统性拷问。

**参与者：** 项目作者（被 grill 方）、Claude（grill 方）

---

## 一、实现状态 vs 文档承诺

| 模块 | 文档描述 | 实际代码 | 差距 |
|------|----------|----------|------|
| **MCP Server** (面向LLM) | 完整 MCP Server | ✅ `server.py` 404行，工具路由、拦截器链、Skill CRUD | 基本一致 |
| **MCP Client** (面向UE) | JSON-RPC 2.0 + SSE | ✅ `client.py` 460行，握手、SSE两阶段解析、preload_all_toolsets | 基本一致 |
| **Context Assembly** | 三层 prompt + 工具过滤 | ✅ `filter.py` + `prompt.py` + `skill_registry.py` | Skill triggers 字段未消费（定义了但代码未使用） |
| **State Cache** | Write-Through L1/L2/L3 | ✅ `models.py` + `interceptor.py` 15个handler + `refresher.py` L3 | **6个代码级问题（见第二节）** |
| **Vision** | 截图→Vision API→验证 | ✅ `capturer.py` + `vision_agent.py` + `config.py` + `.vision.env` | VisionInterceptor（#007）待完成 |
| **Observability** | 日志+回放 | ✅ `logger.py` + `replay.py` + `stats.py` | 基本完整 |
| **Task Memory** | 结构化压缩 | ❌ 仅有 `__init__.py` | **未实现** |
| **Recovery** | 错误分类+重试 | ❌ 仅有 `__init__.py` | **未实现** |
| **Safety** | 规则引擎 | ❌ 仅有 `__init__.py` | **未实现** |

---

## 二、State Cache 的6个代码级问题

### 2.1 Actor 身份仅靠名称匹配 🔴 高
`cache.actors[name]` 用字符串做 key。UE 重连后同名 Actor 不保证是同一个对象；Actor 被改名后旧缓存变 orphan。无稳定 ID 机制。
**位置：** `harness/state/models.py:48`

### 2.2 无磁盘持久化 🔴 高
WorldState 纯内存对象。Harness 进程崩溃后缓存全丢。ADR 0005 承诺了持久化但代码里没有任何序列化逻辑。
**位置：** 整体缺失

### 2.3 短名 fallback 匹配脆弱 🟡 中
`interceptor.py:44-50` 用 `endswith(short)` 匹配 handler。`Foo.get_actor_transform` 和 `Bar.set_actor_transform` 都 endswith "set_actor_transform"，会导致误匹配。
**位置：** `harness/state/interceptor.py:46-49`

### 2.4 L3 刷新不获取属性 🟡 中
`refresher.py` 只调 `find_actors(glob='*')` 拿到名字列表，创建空壳 `ActorSnapshot(name=name)`。transform 和 properties 全是空的——LLM 看到的状态快照是空洞的。
**位置：** `harness/state/refresher.py:50-53`

### 2.5 L2 读验证完全未实现 🟡 中
ADR 0004 承诺了"写后选择性重查验证生效"，但代码里无任何 L2 逻辑。
**位置：** 整体缺失

### 2.6 缓存新鲜度无超时机制 🟢 低
`last_full_refresh` 被记录但从不对照是否过期。Harness 运行数小时无 Hard Boundary 时缓存可能严重漂移。
**位置：** `harness/state/models.py:56`

---

## 三、设计决策（Grill 中确认的7个问题及建议）

| # | 问题 | 建议决策 |
|---|------|----------|
| 1 | **State Cache 一致性漂移** — 用户手动在 UE 编辑器操作不经过 MCP，缓存不知情 | 接受此限制，文档标注"缓存仅在通过 MCP 操作时准确"。加 TTL 过期（5 分钟无 MCP 活动自动标记 dirty） |
| 2 | **MCP 协议版本锁定** — 中间层两面受协议演化挤压 | README 写明"支持 UE 5.8 MCP Server + Claude Code 2025Q2+"。版本锁定不可耻 |
| 3 | **Skill triggers 字段闲置** | 优先做 A 方案：注入到 system prompt 让 LLM 自己匹配（轻量） |
| 4 | **Harness Skill vs UE Skill 重合** | 在 context assembly 里把 UE `UAgentSkill` 相关工具从 LLM 可见列表移除，只暴露 Harness Skill |
| 5 | **Vision 验证频率** — 每步 vs 仅最终 | Skill YAML 增加 `verification.frequency: every_step \| final_only`。默认 `final_only` |
| 6 | **Session 解耦的磁盘持久化** | MVP 做 `json.dump(WorldState)` 到 `~/.ue-harness/sessions/{id}.json` |
| 7 | **State Cache 性能未验证** | 加 benchmark CLI：`harness bench --compare` |

---

## 四、必要性终极判断

**项目值得做完。** 核心理由：

1. **拓扑位置是真空地带。** 当前 MCP 生态里，LLM 客户端和 MCP 服务器之间没有广泛认可的"智能中间层"。类似于 API Gateway 在微服务架构成熟前的阶段——概念合理但实践案例少。做完并开源，可能成为这个模式的参考实现。

2. **双核心功能有不可替代性。** Context Assembly（157 工具动态过滤 + Skill 上下文注入）和 Session Decoupling（三层解耦，MCP 断开不丢状态）是 Claude Code 配置层做不到的增量价值。

3. **叙事完整性对求职有价值。** 项目有：问题发现（10 条局限性）→ 架构设计（6 个 ADR）→ 分阶段实施 → 真实 Bug 修复记录。

4. **完成度比意识到的更高。** MCP 代理链路、Skill 系统、State Cache 框架、Vision 管线、可观测性——都有可运行代码。剩下是填充而非重建。

---

## 五、改进方向优先级

```
P0（MVP 收尾，1-2 周）:
  ├── State Cache 磁盘持久化（~1天）
  ├── L3 刷新补全属性获取（~1天）
  ├── VisionInterceptor 完成 #007（~1天）
  ├── Skill triggers 注入 system prompt（~半天）
  └── 修复短名 fallback 误匹配（~1小时）

P1（提升可靠性，1-2 周）:
  ├── State Cache TTL 过期机制
  ├── verification.frequency 可配置
  ├── MCP 协议版本兼容文档
  └── 基准测试 CLI（harness bench）

P2（完整产品，2+ 周）:
  ├── Task Memory 压缩
  ├── Error Recovery
  ├── Safety Guardrails
  └── 多 UE 实例支持
```

---

## 六、参考案例与可借鉴模式

### 最接近的项目
- **Anthropic/mcp-proxy** — 官方 MCP 代理参考（如果 Anthropic 还没做，他们迟早会做）
- **OpenAI/codex-remote** — Codex CLI 的远程执行代理，概念类似
- **LangChain MCP Adapter** — LangChain 的 MCP 适配器

### 可借鉴的模式
- **Envoy Proxy filter chain** — `interceptor.py` 已经是 pre→call→post 链，可参考 Envoy 做条件路由、限流
- **Kubernetes controller pattern** — State Cache（desired vs actual state reconciliation）和 K8s controller loop 同构，可借鉴 reconcile loop 的 backoff + retry
- **Playwright auto-waiting** — Vision verification 可借鉴"等待直到条件满足"语义

### 建议关注的方向
- Anthropic **Agent Skills spec** 标准化时尽快对齐 YAML 格式
- **MCP streaming 扩展**落地后，Harness 需支持 chunk 转发或缓冲

---

## 后果

- State Cache 一致性边界被明确记录为已知限制（非 bug，是设计取舍）
- MCP 协议版本兼容边界被明确化
- P0-P2 优先级排序为后续开发提供了路线图
- 项目核心叙事（Context Assembly + Session Decoupling 双支柱）被确认有效
