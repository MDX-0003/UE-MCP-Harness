# Harness MVP 闭环开发计划 — 2026-06-16

## 当前状态总览

| Issue | 模块 | 代码 | 测试 | 集成 | 备注 |
|:---:|------|:---:|:---:|:---:|------|
| 001 | Harness 骨架 | ✅ | — | ✅ | CLI / Config / Server / Transport |
| 002 | 工具发现与透传 | ✅ | ✅ | ✅ | MCP 握手 + SSE + 211 工具 |
| 003 | 可观测性 | ✅ | 23 tests | ✅ | JSONL 日志 + stats + replay |
| 004 | Context Assembly | ✅ | 21 tests | ✅ | 工具过滤 + 三层 Provider + get_context |
| 005 | Skill 系统 | ✅ | 31 tests | ✅ | CRUD + match + activate/deactivate/save |
| 006 | Vision Pipeline | ✅ | 24 tests | ⚠️ | CLI 可用，**未接入 MCP 循环** |
| 007 | 验证闭环 | ❌ | — | ❌ | 依赖 005+006 |
| 008 | State Cache | ✅ | 18 tests | ✅ | L1 write-through + L3 refresh |
| 009 | 任务记忆 | ❌ | — | ❌ | 依赖 002+005+008 |
| 010 | 错误恢复 | ⬜ | — | — | **跳过** |
| 011 | 安全护栏 | ❌ | — | ❌ | 依赖 002 |

**全量测试**：137 unit tests passed + L3 e2e 7/7 passed

---

## MVP 闭环定义

```
用户 "改成黄昏"
    │
    ▼
LLM (Claude Code) ← VS Code 侧
    │  activate_skill("黄昏")
    │  tools/call("set_actor_transform", ...)
    │  tools/call("CaptureEditorImage")
    ▼
Harness MCP Server (port 9000)
    │  工具过滤 (004)
    │  拦截器链: DebugPreCall → Logger (003) → StateCache (008) → [Vision !!!]
    │  Skill 管理 (005)
    ▼
UE MCP Server (port 8000)
    │  截图 → Vision API 分析 → 结果注入上下文 → LLM 调整 → 重试
    ▼
Vision Sub-Agent (006) — 独立 LLM API 调用
```

闭环的关键：**验证步骤自动触发**。当 LLM 调了截图工具 → Harness 自动调 Vision 分析 → 结果注入 `get_context` → LLM 看到反馈 → 调整参数。

---

## 剩余步骤

### Phase 1: VS Code 接入验证（30 分钟）— 优先级 P0

**目标**：确认 LLM → Harness → UE 链路在真实 MCP 客户端下可用。

**操作**：
1. 启动 UE + Harness
2. VS Code 加载 `.claude/mcp.json`（已就绪）
3. 验证点：
   - [ ] Claude 能看到 Harness 暴露的工具（activate_skill, get_context, get_context, ~20 个 UE 工具）
   - [ ] `activate_skill("黄昏")` 正确激活 evening-lighting
   - [ ] 简单工具调用链路通（`get_context` 返回有效文本）
   - [ ] `instructions` 是否正确传达给 LLM

**不做代码改动**，纯验证。

**风险**：可能发现 instructions 未到达 LLM、工具描述 LLM 不理解、Skill 匹配不生效等问题。发现问题后记录，在 Phase 2 中修复。

---

### Phase 2: 007 验证闭环（1-2 天）— 优先级 P0

**目标**：Vision 分析接入 Harness MCP 循环，LLM 的截图操作自动触发视觉验证。

**新建文件**：

| 文件 | 说明 |
|------|------|
| `harness/verification/interceptor.py` | `VisionInterceptor(ToolCallInterceptor)` |

**修改文件**：

| 文件 | 改动 |
|------|------|
| `harness/state/models.py` | `WorldState` 新增 `last_vision_verdict: dict | None` |
| `harness/context/prompt.py` | `_render_state_snapshot` 新增"视觉验证反馈"段落 |
| `harness/cli.py` | 拦截器链注册 `VisionInterceptor` |
| `harness/config.py` | 确认 `.vision.env` 加载路径在 `harness start` 流程中生效 |

**核心逻辑**：

```
VisionInterceptor.post_call(event)
  if event.name 是截图工具 (CaptureEditorImage / Screenshot):
      if event.error is None and event.parsed_text:
          base64 = 从 event 提取图片数据
          skill = _active_skill
          expected = skill.verification.expected if skill else None
          verdict = await vision_agent.check(base64, expected=expected)
          cache.last_vision_verdict = {
              "pass": verdict.pass_,
              "reason": verdict.reason,
              "adjustment": verdict.adjustment,
              "at_step": task_memory.current_step,  # 009 未实现时为 None
          }
  else:
      pass  # 非截图工具，透传
```

