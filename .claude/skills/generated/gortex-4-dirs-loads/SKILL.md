---
name: gortex-4-dirs-loads
description: "Work in the . +4 dirs · loads area — 34 symbols across 8 files (73% cohesion)"
---

# . +4 dirs · loads

34 symbols | 8 files | 73% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.client.parse_sse_stream`
- `harness\observability\logger.py`
- `harness\observability\replay.py`
- `harness\server.py`
- `harness\state\refresher.py`
- `tests\test_client.py`
- `tests\tool_verify_level_persistence.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | loads |
| `external-call::dep:harness.client.parse_sse_stream` | harness.client.parse_sse_stream |
| `harness\observability\logger.py` | text, _format_output, text, _truncate, text, ... |
| `harness\observability\replay.py` | _load_jsonl, path |
| `harness\server.py` | result_text, _parse_raw_result |
| `harness\state\refresher.py` | cache, result, result, ue_client, _extract_level_path, ... |
| `tests\test_client.py` | test_line_without_colon, test_comment_lines_ignored, TestSseParser, test_single_event, test_empty_stream, ... |
| `tests\tool_verify_level_persistence.py` | _extract_text, result, main |

## Entry Points

- `tests\tool_verify_level_persistence.py::main`
- `harness\state\refresher.py::full_refresh`

## Connected Communities

- **. +2 dirs · main** (2 cross-edges)
- **. +3 dirs · now** (1 cross-edges)
- **. +4 dirs · harness.state.models.WorldState** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-169"
smart_context with task: "understand . +4 dirs · loads", format: "gcx"
find_usages with id: "tests\tool_verify_level_persistence.py::main", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
