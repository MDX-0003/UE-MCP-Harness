---
name: gortex-2-dirs-harness-state-normalize-normali
description: "Work in the . +2 dirs · harness.state.normalize.normali… area — 71 symbols across 7 files (88% cohesion)"
---

# . +2 dirs · harness.state.normalize.normali…

71 symbols | 7 files | 88% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:harness.state.normalize.normalize_tool_args`
- `external-call::dep:harness.verification.session._format_write_description`
- `external-call::dep:harness.verification.session.get_recent_writes`
- `external-call::dep:harness.verification.session.record_write`
- `harness\state\interceptor.py`
- `tests\test_normalize.py`
- `tests\test_vision_session.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:harness.state.normalize.normalize_tool_args` | harness.state.normalize.normalize_tool_args |
| `external-call::dep:harness.verification.session._format_write_description` | harness.verification.session._format_write_description |
| `external-call::dep:harness.verification.session.get_recent_writes` | harness.verification.session.get_recent_writes |
| `external-call::dep:harness.verification.session.record_write` | harness.verification.session.record_write |
| `harness\state\interceptor.py` | cache, _handle_set_transform, event, cache, event, ... |
| `tests\test_normalize.py` | test_component_refpath_extracts_owner_actor, test_set_properties_refpath, test_load_level_no_actor, test_raw_args_preserved, test_set_label_refpath, ... |
| `tests\test_vision_session.py` | test_record_and_get, test_format_set_transform, test_format_remove_from_scene, test_deque_limit, TestRecentWrites, ... |

## Connected Communities

- **. +2 dirs · parse_screenshot** (1 cross-edges)
- **. +4 dirs · harness.state.models.WorldState** (1 cross-edges)
- **. +3 dirs · now** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-14"
smart_context with task: "understand . +2 dirs · harness.state.normalize.normali…", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
