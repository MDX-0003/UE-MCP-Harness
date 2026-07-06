---
name: gortex-harness-observability-1-dirs
description: "Work in the harness\observability +1 dirs area — 24 symbols across 2 files (70% cohesion)"
---

# harness\observability +1 dirs

24 symbols | 2 files | 70% cohesion

## When to Use

Use this skill when working on files in:
- `harness\observability\snapshotter.py`
- `tests\test_snapshotter.py`

## Key Files

| File | Symbols |
|------|---------|
| `harness\observability\snapshotter.py` | _ensure_dir, _short_name, SnapshotRecorder, skill_name, yaml_text, ... |
| `tests\test_snapshotter.py` | test_skill_activated_saves_yaml, world_state, world_state, snapshot_dir, test_skill_deactivated_writes_marker, ... |

## Connected Communities

- **. +2 dirs · harness.client._extract_text_fr…** (2 cross-edges)
- **. +3 dirs · now** (2 cross-edges)
- **. +2 dirs · post_call · external-call::dep:harness.observability.snapshotter** (2 cross-edges)
- **. +2 dirs · _safe_filename** (1 cross-edges)
- **. +2 dirs · check** (1 cross-edges)
- **. +4 dirs · stop** (1 cross-edges)
- **. +2 dirs · parse_screenshot** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-8"
smart_context with task: "understand harness\observability +1 dirs", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
