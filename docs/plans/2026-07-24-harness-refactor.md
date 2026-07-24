# Harness 重构方案：call_tool 分发、公共层、命名规整

> 2026-07-24 立项。基于全仓文档（architecture/CONTEXT/contracts/8 份 ADR/Issues/Plans/Handoffs）与全部核心源码的完整阅读。
> 三大目标：① call_tool 转发逻辑的 if 堆叠 → 注册表分发；② 重复代码 → 公共工具函数/模块；③ 难读命名 → 「前缀+功能」规整。

---

## 1. 现状诊断

### 1.1 量化病灶

| 病灶 | 位置 | 规模 |
|---|---|---|
| god module | harness/server.py | 1763 行 / 78KB，`build_server` 单个闭包工厂 ~1220 行 |
| if 链 | server.py `call_tool` | ~900 行、12 个 `if name == "..."` 分支；`match_reference` 单分支 ~330 行（截图/指标/MiMo/倒计时/快照/趋势/输出组装 7 个关注点内联），`build_atmosphere_mapping` ~180 行 |
| MCP 结果双层解包 | 全仓 13+ 处 | content[0].text 提取 8 处、returnValue 解包 10 处，其中 `_try_unwrap_return_value`（server.py:1435）与 `_unwrap_return_value`（verification/interceptor.py:331）**逐行相同** |
| handler 样板 | server.py | `t0/duration_ms` 计时 + `_log_harness_call` ×8；`vision_session_manager is None` 守卫 ×4；`CallToolResult(...)` 构造 ×25；emoji badge 映射 ×3 |
| 小克隆族 | 全仓 | 工具短名提取 ×6、`_is_screenshot_tool` ×3（**语义互相分歧**）、`_load_jsonl` ×2、dotenv 解析 ×2、`_safe_filename` ×2、UTC 时间戳 ×4（两种格式混用）、state handler 骨架 ×17 |
| 裸 dict 状态袋 | server.py `_session_reference` | 14 个魔法字符串键散落读写，stop_limit.py 按同批键名二次读取；0714 计划文档自己已指明「未来应收敛为显式会话状态对象」 |
| 跨模块私有 import | 4 处 | server.py 从 vision_agent 导入 `_call_vision_api`/`_extract_json_object`、从 verification.interceptor 导入 `_unwrap_return_value`（**且是死导入**）；cli.py 从 snapshotter 导入私有全局 `_last_saved_screenshot_path` |

### 1.2 调查中发现的 Bug（随重构同批修复，详见 §7）

- **P0** server.py 连续下降检测分支 `lines.append(...)`：`lines` 在该路径尚未赋值，`UnboundLocalError`（应为 `body_lines`）。
- **P0** cli.py:204 `from ... import _last_saved_screenshot_path as _ss_path_ref`：from-import 绑定的是当时的值 `None`，lambda 闭包永远返回 None → ToolCallLogger 的 screenshot 字段恒为 None。
- **P0 待人工核实** client.py `_cancel_request`（:440-463）仍存在且在 ReadTimeout 路径被调用——HANDOFF_0624 根因 1 记录它已**移除**（notifications/cancelled 会删 UE ActiveRequests 条目导致异步截图结果静默丢弃）。需确认是回潮还是有意以 5s best-effort 语义恢复。
- **P1** stats.py 读旧 schema（`tool_name`/`duration_ms`）而 logger 写新 schema（`ts`/`tool`/`ms`），且 `_find_log_file` 只 glob 顶层不递归 session 子目录 → `harness stats` 对现行日志实际不可用。
- **P1** `vision_model` 默认值分裂：Config 类默认 `"mimo-v2.5-pro"` vs from_env 默认 `"claude-sonnet-4-6"` vs verification/config.py 常量——HANDOFF_0621 修过的 sse_read_timeout 同类问题重演。
- **P1** `_is_screenshot_tool` 三份语义分歧：logger 版精确名单（大小写敏感，漏 UE 原生截图工具）vs verification 版关键词子串 vs snapshotter 特判——同一工具在不同拦截器里识别结果不同。
- **P1** `record_write` 仅 7/15 个 write handler 调用，组件/文件夹操作对 Vision context 完全不可见（疑似遗漏）。
- **P1** session.py:482-487 与 :854-858 读取 `last_verdict["pass"]/["reason"]`，但 verdict_dict 从未写入这两键（PLAN_0707 结构化输出移除 pass 字段后读取点未同步）→ P3 上下文块与「上次结论」**静默恒空**。
- **P2** `parse_sse_stream` 与 `_read_sse_stream` 对多行 data 处理行为分歧（一个覆盖、一个累积）。
- **P2** `save_skill` 的 `overwrite` 参数 handler 支持但 inputSchema 未声明——工具 spec 与 handler 分离已久，已开始漂移。
- **死代码**：`Vector3`（models.py）、`VisionConfig`（verification/config.py）、`call_tool_blocking`（client.py，全仓零调用）、`record_harness_dirty`（hard_boundary.py，零调用）、`set_reference_image`（snapshotter.py）、server.py:50 死导入、logger.py `import re`、cli.py editor 死变量、build_atmosphere_mapping Step 6 的 noop getattr 块、capturer.py 未使用的 kernel32/SW_SHOW。
- **耦合隐患**：server.py:373 用 `type(ic).__name__ == "ToolCallLogger"` 字符串匹配找日志拦截器——重命名即静默失效；`capture()` 的 `ue_client` 参数从未被函数体使用（实际用模块全局 `_shot_client`），签名误导全部 3 个调用点。
- **文档-代码漂移**：拦截器链顺序在 016 文档（Readback 挂 StateCache 后）与 cli.py 实际（Readback 在 Logger 前、StopLimit 在链中）不一致；contracts.md 的 pre_call 示例代码（吞异常继续）落后于实现（吞异常但阻断调用）。

