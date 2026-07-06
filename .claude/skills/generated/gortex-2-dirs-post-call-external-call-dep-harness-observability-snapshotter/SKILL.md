---
name: gortex-2-dirs-post-call-external-call-dep-harness-observability-snapshotter
description: "Work in the . +2 dirs · post_call · external-call::dep:harness.observability.snapshotter area — 56 symbols across 4 files (77% cohesion)"
---

# . +2 dirs · post_call · external-call::dep:harness.observability.snapshotter

56 symbols | 4 files | 77% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:harness.observability.snapshotter.SnapshotRecorder`
- `external-call::dep:harness.verification.interceptor._is_screenshot_tool`
- `harness\observability\snapshotter.py`
- `tests\test_snapshotter.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:harness.observability.snapshotter.SnapshotRecorder` | harness.observability.snapshotter.SnapshotRecorder |
| `external-call::dep:harness.verification.interceptor._is_screenshot_tool` | harness.verification.interceptor._is_screenshot_tool |
| `harness\observability\snapshotter.py` | write_session_json, post_call, event |
| `tests\test_snapshotter.py` | snapshot_dir, world_state, world_state, world_state, test_screenshot_saves_png, ... |

## Entry Points

- `tests\test_snapshotter.py::TestSnapshotRecorderBasic.test_context_saves_text_and_state`
- `tests\test_snapshotter.py::TestSnapshotRecorderVisionScreenshot.test_vision_screenshot_with_verdict`
- `tests\test_snapshotter.py::TestSnapshotRecorderVerdict.test_verdict_saved_when_present`
- `tests\test_snapshotter.py::TestSnapshotRecorderVisionScreenshot.test_vision_screenshot_saves_png`
- `tests\test_snapshotter.py::TestSnapshotRecorderBasic.test_screenshot_saves_png`

## Connected Communities

- **. +4 dirs · harness.state.models.WorldState** (10 cross-edges)
- **. +4 dirs · loads** (2 cross-edges)
- **. +2 dirs · post_call · .** (2 cross-edges)
- **harness\observability +1 dirs** (2 cross-edges)
- **. +3 dirs · now** (1 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (1 cross-edges)
- **. +2 dirs · check** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-152"
smart_context with task: "understand . +2 dirs · post_call · external-call::dep:harness.observability.snapshotter", format: "gcx"
find_usages with id: "tests\test_snapshotter.py::TestSnapshotRecorderBasic.test_context_saves_text_and_state", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
