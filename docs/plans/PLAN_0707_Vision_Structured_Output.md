# PLAN 0707: Vision 统一结构化输出 — 取消二元 pass，引入分维度置信度

对应 Bug_analysis_0706.md 的 P0-3（提问模式 pass 硬编码为 True）和本次会话分析发现的
"Vision 旋转判断错误但被 ✅ 章掩盖"问题。

## 目标

无论验证模式还是提问模式，Vision API **始终返回同一份结构化 JSON**，不再有
"自由文本 + 硬编码 pass=True" 的路径。LLM 收到的信号从 `✅ PASS / ❌ FAIL`
变为分维度的、带置信度的结构化报告。

## 当前问题复盘

### 三模式现状

| 模式 | 触发条件 | system prompt | Vision 返回 | pass_ | 徽章 |
|:---|:---|:---|:---|:---|:---|
| verify | 有 expected | `VISION_SYSTEM_PROMPT_VERIFY` | JSON `{pass, reason, adjustment}` | 解析出 | ✅/❌ |
| question | 有 question | `VISION_SYSTEM_PROMPT_QUESTION` | 自由文本 | **硬编码 True** | **✅（谎言）** |
| describe | 都没有 | `VISION_SYSTEM_PROMPT_DESCRIBE` | 自由文本 | **硬编码 True** | **✅（谎言）** |

### 后果链

```
question 模式 → pass_=True 硬编码
  → server.py:520: status = "✅ PASS"
    → LLM 看到 ✅ 徽章 → 信任 Vision 判断 → 不做修正
      → Vision 即使判断错了（如旋转），LLM 也照单全收
        → 验证闭环在此断裂
```

### 涉及的全部文件

| 文件 | 当前角色 |
|:---|:---|
| `harness/verification/vision_agent.py:70-78` | `VisionVerdict` 数据模型 |
| `harness/verification/vision_agent.py:27-67` | 三份 system prompt |
| `harness/verification/vision_agent.py:92-176` | `VisionSubAgent.check()` 模式选择 |
| `harness/verification/vision_agent.py:203-237` | `continue_with_question()` — `verify_mode=False` |
| `harness/verification/vision_agent.py:178-201` | `continue_with_info()` — `verify_mode=True` |
| `harness/verification/vision_agent.py:253-366` | `_call_vision_api()` — prompt 选择 + max_tokens |
| `harness/verification/vision_agent.py:369-423` | `_parse_verdict()` — verify_mode 分支 |
| `harness/server.py:516-541` | `✅ PASS / ❌ FAIL` 徽章拼接 |
| `harness/verification/interceptor.py:140-147` | `last_vision_verdict` 写入 + 日志 |
| `harness/verification/session.py:558-620` | `add_screenshot()` verdict 存储 |
| `harness/verification/session.py:622-661` | `ask()` verdict 存储 |
| `tests/test_verification.py` | Vision 相关测试 |
| `tests/test_verification_interceptor.py` | Vision interceptor 测试 |
| `tests/test_vision_session.py` | Vision session 测试 |

---

## 代码结构变更

### 1. `VisionVerdict` 模型重构

```python
# vision_agent.py — 旧
@dataclass
class VisionVerdict:
    pass_: bool
    reason: str
    adjustment: str
    need_more_info: bool = False
    question: str = ""
    raw_response: str = ""


# vision_agent.py — 新
@dataclass
class VisionVerdict:
    """Vision Sub-Agent 的统一结构化返回。

    answer:      自然语言回答（替代旧 reason）。
    confidence:  "high" / "medium" / "low" — Vision 对自己回答的置信度。
    caveats:     Vision 主动标注的限制条件（"无法从 2D 截图判断旋转"）。
    observations: 分维度观察列表，每项带独立置信度。
    need_more_info: 是否需要补充信息才能做更准确判断。
    question:    如果需要追问，追问的问题文本。
    raw_response: 原始 API 响应，用于 debug。
    """
    answer: str
    confidence: str = "medium"          # high / medium / low
    caveats: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    # observations[i] = {"what": str, "finding": str, "confidence": str}
    need_more_info: bool = False
    question: str = ""
    raw_response: str = ""

    # ---- 向后兼容属性 ----
    @property
    def pass_(self) -> bool | None:
        """已废弃。始终返回 None——Vision 不再做二元判定。"""
        return None

    @property
    def reason(self) -> str:
        """已废弃。请用 answer。"""
        return self.answer

    @property
    def adjustment(self) -> str:
        """已废弃。请用 caveats。"""
        return "; ".join(self.caveats) if self.caveats else ""
```

关键：`pass_` 变为 `None`（永远不造假定），`reason`/`adjustment` 保留为 property 做向后兼容。

### 2. 统一 System Prompt（三合一）

