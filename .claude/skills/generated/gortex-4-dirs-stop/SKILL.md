---
name: gortex-4-dirs-stop
description: "Work in the . +4 dirs · stop area — 33 symbols across 6 files (74% cohesion)"
---

# . +4 dirs · stop

33 symbols | 6 files | 74% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.observability.logger.ToolCallLogger`
- `harness\cli.py`
- `harness\observability\logger.py`
- `harness\verification\session.py`
- `tests\test_observability.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | strftime, wait_for |
| `external-call::dep:harness.observability.logger.ToolCallLogger` | harness.observability.logger.ToolCallLogger |
| `harness\cli.py` | run |
| `harness\observability\logger.py` | event, start, args, _serialize_args, _short_name, ... |
| `harness\verification\session.py` | start, question |
| `tests\test_observability.py` | tmp_path, test_multiple_events, tmp_path, tmp_path, tmp_path, ... |

## Entry Points

- `tests\test_observability.py::TestToolCallLogger.test_multiple_events`
- `tests\test_observability.py::TestToolCallLogger.test_stop_flushes_queue`
- `tests\test_observability.py::TestToolCallLogger.test_post_call_writes_line`
- `tests\test_observability.py::TestToolCallLogger.test_long_output_truncated`
- `tests\test_observability.py::TestToolCallLogger.test_post_call_with_error`

## Connected Communities

- **. +3 dirs · now** (5 cross-edges)
- **. +4 dirs · loads** (5 cross-edges)
- **. +4 dirs · harness.state.models.WorldState** (5 cross-edges)
- **. +3 dirs · cmd_start** (2 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (1 cross-edges)
- **. +2 dirs · check** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-149"
smart_context with task: "understand . +4 dirs · stop", format: "gcx"
find_usages with id: "tests\test_observability.py::TestToolCallLogger.test_multiple_events", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