### 1.3 根因

多轮迭代（截图链 0615→0630 五轮修补、参考图 0708→0714 四轮叠加）都以「定位 server.py 第 N 行块打补丁」形式落地——server.py 成了补丁磁铁。每轮修补加一个分支、加一组魔法键，没有归位机制。本方案的本质是**把沉淀下来的领域边界显式化**。

---

## 2. 约束与红线（方案的硬边界）

以下来自 contracts.md / ADR / Issue 文档，重构不得违反：

1. **Contract 1 冻结**：`ToolCallCompleted` 六字段与 `ToolCallInterceptor.pre_call/post_call` 签名不可改（harness/interceptor.py:23,41）。post_call 不改结果、异常不阻断主链路、拦截器互不读内部状态。
2. **Contract 2/3/4 冻结**：WorldState/ActorSnapshot 字段集、ContextProvider ABC（tier/priority/enabled 类属性 + render 签名）、LevelPersistenceToolset 五工具签名与 FingerprintJSON schema。
3. **LLM 可见面是外部契约**：MCP 工具名（activate_skill、vision_*、match_reference 等）与工具的**输出文本**都是行为控制面（0712 教训：输出文本里的引导语直接驱动 LLM 行为，曾造成死循环/误创建 Actor）。本重构**不改任何工具名、inputSchema 语义与输出文本**——抽取 handler 时文本逐字搬运。
4. **skill 与代码并行演化**：skills/match-atmosphere.yaml 等 allowlist/steps 与 server.py 工具注册是同一契约的两面。若未来要改工具名，必须同批改 skill YAML——本次不改名（见 §9 不做的事）。
5. **拦截器链顺序是架构契约**：以 cli.py 实际代码为准（见 §6 Phase 6 钉死）。011 Safety 未来经 pre_call 抛异常入链（预留 DebugPreCallInterceptor 位），016 Readback 已在链。
6. **传输层不变量**（002/0621/0701）：SSE 必须增量消费；URL 收拢不追加路径；异常翻旗矩阵（ConnectError 翻、ReadTimeout 不翻、RemoteProtocolError 先 ping）；不重连轮询；重连钩子只在 cli.py 接线。
7. **0714 叫停机制公式**：总上限 `max(10, countdown_start_round + 3)`；倒计时激活当轮立即 -1；换参考图清空倒计时；硬终止检查必须先于截图/MiMo 调用；SaveLevelAs 仅 hist 首次 ≥0.70 保存一次。
8. **0713 信任层级**：量化指标与 MiMo 定性冲突时以量化为准；match_reference 是唯一双图对比工具；vision_compare 已删除不得复活。
9. **015 决策**：Recent Writes Buffer 的 deque 放 verification/session.py，StateCacheInterceptor 只写、VisionSessionManager 只读；build_scene_context 保持纯函数（无 ue_client），惰性 class 查询走 resolver 回调注入。
10. **016 Part B 有 4 个待定项**（入口形态/参考图入口/differences schema/循环终止）——重构保持这些维度可插拔，不提前固化。
11. **占位目录**：harness/memory/、recovery/、safety/ 是 009/010/011 的状态占位，不删除也不提前填充。
12. 工程红线：不引入新依赖；`from __future__ import annotations` 开头；文件头 docstring 写明职责与 Issue 编号；测试 `test_<module>.py` 一一对应；文档禁用绝对路径。

---

## 3. 方案总览

### 3.1 三个核心决策

**决策 A：call_tool if 链 → 「HarnessTool 注册表 + ToolContext」分发。**
每个 Harness 自有工具是一个 `HarnessTool` 数据对象（name + description + input_schema + handler），spec 与 handler **同址定义**，从根上消灭 §1.2 的 spec/handler 漂移。`call_tool` 变为：查注册表 → 命中走 handler；未命中走 UE 透传（Contract 1 的 pre/post 链不变）。

