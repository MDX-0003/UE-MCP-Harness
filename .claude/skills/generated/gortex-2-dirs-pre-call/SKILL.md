---
name: gortex-2-dirs-pre-call
description: "Work in the . +2 dirs · pre_call area — 24 symbols across 5 files (76% cohesion)"
---

# . +2 dirs · pre_call

24 symbols | 5 files | 76% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:harness.interceptor.DebugPreCallInterceptor`
- `external-call::dep:harness.interceptor.ToolCallInterceptor`
- `harness\interceptor.py`
- `harness\server.py`
- `tests\test_interceptor.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:harness.interceptor.DebugPreCallInterceptor` | harness.interceptor.DebugPreCallInterceptor |
| `external-call::dep:harness.interceptor.ToolCallInterceptor` | harness.interceptor.ToolCallInterceptor |
| `harness\interceptor.py` | post_call, DebugPreCallInterceptor, name, pre_call, name, ... |
| `harness\server.py` | interceptors |
| `tests\test_interceptor.py` | test_custom_interceptor, TestDebugPreCallInterceptor, test_pre_call_passthrough, pre_call, args, ... |

## Connected Communities

- **. +4 dirs · harness.state.models.WorldState** (2 cross-edges)

## How to Explore

```
get_communities with id: "community-142"
smart_context with task: "understand . +2 dirs · pre_call", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
