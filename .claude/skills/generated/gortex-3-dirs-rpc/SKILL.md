---
name: gortex-3-dirs-rpc
description: "Work in the . +3 dirs · _rpc area — 55 symbols across 7 files (67% cohesion)"
---

# . +3 dirs · _rpc

55 symbols | 7 files | 67% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::stdlib:httpx`
- `harness\cli.py`
- `harness\client.py`
- `harness\observability\replay.py`
- `tests\test_client.py`
- `tests\test_client_health.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | getLogger |
| `external-call::stdlib:httpx` | httpx |
| `harness\cli.py` | _verify_level_persistence_tools, ue_client |
| `harness\client.py` | McpClientSession, load_one, raw, SseEvent, parse_sse_stream, ... |
| `harness\observability\replay.py` | run |
| `tests\test_client.py` | client, test_ping, client, client, client, ... |
| `tests\test_client_health.py` | _BadGatewayResponse, aread |

## Connected Communities

- **tests +2 dirs** (6 cross-edges)
- **. +4 dirs · loads** (3 cross-edges)
- **. +2 dirs · _read_sse_stream** (2 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (2 cross-edges)
- **. +3 dirs · cmd_start** (1 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-2"
smart_context with task: "understand . +3 dirs · _rpc", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