**决策 B：按「协议层 / UE 语义层 / 子系统层」三级归位公共函数，不是新建一个大杂烩 utils。**
MCP 协议解包归 client.py（协议生产者，无循环依赖）；UE 结果语义归 state/normalize.py（0706 先例：共享语义的家）；子系统样板归各子系统模块。

**决策 C：match_reference 的裸 dict 状态 → `ReferenceImageSession` dataclass。**
14 个魔法键变为显式字段，0714 叫停公式变为方法；stop_limit.py 的「假拦截器」归位为该模块的摘要函数。这正是 0714 计划文档自己指明的方向。

### 3.2 目标模块地图

```
harness/
├── interceptor.py            # 不动（Contract 1）
├── tools.py                  # 【新增】HarnessTool / ToolContext / tool_ok/tool_fail / 本地调用日志
├── server.py                 # 1763 → ~400 行：build_server 装配 + list_tools/call_tool 分发骨架 + UE 透传
├── client.py                 # + mcp_* 公共解包族；SSE 解析收敛；删死代码
├── config.py                 # dotenv 公共化；默认值修复；fields 驱动 merge_cli_overrides
├── stop_limit.py             # 【删除】并入 verification/reference.py
├── context/
│   ├── skill_tools.py        # 【新增】activate/save/deactivate_skill + get_context 四个 handler
│   └── (filter/prompt/skill_registry 命名规整)
├── verification/
│   ├── vision_tools.py       # 【新增】vision_screenshot/ask/tell/reset/status 五个 handler
│   ├── reference.py          # 【新增】ReferenceImageSession + match_reference handler + 趋势/指标渲染 + 叫停摘要
│   ├── atmosphere.py         # 【新增】build_atmosphere_mapping + 属性索引/MiMo prompt/Markdown 渲染
│   └── (capturer/session/interceptor/vision_agent/metrics 内部抽取与改名)
├── state/
│   └── (normalize.py 公共化解包/解析；interceptor.py handler 样板抽取 + record_write 注入化)
└── observability/
    └── (obs_load_jsonl/obs_utc_timestamp/截图判定统一/stats 修复)
```

新增 5 个文件、删除 1 个文件。立项依据：server.py 拆分是本次重构的明确目标（用户直接要求），metrics.py（0708，纯计算无承载点）是既有先例；每个新文件对应一个已有文档背书的子系统边界（005 Skill / 015 Vision Session / 016 参考图 / 0708 atmosphere mapping）。

---

## 4. Part A：call_tool 注册表分发（详细设计）

### 4.1 协议对象（harness/tools.py，新增）

```python
"""Harness 自有工具协议 — call_tool 注册表分发的基础设施。

涉及的 Issue：002（透传边界）、005（Skill）、015（Vision Session）、016（参考图）。
"""

@dataclass
class HarnessTool:
    """一个 Harness 自有工具：spec 与 handler 同址，杜绝漂移。"""
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[CallToolResult]]

@dataclass
class ToolContext:
    """handler 的依赖注入容器 — 替代 build_server 的 11 参闭包捕获。"""
    config: Config
    ue_client: McpClientSession
    world_state: WorldState | None
    skill_registry: SkillRegistry
    skill_ref: list[dict | None] | None
    snapshot_recorder: Any | None
    pending_screenshot_ref: list[Any] | None
    vision_session_manager: Any | None
    reference_session: ReferenceImageSession      # §5
    stop_summary: Callable[..., str]              # 原 StopLimitInterceptor.build_summary
    tool_logger: Any | None                       # 直接引用，替代 type(ic).__name__ 字符串匹配
    post_interceptors: list[ToolCallInterceptor]  # 本地事件全链广播用（仅 vision_screenshot）

# 结果构造（消灭 25+ 处重复）
def tool_ok(text: str) -> CallToolResult: ...
def tool_fail(text: str) -> CallToolResult: ...

# 本地工具计时 + JSONL 日志（消灭 8 处 t0/duration/_log_harness_call 样板）
class LocalToolCall:
    """async with LocalToolCall(ctx, name, args) as call: ... call.finish(text)"""
    # 进入时记 t0；finish(text, error=None) 自算 duration 并写 ToolCallCompleted 给 tool_logger。

# 本地事件广播（仅 vision_screenshot 需要 —— VisionInterceptor/SnapshotRecorder 消费截图）
async def emit_local_event(ctx: ToolContext, event: ToolCallCompleted) -> None:
    """对 post_interceptors 全链 post_call，异常不阻断（同 Contract 1 语义）。"""

def require_vision_manager(ctx) -> VisionSessionManager | CallToolResult:
    """消灭 4 处 vision_session_manager is None 守卫克隆。"""
```

