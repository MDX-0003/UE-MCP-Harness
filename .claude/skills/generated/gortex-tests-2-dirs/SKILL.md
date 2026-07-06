---
name: gortex-tests-2-dirs
description: "Work in the tests +2 dirs area — 31 symbols across 4 files (73% cohesion)"
---

# tests +2 dirs

31 symbols | 4 files | 73% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `harness\client.py`
- `tests\test_client_health.py`
- `tests\test_verification.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | timeout, unittest.mock.AsyncMock, unittest.mock.patch, patch, AsyncMock |
| `harness\client.py` | timeout, _ensure_connected, ping, reconnect |
| `tests\test_client_health.py` | test_ping_returns_true_when_ue_alive, test_hooks_called_after_reconnect, test_hook_failure_does_not_block_others, test_hook_failure_does_not_block_caller, test_ping_returns_true_on_404, ... |
| `tests\test_verification.py` | _setup_shot_client |

## Entry Points

- `tests\test_client_health.py::TestReconnectHooks.test_hooks_called_after_reconnect`
- `tests\test_client_health.py::TestReconnectHooks.test_hooks_executed_in_registration_order`
- `tests\test_client_health.py::TestEnsureConnected.test_reconnect_path_ping_success`
- `tests\test_client_health.py::TestReconnectHooks.test_hook_failure_does_not_block_others`

## Connected Communities

- **. +2 dirs · post_call · .** (11 cross-edges)
- **. +3 dirs · _rpc** (3 cross-edges)
- **. +2 dirs · MagicMock** (3 cross-edges)
- **. +2 dirs · _read_sse_stream** (1 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-138"
smart_context with task: "understand tests +2 dirs", format: "gcx"
find_usages with id: "tests\test_client_health.py::TestReconnectHooks.test_hooks_called_after_reconnect", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
