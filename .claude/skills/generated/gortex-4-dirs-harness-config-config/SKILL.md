---
name: gortex-4-dirs-harness-config-config
description: "Work in the . +4 dirs · harness.config.Config area — 71 symbols across 12 files (74% cohesion)"
---

# . +4 dirs · harness.config.Config

71 symbols | 12 files | 74% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.config.Config`
- `external-call::dep:harness.observability.replay.cmd_replay`
- `external-call::dep:harness.observability.stats.cmd_stats`
- `external-call::dep:harness.verification.capturer.capture_from_file`
- `harness\cli.py`
- `harness\config.py`
- `harness\observability\replay.py`
- `harness\verification\config.py`
- `harness\verification\debug.py`
- `tests\test_config.py`
- `tests\test_verification.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | basicConfig, ArgumentParser, Path, logging, argparse |
| `external-call::dep:harness.config.Config` | harness.config.Config |
| `external-call::dep:harness.observability.replay.cmd_replay` | harness.observability.replay.cmd_replay |
| `external-call::dep:harness.observability.stats.cmd_stats` | harness.observability.stats.cmd_stats |
| `external-call::dep:harness.verification.capturer.capture_from_file` | harness.verification.capturer.capture_from_file |
| `harness\cli.py` | level, main, args, args, _cmd_replay, ... |
| `harness\config.py` | ue_host, merge_cli_overrides, ue_base_url, ue_port, ue_project_root, ... |
| `harness\observability\replay.py` | ue_port, cmd_replay, log_file |
| `harness\verification\config.py` | create_vision_env_template, project_root |
| `harness\verification\debug.py` | config, init |
| `tests\test_config.py` | TestConfigFromEnv, test_ue_screenshot_dir_from_env, monkeypatch, test_default_ue_port, test_empty_env_yields_none, ... |
| `tests\test_verification.py` | test_base_url_default_when_not_set, test_default_base_url, monkeypatch, test_api_key_field_exists, TestVisionConfigField, ... |

## Entry Points

- `harness\cli.py::main`
- `harness\observability\replay.py::cmd_replay`

## Connected Communities

- **. +3 dirs · _rpc** (7 cross-edges)
- **. +1 dirs · harness.verification.capturer.c…** (3 cross-edges)
- **. +2 dirs · harness.client._extract_text_fr…** (2 cross-edges)
- **. +2 dirs · _read_sse_stream** (2 cross-edges)
- **. +2 dirs · check** (2 cross-edges)
- **. +3 dirs · SkillRegistry** (1 cross-edges)
- **. +1 dirs · harness.verification.config.loa…** (1 cross-edges)
- **harness · _handle_sse** (1 cross-edges)
- **. +3 dirs · cmd_start** (1 cross-edges)
- **. +4 dirs · loads** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-7"
smart_context with task: "understand . +4 dirs · harness.config.Config", format: "gcx"
find_usages with id: "harness\cli.py::main", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
