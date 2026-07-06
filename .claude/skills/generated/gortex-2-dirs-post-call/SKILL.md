---
name: gortex-2-dirs-post-call
description: "Work in the . +2 dirs · post_call · . area — 105 symbols across 7 files (84% cohesion)"
---

# . +2 dirs · post_call · .

105 symbols | 7 files | 84% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.verification.capturer.Screenshot`
- `external-call::dep:harness.verification.interceptor.VisionInterceptor`
- `external-call::dep:harness.verification.vision_agent.VisionVerdict`
- `harness\verification\interceptor.py`
- `tests\test_interceptor.py`
- `tests\test_verification_interceptor.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | object |
| `external-call::dep:harness.verification.capturer.Screenshot` | harness.verification.capturer.Screenshot |
| `external-call::dep:harness.verification.interceptor.VisionInterceptor` | harness.verification.interceptor.VisionInterceptor |
| `external-call::dep:harness.verification.vision_agent.VisionVerdict` | harness.verification.vision_agent.VisionVerdict |
| `harness\verification\interceptor.py` | name, event, post_call, _is_screenshot_tool, VisionInterceptor |
| `tests\test_interceptor.py` | post_call, event |
| `tests\test_verification_interceptor.py` | test_error_event_skips_vision, _screenshot_event, test_failing_verdict_written_to_cache, mock_vision_agent, world_state, ... |

## Entry Points

- `tests\test_verification_interceptor.py::TestVisionInterceptorVisionScreenshot.test_vision_screenshot_error_event_skips`
- `tests\test_verification_interceptor.py::TestVisionInterceptorFailureTolerance.test_vision_failure_keeps_previous_verdict`

## Connected Communities

- **. +4 dirs · harness.state.models.WorldState** (12 cross-edges)
- **. +2 dirs · check** (5 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (2 cross-edges)
- **. +3 dirs · now** (1 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-162"
smart_context with task: "understand . +2 dirs · post_call · .", format: "gcx"
find_usages with id: "tests\test_verification_interceptor.py::TestVisionInterceptorVisionScreenshot.test_vision_screenshot_error_event_skips", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
