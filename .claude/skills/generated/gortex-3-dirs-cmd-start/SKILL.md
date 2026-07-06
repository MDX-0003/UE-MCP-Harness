---
name: gortex-3-dirs-cmd-start
description: "Work in the . +3 dirs · cmd_start area — 28 symbols across 12 files (57% cohesion)"
---

# . +3 dirs · cmd_start

28 symbols | 12 files | 57% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.server.build_server`
- `external-call::dep:harness.state.hard_boundary.execute_hard_boundary`
- `external-call::dep:harness.transport.serve`
- `external-call::dep:harness.verification.capturer.close_shot_session`
- `external-call::dep:harness.verification.capturer.init_shot_session`
- `external-call::dep:harness.verification.debug.init`
- `external-call::dep:harness.verification.drift_alert.DriftAlertInterceptor`
- `harness\cli.py`
- `harness\client.py`
- `harness\observability\logger.py`
- `harness\verification\session.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | uuid4, set_event_loop, Queue, gather, uuid, ... |
| `external-call::dep:harness.server.build_server` | harness.server.build_server |
| `external-call::dep:harness.state.hard_boundary.execute_hard_boundary` | harness.state.hard_boundary.execute_hard_boundary |
| `external-call::dep:harness.transport.serve` | harness.transport.serve |
| `external-call::dep:harness.verification.capturer.close_shot_session` | harness.verification.capturer.close_shot_session |
| `external-call::dep:harness.verification.capturer.init_shot_session` | harness.verification.capturer.init_shot_session |
| `external-call::dep:harness.verification.debug.init` | harness.verification.debug.init |
| `external-call::dep:harness.verification.drift_alert.DriftAlertInterceptor` | harness.verification.drift_alert.DriftAlertInterceptor |
| `harness\cli.py` | cmd_start, args |
| `harness\client.py` | add_reconnect_hook, hook |
| `harness\observability\logger.py` | __init__, log_dir, get_verdict, get_screenshot_path, session_id |
| `harness\verification\session.py` | log_dir, set_log_dir |

## Entry Points

- `harness\cli.py::cmd_start`

## Connected Communities

- **. +3 dirs · _rpc** (8 cross-edges)
- **. +4 dirs · harness.config.Config** (4 cross-edges)
- **. +3 dirs · SkillRegistry** (3 cross-edges)
- **. +4 dirs · stop** (3 cross-edges)
- **. +2 dirs · post_call · external-call::dep:harness.observability.snapshotter** (2 cross-edges)
- **. +4 dirs · harness.state.models.WorldState** (2 cross-edges)
- **. +2 dirs · _read_sse_stream** (1 cross-edges)
- **. +1 dirs · serve** (1 cross-edges)
- **. +1 dirs · harness.verification.config.loa…** (1 cross-edges)
- **. +1 dirs · mgr** (1 cross-edges)
- **. +2 dirs · check** (1 cross-edges)
- **harness · _handle_sse** (1 cross-edges)
- **. +2 dirs · post_call · .** (1 cross-edges)
- **. +2 dirs · pre_call** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-1"
smart_context with task: "understand . +3 dirs · cmd_start", format: "gcx"
find_usages with id: "harness\cli.py::cmd_start", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
