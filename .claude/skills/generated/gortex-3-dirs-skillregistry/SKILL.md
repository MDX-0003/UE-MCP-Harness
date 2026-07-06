---
name: gortex-3-dirs-skillregistry
description: "Work in the . +3 dirs · SkillRegistry area — 83 symbols across 8 files (91% cohesion)"
---

# . +3 dirs · SkillRegistry

83 symbols | 8 files | 91% cohesion

## When to Use

Use this skill when working on files in:
- ``
- `external-call::dep:harness.context.skill_registry.BUILTIN_SKILL_TEMPLATE`
- `external-call::dep:harness.context.skill_registry.SkillRegistry`
- `external-call::dep:harness.context.skill_registry.validate_skill`
- `external-call::stdlib:yaml`
- `harness\cli.py`
- `harness\context\skill_registry.py`
- `tests\test_skill.py`

## Key Files

| File | Symbols |
|------|---------|
| `` | startfile |
| `external-call::dep:harness.context.skill_registry.BUILTIN_SKILL_TEMPLATE` | harness.context.skill_registry.BUILTIN_SKILL_TEMPLATE |
| `external-call::dep:harness.context.skill_registry.SkillRegistry` | harness.context.skill_registry.SkillRegistry |
| `external-call::dep:harness.context.skill_registry.validate_skill` | harness.context.skill_registry.validate_skill |
| `external-call::stdlib:yaml` | yaml |
| `harness\cli.py` | args, _cmd_skill |
| `harness\context\skill_registry.py` | SkillRegistry, delete_skill, yaml_content, load_skill_yaml, name, ... |
| `tests\test_skill.py` | test_match_by_trigger, registry, registry, registry, tmp_path, ... |

## Connected Communities

- **. +3 dirs · from_env** (2 cross-edges)
- **. +2 dirs · _safe_filename** (2 cross-edges)
- **. +4 dirs · harness.config.Config** (1 cross-edges)
- **. +2 dirs · main** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-6"
smart_context with task: "understand . +3 dirs · SkillRegistry", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