```python
# vision_agent.py — 替代 VISION_SYSTEM_PROMPT_VERIFY / _QUESTION / _DESCRIBE

VISION_SYSTEM_PROMPT = """你是一个 Unreal Engine 编辑器截图分析器。
你会收到编辑器截图，以及可选的场景上下文和问题。请基于截图可见内容回答。

你必须返回以下 JSON 格式（不要包含其他内容）：

{
  "answer": "你的自然语言回答。直接、准确、基于截图可见内容。",
  "confidence": "high/medium/low",
  "caveats": ["从 2D 截图无法精确判断 3D 旋转角度"],
  "observations": [
    {"what": "物体位置", "finding": "位于画面左下方", "confidence": "high"},
    {"what": "物体旋转", "finding": "从当前视角呈直立姿态", "confidence": "low"}
  ],
  "need_more_info": false,
  "question": ""
}

字段说明：
- answer: 核心回答。如果收到的是验证性提问，请在回答中明确给出"符合预期"或"不符合预期"的结论。
- confidence: 你对 answer 的整体置信度（high/medium/low）。
- caveats: 你主动识别的限制条件。例如"截图分辨率不足"、"从当前视角无法判断深度关系"、"编辑器 gizmo 线框不代表最终渲染效果"。没有限制条件时写空数组。
- observations: 分维度的观察列表，每项描述你在截图中看到的一个具体事实，并给出该事实的置信度。至少包含 1 项。
- need_more_info: 如果确实无法给出有意义的回答（截图信息不足），设为 true 并填写 question。
- question: 仅在 need_more_info 为 true 时填写，说明需要什么补充信息。

重要：不要因为问题有"预期描述"就假设截图一定符合预期。你的任务是如实报告截图中的可见内容，不是迎合提问者。
判断灯光颜色/强度时，请关注被照亮的表面（而非编辑器 gizmo 线框或图标）。
"""
```

### 3. `VisionSubAgent.check()` 简化

```python
# vision_agent.py — 旧：三层优先级 question > expected > describe
if question:
    user_message = f"问题：{question}"
    is_verify = False
elif expected and expected != "描述截图内容":
    user_message = f"预期场景：{expected}\n容忍度：{tolerance}"
    is_verify = True
else:
    user_message = "请描述这张截图中的场景内容。"
    is_verify = False

# vision_agent.py — 新：统一组装，不再区分模式
parts = []
if question:
    parts.append(f"问题：{question}")
elif expected and expected != "描述截图内容":
    parts.append(f"预期场景：{expected}")
    parts.append(f"容忍度：{tolerance}")
else:
    parts.append("请描述这张截图中的场景内容。")
if scene_context:
    parts.append(f"场景上下文：\n{scene_context}")
user_message = "\n\n".join(parts)
```

不再需要 `is_verify` 变量。`_call_vision_api` 也不再需要 `verify_mode` 参数。
`continue_with_question()` 和 `continue_with_info()` 同样去掉 `verify_mode` 参数。

### 4. `_call_vision_api()` 简化

```python
# vision_agent.py — 旧
async def _call_vision_api(config, messages, verify_mode=True, question="") -> str:
    if verify_mode:
        system = VISION_SYSTEM_PROMPT_VERIFY
    elif question:
        system = VISION_SYSTEM_PROMPT_QUESTION
    else:
        system = VISION_SYSTEM_PROMPT_DESCRIBE

# vision_agent.py — 新
async def _call_vision_api(config, messages) -> str:
    system = VISION_SYSTEM_PROMPT  # 唯一 prompt
```

同时 `max_tokens` 从 1024 提升到 2048——新 JSON 结构比旧模式更丰富，需要更多 token 空间。旧 1024 在 extended thinking 模型下已被验证不够（P1-4 截断 bug）。

### 5. `_parse_verdict()` 单一路径

```python
# vision_agent.py — 旧：verify_mode 分支，question 模式全文即 reason + pass_=True
if not verify_mode:
    return VisionVerdict(pass_=True, reason=raw.strip(), ...)

# vision_agent.py — 新：统一 JSON 解析
def _parse_verdict(raw: str) -> VisionVerdict:
    """从 Vision model 返回中解析结构化 JSON。"""
    # JSON 提取逻辑不变（```json block → 裸 JSON 对象 → raw fallback）
    json_str = _extract_json(raw)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # 解析失败：全文作为 answer，confidence=low
        return VisionVerdict(
            answer=raw[:1000],
            confidence="low",
            caveats=["Vision model 未返回有效 JSON——以下为原始回答"],
            raw_response=raw,
        )

    return VisionVerdict(
        answer=data.get("answer", ""),
        confidence=data.get("confidence", "medium"),
        caveats=data.get("caveats", []),
        observations=data.get("observations", []),
        need_more_info=data.get("need_more_info", False),
        question=data.get("question", ""),
        raw_response=raw,
    )
```

### 6. `server.py` — LLM 看到的徽章和输出变更

```python
# server.py:516-524 — 旧
status = "✅ PASS" if v.get("pass") else "❌ FAIL"
reason = v.get("reason", "")
vision_info = f"\n\n[Vision 分析] {status}\n{reason}"

