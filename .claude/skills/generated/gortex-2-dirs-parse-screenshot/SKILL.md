---
name: gortex-2-dirs-parse-screenshot
description: "Work in the . +2 dirs · parse_screenshot area — 34 symbols across 5 files (82% cohesion)"
---

# . +2 dirs · parse_screenshot

34 symbols | 5 files | 82% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:PIL.Image`
- `harness\state\interceptor.py`
- `harness\verification\capturer.py`
- `harness\verification\interceptor.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | io, decode, struct, b64encode, unpack, ... |
| `external-call::dep:PIL.Image` | PIL.Image |
| `harness\state\interceptor.py` | text, _extract_actor_from_result |
| `harness\verification\capturer.py` | path, max_height, max_height, parse_screenshot, data, ... |
| `harness\verification\interceptor.py` | get_active_skill, cache, get_pending_screenshot, session_manager, vision_agent, ... |

## Connected Communities

- **. +2 dirs · _safe_filename** (2 cross-edges)
- **. +4 dirs · loads** (2 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-23"
smart_context with task: "understand . +2 dirs · parse_screenshot", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
