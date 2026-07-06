---
name: gortex-2-dirs-check
description: "Work in the . +2 dirs · check area — 53 symbols across 7 files (78% cohesion)"
---

# . +2 dirs · check

53 symbols | 7 files | 78% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.verification.vision_agent.VisionSubAgent`
- `external-call::dep:harness.verification.vision_agent._parse_verdict`
- `external-call::stdlib:anthropic`
- `harness\verification\session.py`
- `harness\verification\vision_agent.py`
- `tests\test_verification.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | isoformat |
| `external-call::dep:harness.verification.vision_agent.VisionSubAgent` | harness.verification.vision_agent.VisionSubAgent |
| `external-call::dep:harness.verification.vision_agent._parse_verdict` | harness.verification.vision_agent._parse_verdict |
| `external-call::stdlib:anthropic` | anthropic |
| `harness\verification\session.py` | screenshot_meta, add_screenshot, question, scene_context, ask, ... |
| `harness\verification\vision_agent.py` | extra_context, reset, question, call_count, config, ... |
| `tests\test_verification.py` | test_reset_clears_history, config, config, test_plain_json, test_json_in_markdown, ... |

## Entry Points

- `harness\verification\session.py::VisionSessionManager.ask`

## Connected Communities

- **. +3 dirs · now** (7 cross-edges)
- **. +2 dirs · parse_screenshot** (2 cross-edges)
- **harness\verification · build_full_prompt_context** (2 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (2 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)
- **. +4 dirs · stop** (1 cross-edges)
- **. +2 dirs · init_shot_session** (1 cross-edges)
- **. +4 dirs · loads** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-31"
smart_context with task: "understand . +2 dirs · check", format: "gcx"
find_usages with id: "harness\verification\session.py::VisionSessionManager.ask", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