**本地工具的两条事件通道（重要，保持现行为）**：① `LocalToolCall` 只写 ToolCallLogger（vision_ask/tell/reset/status、skill 系、match_reference、build_atmosphere_mapping 走这条，避免 StateCache 误标 dirty）；② `emit_local_event` 全链广播（只有 vision_screenshot 走这条，VisionInterceptor 与 SnapshotRecorder 必须看到截图事件）。当前代码正是这个区分（字符串匹配找 logger vs 全链 for 循环），新设计把它显式化、文档化。

### 4.2 server.py 瘦身后的形态

```python
def build_server(config, ue_client, interceptors=None, context_providers=None,
                 world_state=None, skill_ref=None, snapshot_recorder=None,
                 pending_screenshot_ref=None, vision_session_manager=None,
                 skills_dir=None, stop_limit=None) -> Server:
    server = Server("ue-agent-harness")
    ctx = ToolContext(...)                     # 装配（注册表在模块级构建）
    tools = build_tool_registry()              # list[HarnessTool]，从三个 handler 模块收集

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        ue_tools = await _ue_filtered_tools()  # 原 _rebuild_tool_reference 改名
        return [_to_mcp_tool(t) for t in ue_tools] + [t.spec() for t in tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        hit = registry_lookup(tools, name)
        if hit is not None:
            return await hit.handler(ctx, arguments)
        return await _forward_to_ue(name, arguments)   # 见下

    return server
```

`_forward_to_ue` 是 Contract 1 调用规范的纯净实现（~60 行）：pre 链（异常→阻断，语义同现状）→ `ue_client.call_tool` → 解析一次（`mcp_parse_result` + `mcp_extract_text`）→ 构造 `ToolCallCompleted` → post 链 → parsed_text 回同步（DriftAlert/Readback 徽章通道）→ Hard Boundary `_needs_refresh` 检查 → 返回。逻辑与现状逐行等价，只是从 12 个 if 的尾部解脱出来。

### 4.3 handler 分模块归位

| 模块 | handlers | 说明 |
|---|---|---|
| context/skill_tools.py | activate_skill / save_skill / deactivate_skill / get_context | 依赖 skill_registry + skill_ref + snapshot_recorder，全在 ToolContext |
| verification/vision_tools.py | vision_screenshot / vision_ask / vision_tell / vision_reset / vision_status | 薄包装 VisionSessionManager + capturer；vision_screenshot 走 emit_local_event |
| verification/reference.py | match_reference | 见 §5 状态类化 |
| verification/atmosphere.py | build_atmosphere_mapping | 属性扫描/索引/MiMo 分类/Markdown 渲染，含现有全部 `_*mapping*_` 辅助函数 |

### 4.4 兼容策略（测试不破）

tests 直接从 harness.server 导入私有 helper（`_build_property_index`、`_build_mimo_prompt`、`_resolve_mimo_indices`、`_render_mapping_markdown`、`build_server`）。迁移期在 server.py 保留 re-export shim（`from harness.verification.atmosphere import _build_property_index  # noqa: F401 兼容 shim, Phase 4 删除`），Phase 4 统一改测试导入后删 shim。

---

## 5. Part B：match_reference 状态类化（verification/reference.py）

### 5.1 ReferenceImageSession

```python
@dataclass
class ReferenceImageSession:
    """参考图对比会话状态（0714 计划指定的「显式会话状态对象」）。

    替代 server.py `_session_reference` 裸 dict 的 14 个魔法键。
    不变量（0714）：总上限 max(10, countdown_start_round+3)；激活当轮立即 -1；
    换参考图清空全部累积状态；SaveLevelAs 仅 hist 首次 ≥0.70 保存一次。
    """
    ref_path: str = ""
    ref_b64: str = ""
    loaded: bool = False
    match_count: int = 0
    countdown_remaining: int | None = None
    countdown_start_round: int = 0
    max_allowed_rounds: int = 10
    metrics: dict | None = None
    prev_metrics: dict | None = None
    prev_hist: float | None = None
    best_metrics: dict | None = None
    decline_count: int = 0
    snapshot_saved: bool = False
    best_snapshot_path: str | None = None
    mapping_generated: bool = False

    def check_stop(self) -> str | None: ...        # 硬终止判定（先于截图/MiMo，0714 红线）
    def begin_round(self, ref_path: str) -> bool:  # 换图重置 + 计数，返回是否首次加载
    def activate_countdown(self, hist: float) -> None: ...
    def record_metrics(self, m: dict) -> RoundEvents: ...  # best 追踪/连续下降/倒计时递减
```

### 5.2 handler 变为线性七步