**System Context 新增段落**（在 `_render_state_snapshot` 中）：

```
上次视觉验证：
  ✅ 通过：光照角度正确，阴影长度符合预期
  或
  ❌ 未通过：亮度过高，方向光角度仍接近正午。建议：降至 15 度，强度降至 30%

如果连续 3 次未通过 → 标记步骤失败，建议向用户确认
```

**测试**：新增 `tests/test_verification_interceptor.py` — mock Vision API，验证：
- 截图工具调用 → post_call 触发 Vision 分析
- Vision 结果正确写入 WorldState
- 非截图工具调用 → 不触发 Vision 分析
- Vision 分析失败 → 不影响主流程（error 容忍）

---

### Phase 3: VS Code 功能验证（30 分钟）— 优先级 P1

Phase 2 完成后，重新在 VS Code 中测试完整链路：

- [ ] 用户说"改成黄昏" → Claude 调 `activate_skill("黄昏")` → Skill 激活
- [ ] Claude 按 Skill 步骤调工具 → 步骤完成
- [ ] Claude 调 `CaptureEditorImage` → Harness 自动 Vision 分析 → 结果出现在 `get_context`
- [ ] Claude 根据 Vision 反馈调整参数 → 重新截图 → Vision 通过 → 进入下一步
- [ ] Skill 完成后，Claude 可选择 `save_skill` 保存调整后的版本

---

### Phase 4: 009 任务记忆（1-2 天）— 优先级 P1

**目标**：长任务（10+ 步）的 context 不爆炸。

**新建文件**：

| 文件 | 说明 |
|------|------|
| `harness/memory/compressor.py` | `TaskMemory` pydantic 模型 + 压缩逻辑 |
| `harness/memory/injector.py` | 将 `TaskMemory` JSON 注入 LLM context |

**核心逻辑**：`TaskMemory` 从 Skill 的 `steps` 初值化 `pending` 列表。每次工具调用后，如果 Vision PASS 或 LLM 声明步骤完成，`pending[0]` → `completed`。当 tool_call_count > 20 时，injector 将结构化 `TaskMemory` JSON 注入 context，替代原始历史。

**阻塞**：依赖 005（Skill steps 初值化）+ 008（StateCache 提供 key_assets）

**测试**：mock 30 步工具调用 → 验证压缩后 context < 1000 tokens

---

### Phase 5: 011 安全护栏（半天）— 优先级 P2

**目标**：防止 LLM 的破坏性操作。

**新建文件**：

| 文件 | 说明 |
|------|------|
| `harness/safety/preflight.py` | `SafetyInterceptor(ToolCallInterceptor)` — pre_call 钩子 |
| `harness/safety/defaults.py` | 默认规则集 |

**核心逻辑**：在 `pre_call` 阶段检查工具名和参数：
- 删除包含 "PlayerStart" 的 Actor → DENY（除非显式 `allow_dangerous: true`）
- 对 `/Engine/` 或 `/Game/System/` 路径的写操作 → ASK_USER
- 批量删除（>10 Actor）→ ASK_USER
- PIE 运行中禁止写操作 → DENY

**集成**：在拦截器链中替换 `DebugPreCallInterceptor` → `SafetyInterceptor`

---

## 执行建议

```
Day 1 上午: Phase 1  VS Code 接入验证  (30 分钟)
                           │
                           ▼ (反馈问题)
Day 1 下午: Phase 2  007 验证闭环      (核心开发)
Day 2 上午: Phase 2  007 收尾 + 测试
Day 2 下午: Phase 3  VS Code 功能验证  (30 分钟)
                           │
Day 3:      Phase 4  009 任务记忆      (长任务支持)
Day 4:      Phase 5  011 安全护栏      (保护性收尾)
```

每个 Phase 完成后独立验证，不阻塞下一个 Phase。

---

## 当前已知隐患

| # | 隐患 | Phase 中解决 |
|:---:|---|:---:|
| 1 | 未与真实 LLM 联调——可能有协议/工具描述/匹配问题 | Phase 1 暴露，Phase 2-3 修复 |
| 2 | `instructions` 未验证是否到达 LLM | Phase 1 |
| 3 | `get_context` 文本格式 LLM 能否理解 | Phase 3 |
| 4 | Skill YAML 短名 vs UE 全限定名映射 | Phase 3 暴露 |
| 5 | `/mcp/mcp` 双重路径 | 低优先级，择机修 |
