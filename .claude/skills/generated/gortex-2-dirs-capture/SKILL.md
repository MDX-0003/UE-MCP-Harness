---
name: gortex-2-dirs-capture
description: "Work in the . +2 dirs · capture area — 31 symbols across 3 files (79% cohesion)"
---

# . +2 dirs · capture

31 symbols | 3 files | 79% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:harness.verification.debug.log_exception`
- `harness\verification\capturer.py`
- `tests\test_verification.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:harness.verification.debug.log_exception` | harness.verification.debug.log_exception |
| `harness\verification\capturer.py` | _capture_asset_image_with_file_fallback, _is_sse_no_result_error, asset_path, max_height, max_width, ... |
| `tests\test_verification.py` | run, run, run, run, run, ... |

## Entry Points

- `harness\verification\capturer.py::capture`

## Connected Communities

- **. +2 dirs · parse_screenshot** (3 cross-edges)
- **. +1 dirs · _poll_and_capture** (2 cross-edges)
- **. +1 dirs · _activate_ue_window** (2 cross-edges)

## How to Explore

```
get_communities with id: "community-22"
smart_context with task: "understand . +2 dirs · capture", format: "gcx"
find_usages with id: "harness\verification\capturer.py::capture", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
