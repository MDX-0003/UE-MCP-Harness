---
name: gortex-2-dirs-main
description: "Work in the . +2 dirs · main area — 25 symbols across 9 files (75% cohesion)"
---

# . +2 dirs · main

25 symbols | 9 files | 75% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:mcp.ClientSession`
- `external-call::dep:mcp.client.sse.sse_client`
- `external-call::dep:mcp.client.streamable_http.streamablehttp_client`
- `harness\server.py`
- `tests\test_l3_e2e.py`
- `tests\tool_probe_ue.py`
- `tests\tool_verify_harness_passthrough.py`
- `tests\tool_verify_harness_vision.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | sleep, home |
| `external-call::dep:mcp.ClientSession` | mcp.ClientSession |
| `external-call::dep:mcp.client.sse.sse_client` | mcp.client.sse.sse_client |
| `external-call::dep:mcp.client.streamable_http.streamablehttp_client` | mcp.client.streamable_http.streamablehttp_client |
| `harness\server.py` | list_tools, _rebuild_tool_reference |
| `tests\test_l3_e2e.py` | main, result, _extract_text |
| `tests\tool_probe_ue.py` | main, result, _extract_text |
| `tests\tool_verify_harness_passthrough.py` | main |
| `tests\tool_verify_harness_vision.py` | text, extra, _has_vision_verdict, _print_vision_lines, _test_mode, ... |

## Entry Points

- `tests\test_l3_e2e.py::main`
- `tests\tool_probe_ue.py::main`
- `tests\tool_verify_harness_passthrough.py::main`
- `tests\tool_verify_harness_vision.py::main`

## Connected Communities

- **tests · _find_asset** (2 cross-edges)

## How to Explore

```
get_communities with id: "community-168"
smart_context with task: "understand . +2 dirs · main", format: "gcx"
find_usages with id: "tests\test_l3_e2e.py::main", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
