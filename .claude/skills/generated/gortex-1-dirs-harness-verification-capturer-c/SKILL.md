---
name: gortex-1-dirs-harness-verification-capturer-c
description: "Work in the . +1 dirs · harness.verification.capturer.c… area — 31 symbols across 7 files (79% cohesion)"
---

# . +1 dirs · harness.verification.capturer.c…

31 symbols | 7 files | 79% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.client.JsonRpcError`
- `external-call::dep:harness.client.JsonRpcResponse`
- `external-call::dep:harness.verification.capturer._is_sse_no_result_error`
- `external-call::dep:harness.verification.capturer.capture`
- `tests\test_client.py`
- `tests\test_verification.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | run |
| `external-call::dep:harness.client.JsonRpcError` | harness.client.JsonRpcError |
| `external-call::dep:harness.client.JsonRpcResponse` | harness.client.JsonRpcResponse |
| `external-call::dep:harness.verification.capturer._is_sse_no_result_error` | harness.verification.capturer._is_sse_no_result_error |
| `external-call::dep:harness.verification.capturer.capture` | harness.verification.capturer.capture |
| `tests\test_client.py` | TestJsonRpc, test_success_response, test_error_response |
| `tests\test_verification.py` | tmp_path, test_no_shot_session_raises_runtime_error, monkeypatch, tmp_path, test_asset_empty_path_raises_value_error, ... |

## Entry Points

- `tests\test_verification.py::TestCaptureWithFileFallback.test_readtimeout_falls_back_to_file`
- `tests\test_verification.py::TestCaptureWithFileFallback.test_other_jsonrpc_error_reraises`
- `tests\test_verification.py::TestCaptureWithFileFallback.test_asset_mode_no_fallback`
- `tests\test_verification.py::TestCaptureWithFileFallback.test_jsonrpc_sse_no_result_falls_back`

## Connected Communities

- **. +2 dirs · MagicMock** (11 cross-edges)
- **tests +2 dirs** (5 cross-edges)
- **. +3 dirs · _rpc** (2 cross-edges)
- **. +1 dirs · _poll_and_capture** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-161"
smart_context with task: "understand . +1 dirs · harness.verification.capturer.c…", format: "gcx"
find_usages with id: "tests\test_verification.py::TestCaptureWithFileFallback.test_readtimeout_falls_back_to_file", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
