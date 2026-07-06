---
name: gortex-3-dirs-now
description: "Work in the . +3 dirs · now area — 41 symbols across 8 files (72% cohesion)"
---

# . +3 dirs · now

41 symbols | 8 files | 72% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.verification.session.ContextBlock`
- `external-call::dep:harness.verification.session.ScreenshotRef`
- `external-call::dep:harness.verification.session.VisionSession`
- `external-call::dep:harness.verification.session.build_full_prompt_context`
- `harness\observability\snapshotter.py`
- `harness\verification\session.py`
- `tests\test_vision_session.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | now |
| `external-call::dep:harness.verification.session.ContextBlock` | harness.verification.session.ContextBlock |
| `external-call::dep:harness.verification.session.ScreenshotRef` | harness.verification.session.ScreenshotRef |
| `external-call::dep:harness.verification.session.VisionSession` | harness.verification.session.VisionSession |
| `external-call::dep:harness.verification.session.build_full_prompt_context` | harness.verification.session.build_full_prompt_context |
| `harness\observability\snapshotter.py` | snapshot_dir, get_pending_screenshot, __init__, cache |
| `harness\verification\session.py` | touch, VisionSessionManager, info, log_dir, reset, ... |
| `tests\test_vision_session.py` | test_includes_manual_tell, test_create_session, TestVisionSession, test_reset_writes_archive_json, TestBuildFullPromptContext, ... |

## Entry Points

- `harness\verification\session.py::VisionSessionManager.status_text`
- `tests\test_vision_session.py::TestVisionSessionManager.test_reset_writes_archive_json`
- `tests\test_vision_session.py::TestVisionSession.test_add_screenshots`

## Connected Communities

- **. +4 dirs · harness.config.Config** (5 cross-edges)
- **. +2 dirs · check** (2 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (1 cross-edges)
- **. +4 dirs · stop** (1 cross-edges)
- **. +2 dirs · harness.state.normalize.normali…** (1 cross-edges)
- **. +4 dirs · loads** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-27"
smart_context with task: "understand . +3 dirs · now", format: "gcx"
find_usages with id: "harness\verification\session.py::VisionSessionManager.status_text", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