# server.py — 新
answer = v.get("answer", "")
confidence = v.get("confidence", "medium")
caveats = v.get("caveats", [])
observations = v.get("observations", [])

# 徽章：只用置信度，不造假 pass/fail
badges = {"high": "🟢", "medium": "🟡", "low": "🔴"}
badge = badges.get(confidence, "🟡")

parts = [f"\n\n[Vision 分析] {badge} 置信度: {confidence}"]
parts.append(answer)
if caveats:
    parts.append("⚠ 限制: " + "; ".join(caveats))
if observations:
    obs_lines = []
    for o in observations[:8]:  # 最多 8 条，避免 token 爆炸
        obs_badge = {"high": "✓", "medium": "~", "low": "?"}.get(o.get("confidence", ""), "~")
        obs_lines.append(f"  {obs_badge} {o.get('what', '')}: {o.get('finding', '')}")
    if obs_lines:
        parts.append("分项观察:\n" + "\n".join(obs_lines))
vision_info = "\n".join(parts)
```

LLM 看到的从 `✅ PASS\n原因...` 变为 `🟢 置信度: high\n回答...\n分项观察:...`。不再有被误解的二元信号。

### 7. `VisionInterceptor` — 日志和缓存写入

```python
# verification/interceptor.py:140-147 — 旧
self._cache.last_vision_verdict = {
    "pass": verdict.pass_,
    "reason": verdict.reason,
    "adjustment": verdict.adjustment,
    "at": datetime.now(timezone.utc).isoformat(),
}
status = "✅ PASS" if verdict.pass_ else "❌ FAIL"

# verification/interceptor.py — 新
self._cache.last_vision_verdict = {
    "answer": verdict.answer,
    "confidence": verdict.confidence,
    "caveats": verdict.caveats,
    "observations": verdict.observations,
    "at": datetime.now(timezone.utc).isoformat(),
}
badges = {"high": "🟢", "medium": "🟡", "low": "🔴"}
logger.info("Vision 分析完成: %s confidence=%s — %s",
            badges.get(verdict.confidence, "🟡"),
            verdict.confidence,
            verdict.answer[:120])
```

### 8. `VisionSessionManager` — 归档中的 verdict 字典

```python
# session.py:648-653 — 旧
verdict_dict = {
    "pass": verdict.pass_,
    "reason": verdict.reason[:1000],
    "adjustment": verdict.adjustment,
    "question": question[:500],
}

# session.py — 新
verdict_dict = {
    "answer": verdict.answer[:2000],
    "confidence": verdict.confidence,
    "caveats": verdict.caveats,
    "observations": verdict.observations[:10],
    "question": question[:500],
}
```

### 9. 测试变更

| 文件 | 变更 |
|:---|:---|
| `tests/test_verification.py` | Mock Vision API 返回新 JSON 格式；`pass_` → 验证为 None |
| `tests/test_verification_interceptor.py` | `last_vision_verdict` 字段名变更；日志格式变更 |
| `tests/test_vision_session.py` | verdict_dict 字段变更；`TestBuildSceneContext` 不变（不依赖 VisionVerdict） |

---

## 涉及文件总览

| 文件 | 改动 | 行数估算 |
|:---|:---|:---:|
| `harness/verification/vision_agent.py` | VisionVerdict 重构 + 统一 prompt + 去掉 verify_mode | +60 / -80 |
| `harness/server.py` | 徽章拼接从 ✅/❌ 改为 🟢/🟡/🔴 + 置信度 | +20 / -10 |
| `harness/verification/interceptor.py` | last_vision_verdict 字段变更 | +8 / -8 |
| `harness/verification/session.py` | verdict_dict 字段变更（2 处） | +8 / -8 |
| `tests/test_verification.py` | Mock 响应格式 + 断言 | +10 / -10 |
| `tests/test_verification_interceptor.py` | 字段变更 | +5 / -5 |
| `tests/test_vision_session.py` | verdict_dict 断言 | +5 / -5 |

**零新文件。净代码量 ≈ +60 行（prompt 文本占大头）。**

---

## 验收

1. `uv run pytest tests/ -v` 全绿
2. 重跑 Bug_analysis_0706 的同一任务（改灯色 + vision 验证）：
   - Vision 返回 `{"answer": "...", "confidence": "medium", "observations": [...]}`
   - LLM 看到 `🟡 置信度: medium` 而非 `✅ PASS`
   - 如果 Vision 说"灯光看起来不是红色"但 confidence=low，LLM 有动机去调 L2 读回确认
3. `last_vision_verdict` 缓存中不再有 `"pass"` 字段，`get_context` 输出相应更新
4. Vision session 归档 JSON 中 `verdict` 字段包含 `confidence` 和 `observations`

## 不做的事

- 不改变 `need_more_info` 追问机制（已有路径，本次不改）
- 不改变 Vision Session 管理器架构
- 不改变截图获取流程（capturer / file fallback）
- 不在本次加 Actor label 叠加（UE 侧改动，另开 Issue）
- 不改变 `vision_max_size` 配置（另开 Issue）