```
check_stop → 加载参考图(PIL) → capture_screenshot → compute metrics（失败降级 ⚠ 注记）
→ record_metrics（含 SaveLevelAs 快照一次）→ Vision 双图对比 → 渲染（header 倒计时 + 趋势 + 9 维度 + 指标表 + 尾部固定引导文本）
```

每步一个私有函数，输出文本逐字保留。`ref_build_stop_summary`（原 StopLimitInterceptor.build_summary）同模块归位；`ref_resolve_snapshot_path`（原 `_ensure_best_snapshot_path`，改名——它不「ensure」任何东西，是解析+缓存路径）；趋势渲染 `_render_metrics_trend`（原 `_build_trend_summary`）。**同批修复 `lines`→`body_lines` 的 UnboundLocalError。**

---

## 6. 公共函数抽取清单（按三级归位）

### 6.1 协议层 → harness/client.py（新增公开 mcp_ 族）

| 新函数 | 吸收的重复（删除处） |
|---|---|
| `mcp_parse_result(raw) -> Any` | server.py `_parse_raw_result`；全仓 35 处防御性 json.loads 的公共入口 |
| `mcp_extract_text(raw) -> str \| None` | client.py:68 `_extract_text_from_result`、server.py:1746 `_extract_parsed_text`、hard_boundary.py:169 `_unwrap_tool_result`、verification/interceptor.py:311 `_unwrap_mcp_text` |
| `mcp_unwrap_return_value(text) -> dict \| None` | server.py:1435 ≡ verification/interceptor.py:331（Type-1 克隆删一份）+ hard_boundary/refresher 内联 ×4 |
| `mcp_unwrap_return_text(text) -> str` | server.py:1414 `_unwrap_return_value_text` |
| `mcp_tool_short_name(full) -> str` | normalize.extract_short_name + 5 处克隆（logger/snapshotter/replay/state.interceptor×2/verification.interceptor） |
| `mcp_build_request_frame(...)` / `mcp_parse_error_frame(...)` / `mcp_classify_http_error(...)` | client.py 内帧构造 ×3、错误帧解析 ×4、HTTP 状态分类 ×2 |

依赖方向安全：server / verification / state 均已 import client，client 不反向 import，无环。

### 6.2 UE 语义层 → harness/state/normalize.py（0706 先例的家）

| 新函数 | 吸收的重复 |
|---|---|
| `state_parse_actor_names(result) -> list[str]` | refresher.py `_parse_actor_list` + server.py `_extract_actor_names`（fallback 行为已漂移，**取并集**） |
| `state_parse_ref_path`（公开化） | `_parse_ref_path` 私有名被 2 模块 4 处跨用 |
| `state_get_or_create_snapshot(cache, name)` / `state_mark_actor_dirty(cache, name)` | state/interceptor.py 17 个 handler 的样板骨架 |
| `state_fingerprint_mismatches(cur, expected) -> list[str]` | hard_boundary.py 比对与日志两处硬编码同一字段表 |

### 6.3 子系统层

**verification/capturer.py**：`capture_b64_to_pil` / `capture_pil_to_b64`（吸收 server.py `_b64_to_pil` 与内联 BytesIO）、`capture_resize_to_screenshot`（合并 capture_from_file 与 parse_screenshot 的三段克隆）、`capture_extract_b64(event, get_pending)`（合并 VisionInterceptor 与 SnapshotRecorder 的双路径提取）、`capture_is_screenshot_tool`（**收敛为一份**，取 verification 版关键词语义；同步修 logger 漏判 UE 原生工具的 Bug B5）。

**verification/vision_agent.py**：`VISION_CONFIDENCE_BADGES` 常量（emoji ×3）；四个公开方法收敛为 `_vision_invoke(messages, system, temperature, record_history)` 私有骨架（四段式样板 ~60% 重复）；删废弃 property（pass_/reason/adjustment 恒 None 兼容层）。

**verification/session.py**：`vision_record_verdict(...)` 合并 add_screenshot/ask 尾段 verdict_dict 克隆 ×2；`vision_verdict_to_dict` 统一第三处变体（interceptor.py:143）；**同批修复 B7**（读取从未写入的 pass/reason 键）。

**observability**：`obs_load_jsonl`（replay/stats 合一）、`obs_utc_timestamp`（4 处 2 格式统一）、`obs_sanitize_args`、safe_filename 合一、`_short_name` 删除改用 `mcp_tool_short_name`；`obs_session_dir(log_dir, session_id)` 布局知识单点化（**同批修复 B3 stats**）。

**config**：`config_load_dotenv_file(candidates)`（合并 config._load_dotenv ≡ verification/config._parse_and_set 克隆）；`merge_cli_overrides` 改 `dataclasses.fields` 驱动（消灭曾致事故的 19 字段手工清单）；vision 默认值单源化（**修 B4**）。

