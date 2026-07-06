---
name: gortex-3-dirs-build-server
description: "Work in the . +3 dirs · build_server area — 70 symbols across 11 files (87% cohesion)"
---

# . +3 dirs · build_server

70 symbols | 11 files | 87% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.context.filter.apply_filter`
- `external-call::dep:harness.context.filter.is_escape_hatch`
- `external-call::dep:harness.context.prompt.assemble_system_prompt`
- `external-call::dep:mcp.server.Server`
- `external-call::dep:mcp.types.CallToolResult`
- `external-call::dep:mcp.types.TextContent`
- `external-call::dep:mcp.types.Tool`
- `harness\context\provider.py`
- `harness\server.py`
- `tests\test_context.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | monotonic |
| `external-call::dep:harness.context.filter.apply_filter` | harness.context.filter.apply_filter |
| `external-call::dep:harness.context.filter.is_escape_hatch` | harness.context.filter.is_escape_hatch |
| `external-call::dep:harness.context.prompt.assemble_system_prompt` | harness.context.prompt.assemble_system_prompt |
| `external-call::dep:mcp.server.Server` | mcp.server.Server |
| `external-call::dep:mcp.types.CallToolResult` | mcp.types.CallToolResult |
| `external-call::dep:mcp.types.TextContent` | mcp.types.TextContent |
| `external-call::dep:mcp.types.Tool` | mcp.types.Tool |
| `harness\context\provider.py` | state, render, active_skill, ContextProvider |
| `harness\server.py` | pending_screenshot_ref, build_server, vision_session_manager, _log_harness_call, world_state, ... |
| `tests\test_context.py` | skill, state, test_is_escape_hatch, render, render, ... |

## Entry Points

- `harness\server.py::build_server`

## Connected Communities

- **. +3 dirs · SkillRegistry** (9 cross-edges)
- **. +4 dirs · harness.state.models.WorldState** (5 cross-edges)
- **. +2 dirs · pre_call** (5 cross-edges)
- **. +3 dirs · _rpc** (4 cross-edges)
- **. +1 dirs · harness.context.skill_registry.…** (2 cross-edges)
- **. +3 dirs · cmd_start** (1 cross-edges)
- **. +2 dirs · main** (1 cross-edges)
- **. +1 dirs · harness.verification.capturer.c…** (1 cross-edges)
- **. +2 dirs · capture** (1 cross-edges)
- **. +4 dirs · loads** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-11"
smart_context with task: "understand . +3 dirs · build_server", format: "gcx"
find_usages with id: "harness\server.py::build_server", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
