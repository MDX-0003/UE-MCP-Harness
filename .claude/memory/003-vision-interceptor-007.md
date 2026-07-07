---
name: vision-pipeline-architecture
description: Vision 验证闭环完整架构——VisionInterceptor、VisionSessionManager、VisionVerdict、vision_calls.jsonl
metadata:
  type: project
---

# Vision 验证闭环 — 当前架构

## 数据流（当前）
```
LLM 调 vision_screenshot(question="...")
→ server.py: 截图 → pending_screenshot_ref
→ VisionInterceptor.post_call: 取截图 → VisionSessionManager.add_screenshot()
→ VisionSubAgent.check(image, question, scene_context) → _call_vision_api()
→ MiMo API (response_format JSON mode + unified prompt + _VISION_FORMAT_REMINDER)
→ _parse_verdict(raw) → VisionVerdict{answer, confidence, caveats, observations}
→ 写入 WorldState.last_vision_verdict (内存单槽)
→ _append_vision_call_log() → vision_calls.jsonl (实时落盘, question+answer 配对)
→ server.py 格式化: [Vision 分析] 🟢/🟡/🔴 置信度 + answer + 分项观察 → LLM 看到

LLM 调 vision_ask(question="...")
→ VisionSessionManager.ask() → VisionSubAgent.continue_with_question()
→ 复用对话历史(含之前截图) → 同样走 _parse_verdict

LLM 调 vision_reset()
→ VisionSessionManager.reset() → _write_session_json() → vision_sessions/{id}.json

Harness 关闭
→ VisionSessionManager.close_active() → 自动归档未关闭的 Vision session
```

## VisionVerdict (当前格式)
```python
@dataclass
class VisionVerdict:
    answer: str              # 自然语言回答
    confidence: str = "medium"  # high/medium/low
    caveats: list[str]       # Vision 主动标注的限制条件
    observations: list[dict] # [{"what","finding","confidence"},...]
    need_more_info: bool
    question: str
    raw_response: str
    # 向后兼容 property: pass_ (→None), reason (→answer), adjustment (→caveats)
```

## 关键源码
- `harness/verification/vision_agent.py` — VisionSubAgent, VisionVerdict, _parse_verdict, unified prompt
- `harness/verification/session.py` — VisionSessionManager (add_screenshot/ask/reset/close_active, _append_vision_call_log, build_scene_context + 附近几何体)
- `harness/verification/interceptor.py` — VisionInterceptor.post_call
- `harness/server.py:516-541` — 徽章格式化 🟢/🟡/🔴

## Vision 模型
- MiMo `mimo-v2.5-pro` via Xiaomi proxy
- `response_format: {"type": "json_object"}` 通过 `extra_body` 启用
- `max_tokens=4096`
- 见 [[vision-model-config]]

Why: Vision 闭环是 Harness 核心价值——act → Vision verify → 闭环修正。
How to apply: 修改 Vision 相关代码前参考此架构，避免引入与现有数据流冲突的路径。