**transport.py**：`mcp_run_uvicorn_gracefully` 合并 serve 两分支启动/关停序列；`_set_error_log_path` 降私有并修 docstring。

**verification/interceptor.py**：`_prepend_badge(event, badge)` 合并 Readback 两条注入路径；`_confirm_cache` → `_state_confirm_readback`；Vision/Readback 的 `cache` 形参统一改名 `world_state`。

**state/interceptor.py**：`record_write` 改**回调注入**（`StateCacheInterceptor(cache, on_write=...)`，cli.py 接线 `vision_record_write`）——切断 state→verification 反向 import（消除 state↔verification 环），同时把调用点从 7 个 handler 内手动调用收口到 post_call 成功分支单点（**修 B6 漏调**）。

**WorldState**：`_needs_refresh` 私有属性被 3 模块当协议字段用 → 加 `request_refresh()` / `consume_refresh()` 方法封装（字段集不变，不触碰 Contract 2）。

---

## 7. Bug 修复清单（重构同批落地）

| # | 级别 | 内容 | 批次 |
|---|---|---|---|
| B1 | P0 | server.py 连续下降分支 `lines`→`body_lines`（UnboundLocalError） | Phase 3 |
| B2 | P0 | cli.py:204 from-import 绑定 None → 改 `lambda: snapshotter_module.last_saved_screenshot_path()` getter | Phase 5 |
| B8 | P0 | 核实 `_cancel_request` 是否违反 HANDOFF_0624 禁令（回潮 or 有意恢复），按结论修复或补注释 | Phase 1 前核实 |
| B3 | P1 | stats.py 适配新 schema（ts/tool/ms）+ 递归 session 子目录 | Phase 5 |
| B4 | P1 | vision_model 默认值单源化（对齐「项目选用 MiMo」的文档口径核实后定） | Phase 5 |
| B5 | P1 | `capture_is_screenshot_tool` 收敛一份 | Phase 4 |
| B6 | P1 | record_write 收口到 post_call 单点，组件/文件夹操作不再漏记 | Phase 4 |
| B7 | P1 | session.py 删除读取 pass/reason 的死块或按 0707 schema 修正确认键 | Phase 4 |
| B9 | P2 | SSE 解析收敛为单一累加器（多行 data 行为统一） | Phase 5 |
| B10 | P2 | save_skill overwrite 入 schema（注册表化后自然消除） | Phase 2 |
| B12 | P2 | 死代码批删（Vector3/VisionConfig/call_tool_blocking/死导入/死变量/noop 块；`record_harness_dirty` 二选一：接线或删除并同步 contracts.md） | Phase 5 |
| B13 | P2 | 字符串类名匹配 → ToolContext 直接引用 | Phase 2 |
| B14 | P2 | capture() 删 dead ue_client 参数（3 调用点同步） | Phase 4 |
| B16 | P2 | REQUIRED_FIELDS 补 tools_allowlist 对齐 validate 行为 | Phase 5 |
| B17 | P2 | skill_registry 循环变量 field 遮蔽 dataclasses.field → 改名 | Phase 5 |
| B18 | P2 | state `_build_handlers` 170 键展开 vs 短名 fallback 冗余，留其一 | Phase 4 |
| B19 | P1 | 拦截器链顺序：以代码为准钉死 + 顺序断言测试 + 文档回标 | Phase 2/6 |

---

## 8. 命名规整（前缀+功能）

### 8.1 规约

- 跨模块符号：公开（无下划线）+ 子系统前缀：`mcp_`（协议）、`state_`（世界状态）、`vision_`、`capture_`、`skill_`、`obs_`（可观测）、`ctx_`（上下文组装）、`cli_`、`ref_`（参考图，归 verification 域的可视情况用 vision_/capture_）。
- 模块私有：`_` 开头，跟随所属模块语境即可，不强制前缀。
- 类名：CamelCase 保持，语义指向修正（如 StopLimitInterceptor 名不副实——不覆盖任何钩子）。
- LLM 可见 MCP 工具名不在本次范围（§9）。

### 8.2 重命名主表（P0/P1，全表随方案评审定稿）

**P0 — 跨模块共享 + 表意误导（随 Phase 1-3）：**

| 当前名 | 位置 | 新名 |
|---|---|---|
| `_extract_text_from_result` | client.py:66 | `mcp_extract_text`（公开，合并 4 处同族） |
| `_try_unwrap_return_value` | server.py:1435 | `mcp_unwrap_return_value`（删 verification 克隆） |
| `_unwrap_return_value_text` | server.py:1414 | `mcp_unwrap_return_text` |
| `_parse_raw_result` | server.py:1736 | `mcp_parse_result` |
| `extract_short_name` | state/normalize.py:202 | `mcp_tool_short_name`（normalize 留别名过渡） |
| `_parse_ref_path` | state/normalize.py:101 | `state_parse_ref_path`（公开化） |
| `_parse_actor_list` + `_extract_actor_names` | refresher.py:82 / server.py:1248 | `state_parse_actor_names`（行为并集） |
| `capture` | capturer.py:95 | `capture_screenshot`（+删 dead ue_client 参数） |
| `init_shot_session` / `close_shot_session` | capturer.py:42,77 | `capture_init_session` / `capture_close_session`（shot→screenshot 术语统一） |
| `parse_screenshot`（123 行） | capturer.py:602 | 拆 `capture_extract_b64` + `capture_resize_to_screenshot`，门面 `capture_parse_result` |
| `record_write` / `get_recent_writes` | verification/session.py | `vision_record_write` / `vision_get_recent_writes`（位置不动，015 决策） |
| `_parse_and_set` | verification/config.py:104 | `config_load_dotenv_file`（合并 config.py 克隆） |
| `full_refresh` | state/refresher.py:22 | `state_full_refresh` |
| `StopLimitInterceptor.build_summary` | stop_limit.py | `ref_build_stop_summary`（入 reference.py，类废止） |
| `_rebuild_tool_reference` | server.py | `_ue_filtered_tools`（无 rebuild 语义，是带缓存获取） |
| `_ensure_best_snapshot_path` | server.py | `ref_resolve_snapshot_path` |
| `_log_harness_call` | server.py | 工具化为 `LocalToolCall`（tools.py） |
| `_b64_to_pil` | server.py | `capture_b64_to_pil`（移 capturer） |

**P1 — 公开但过泛（随 Phase 4-5）：**

| 当前名 | 新名 | 当前名 | 新名 |
|---|---|---|---|
| `apply_filter` | `ctx_filter_tools` | `assemble_system_prompt` | `ctx_assemble_prompt` |
| `is_escape_hatch` / `ESCAPE_HATCH_TOOLS` | `ctx_is_always_visible_tool` / `CTX_ALWAYS_VISIBLE_TOOLS` | `_pie_str` | `_format_pie_status` |
| `_is_screenshot_tool` ×3 | `capture_is_screenshot_tool`（一份） | `_load_jsonl` ×2 | `obs_load_jsonl` |
| `_serialize_args` | `obs_sanitize_args` | `_timestamp` | `obs_utc_timestamp` |
| `_format_output` / `_summarize_verbose_output` / `_truncate` | `obs_format_tool_output` / `obs_summarize_tool_output` / `obs_truncate_text` | `validate_skill` | `skill_validate_yaml` |
| `_normalize_list` | `skill_normalize_str_list` | `init` / `enabled` / `log_exception`（debug.py） | `vision_debug_init` / `vision_debug_enabled` / `vision_log_exception` |
| `VisionSubAgent.check` | `analyze_screenshot` | `VisionSubAgent.classify` | `classify_properties` |
| `build_scene_context` / `build_full_prompt_context` | `vision_build_scene_context` / `vision_build_prompt_context` | `_cap_context` | `_truncate_context_blocks` |
| `_confirm_cache` | `_state_confirm_readback` | interceptor 的 `cache` 形参 ×2 | `world_state`（与 VisionSessionManager 统一） |
| `cmd_start` / `_cmd_stats` 等 | `cli_cmd_start` / `cli_cmd_stats` …（风格统一） | `_setup_logging` | `cli_setup_logging` |
| `_verify_level_persistence_tools` | `state_verify_persistence_tools` | `EXPECTED_LEVEL_TOOLS` | `LEVEL_PERSISTENCE_EXPECTED_TOOLS` |
| `_build_mimo_prompt` | `_build_classify_prompt`（去供应商名耦合） | `_resolve_mimo_indices` | `_resolve_classified_indices` |
| `_build_trend_summary` | `_render_metrics_trend` | `_render_mapping_markdown` | `_render_mapping_md` |
| `_item_to_name` | `_actor_ref_name` | `_extract_property_names` | `_parse_property_names`（入 atmosphere.py） |
| `_rebuild_shot_session` / `_refresh_cache_on_reconnect` | `_restore_capture_session` / `_state_refresh_on_reconnect` | 嵌套 `run` ×2 | `_run_server` / `_run_vision_check` |
| `_try_file_fallback` / `_poll_and_capture` | `_capture_via_file_fallback` / `_capture_poll_screenshot_dir` | `_capture_asset_image_with_file_fallback` | `_capture_call_asset_image` |
| `VisionSession.touch` | `mark_active` | `VisionSessionManager.start` / `reset` | `start_session` / `reset_session` |
| `parse_sse_stream` / `_read_sse_stream` | `sse_parse_bytes` / `_sse_read_tool_result`（收敛后） | `_rpc` | `_mcp_post_rpc`（`call_tool_blocking` 删除） |
| `compute_match_metrics` | `ref_compute_metrics` | `_default_log_dir` | `obs_default_log_dir` |

**P2（随迁移顺带）**：`build_server`→`create_harness_server`（可选）；`SnapshotRecorder`→`ObsSessionRecorder`（可选，需同步 docs）；`_handle_*` ×17 保留（与工具名逐一对齐）；`DebugPreCallInterceptor` 保留（contracts.md 引用，011 落地时替换）。

---

## 9. 不做的事（范围护栏）

1. **不改 LLM 可见 MCP 工具名**（activate_skill→skill_activate 之类）：外部契约 + skill YAML + instructions 三方耦合，单独立项评估。
2. **不合并 `_rpc` 与 `call_tool`**：PLAN_0621 遗留债，单列后续 Phase（本次只做帧构造/错误解析的公共化）。
3. **不实现 011 Safety**：只为它保留 pre_call 挂点与文档语义澄清。
4. **不实现 009 轨迹记忆 / 不填充占位目录**（memory/recovery/safety）。
5. **不改工具输出文本**（行为控制面，0712 教训）；不改 0713/0714 信任层级与叫停公式。
6. **不碰 UE 侧插件**（LevelPersistenceToolset / SaveLevelAs / LoadLevel 在 {UE_PROJECT_ROOT}）。
7. **不提前固化 016 Part B 的 4 个待定项**。

---

## 10. 分阶段实施计划（每阶段全量测试绿为门禁）

**Phase 0 — 基线与补缺（0.5d）**：确认 331+4 基线；补 match_reference handler 流程测试（当前仅 test_stop_limit 间接覆盖）；加拦截器链顺序断言测试（B19）；核实 B8 `_cancel_request`。

**Phase 1 — 协议层公共化（1d）**：client.py 落地 mcp_ 族（§6.1）；13+ 处解包点迁移；删 server.py:50 死导入与 verification 克隆。纯机械、行为保持。门禁：test_client / test_client_health / test_normalize / test_state / test_verification_interceptor 全绿。

**Phase 2 — call_tool 注册表化（2d）**：tools.py + 三个 handler 模块；ToolContext 装配；`call_tool` 分发改造；`_forward_to_ue` 独立；re-export shim 保测试；B10/B13 随批。门禁：test_build_atmosphere_mapping / test_stop_limit / test_context / test_skill / test_vision_session 全绿。

**Phase 3 — match_reference 状态类化（1.5d）**：reference.py + atmosphere.py 落地；ReferenceImageSession；stop_limit.py 删除并入；B1 修复；0714 公式单测。门禁：test_stop_limit / test_metrics / test_build_atmosphere_mapping 全绿。

**Phase 4 — 命名规整 sweep（1d）**：rename_symbol 批量执行 §8.2 主表；capture 系列改名 + dead 参数（B14）；record_write 注入化（B6）；B5/B7/B18；测试导入更新 + 删 shim。门禁：全量绿。

**Phase 5 — 周边修复与清理（1d）**：B2/B3/B4/B9/B12/B16/B17；cli.py 拆解（instructions ~55 行 prompt 文本移入 context/prompt.py 或模板；cmd_start 337 行拆出装配函数）；client.py 帧助手 + SSE 收敛；transport 整理。门禁：全量绿 + `uv run harness start` 冒烟（连 UE 验证 L3 e2e 可选）。

**Phase 6 — 文档同步（0.5d，neat-freak）**：architecture.md §4 目录重绘 + 工具数口径；contracts.md 追加修订段（pre_call 语义、链顺序、ActorSnapshot label/tags、Contract 2 引 ADR 0008）；CONTEXT.md 按 ADR 0008 修订 State Cache/Task Memory 词条；CLAUDE.md 状态表；ADR 0005 回标持久化条款修订。

**顺序理由**：先公共层（Phase 1）让后续抽取有工具可用 → 再分发骨架（Phase 2）解决主诉 → 再状态类化（Phase 3）啃最硬的 match_reference → 命名（Phase 4）在结构稳定后扫尾 → 修复清理（Phase 5）→ 文档（Phase 6）。

**回退策略**：每 Phase 独立 commit；Phase 2/3 保留 shim 保证任何中间态测试全绿；出问题按 Phase  revert。

**风险**：① Phase 2 移动 handler 时输出文本必须逐字（LLM 行为契约）——diff 审查清单；② 拦截器顺序改动风险——本方案不改顺序，仅删除空转的 StopLimitInterceptor 出链（它无钩子，在链与否行为等价）；③ state↔verification 环在移动符号时易爆 import cycle——Phase 4 先做完 record_write 注入化再动其他符号。
