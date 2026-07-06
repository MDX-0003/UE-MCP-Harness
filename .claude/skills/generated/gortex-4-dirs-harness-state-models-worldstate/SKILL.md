---
name: gortex-4-dirs-harness-state-models-worldstate
description: "Work in the . +4 dirs · harness.state.models.WorldState area — 93 symbols across 28 files (81% cohesion)"
---

# . +4 dirs · harness.state.models.WorldState

93 symbols | 28 files | 81% cohesion

## When to Use

Use this skill when working on files in:
- `external-call::dep:harness.context.prompt.SystemContextProvider`
- `external-call::dep:harness.context.prompt.TaskContextProvider`
- `external-call::dep:harness.context.prompt.ToolReferenceProvider`
- `external-call::dep:harness.interceptor.ToolCallCompleted`
- `external-call::dep:harness.state.interceptor.StateCacheInterceptor`
- `external-call::dep:harness.state.interceptor._handle_add_tag`
- `external-call::dep:harness.state.interceptor._handle_add_to_scene`
- `external-call::dep:harness.state.interceptor._handle_load_level`
- `external-call::dep:harness.state.interceptor._handle_remove_from_scene`
- `external-call::dep:harness.state.interceptor._handle_remove_tag`
- `external-call::dep:harness.state.interceptor._handle_select_actors`
- `external-call::dep:harness.state.interceptor._handle_set_label`
- `external-call::dep:harness.state.interceptor._handle_set_properties`
- `external-call::dep:harness.state.interceptor._handle_set_transform`
- `external-call::dep:harness.state.models.ActorSnapshot`
- `external-call::dep:harness.state.models.WorldState`
- `external-call::dep:harness.verification.session.build_scene_context`
- `harness\context\prompt.py`
- `harness\interceptor.py`
- `harness\state\interceptor.py`
- `harness\state\models.py`
- `tests\test_context.py`
- `tests\test_interceptor.py`
- `tests\test_normalize.py`
- `tests\test_snapshotter.py`
- `tests\test_state.py`
- `tests\test_verification_interceptor.py`
- `tests\test_vision_session.py`

## Key Files

| File | Symbols |
|------|---------|
| `external-call::dep:harness.context.prompt.SystemContextProvider` | harness.context.prompt.SystemContextProvider |
| `external-call::dep:harness.context.prompt.TaskContextProvider` | harness.context.prompt.TaskContextProvider |
| `external-call::dep:harness.context.prompt.ToolReferenceProvider` | harness.context.prompt.ToolReferenceProvider |
| `external-call::dep:harness.interceptor.ToolCallCompleted` | harness.interceptor.ToolCallCompleted |
| `external-call::dep:harness.state.interceptor.StateCacheInterceptor` | harness.state.interceptor.StateCacheInterceptor |
| `external-call::dep:harness.state.interceptor._handle_add_tag` | harness.state.interceptor._handle_add_tag |
| `external-call::dep:harness.state.interceptor._handle_add_to_scene` | harness.state.interceptor._handle_add_to_scene |
| `external-call::dep:harness.state.interceptor._handle_load_level` | harness.state.interceptor._handle_load_level |
| `external-call::dep:harness.state.interceptor._handle_remove_from_scene` | harness.state.interceptor._handle_remove_from_scene |
| `external-call::dep:harness.state.interceptor._handle_remove_tag` | harness.state.interceptor._handle_remove_tag |
| `external-call::dep:harness.state.interceptor._handle_select_actors` | harness.state.interceptor._handle_select_actors |
| `external-call::dep:harness.state.interceptor._handle_set_label` | harness.state.interceptor._handle_set_label |
| `external-call::dep:harness.state.interceptor._handle_set_properties` | harness.state.interceptor._handle_set_properties |
| `external-call::dep:harness.state.interceptor._handle_set_transform` | harness.state.interceptor._handle_set_transform |
| `external-call::dep:harness.state.models.ActorSnapshot` | harness.state.models.ActorSnapshot |
| `external-call::dep:harness.state.models.WorldState` | harness.state.models.WorldState |
| `external-call::dep:harness.verification.session.build_scene_context` | harness.verification.session.build_scene_context |
| `harness\context\prompt.py` | _pie_str, state, active_skill, render, TaskContextProvider, ... |
| `harness\interceptor.py` | ToolCallCompleted |
| `harness\state\interceptor.py` | tool_name, _handle_delete_folder, event, _is_write_tool, cache, ... |
| `harness\state\models.py` | ActorSnapshot, WorldState |
| `tests\test_context.py` | test_empty_state, TestTaskContextProvider, test_skill_with_steps, test_populated_state, test_null_state_outputs_placeholder, ... |
| `tests\test_interceptor.py` | test_defaults, TestToolCallCompleted, test_error_field, test_basic_fields |
| `tests\test_normalize.py` | TestIntegration, test_full_write_chain_set_transform, test_full_write_chain_set_properties, test_dirty_actors_not_empty_with_refpath |
| `tests\test_snapshotter.py` | _error_event |
| `tests\test_state.py` | test_add_tag, test_post_call_error_skips_update, TestStateCacheInterceptor, test_set_transform, test_set_properties_merge, ... |
| `tests\test_verification_interceptor.py` | name, _error_event |
| `tests\test_vision_session.py` | TestBuildSceneContext, test_with_actors, test_dirty_actors_highlighted, test_question_mention_extraction, test_empty_world_state |

## Entry Points

- `harness\context\prompt.py::_render_state_snapshot`
- `tests\test_normalize.py::TestIntegration.test_dirty_actors_not_empty_with_refpath`

## Connected Communities

- **. +2 dirs · harness.state.normalize.normali…** (4 cross-edges)
- **. +3 dirs · now** (1 cross-edges)
- **. +3 dirs · build_server** (1 cross-edges)

## How to Explore

```
get_communities with id: "community-154"
smart_context with task: "understand . +4 dirs · harness.state.models.WorldState", format: "gcx"
find_usages with id: "harness\context\prompt.py::_render_state_snapshot", format: "gcx"
```

_`format: "gcx"` returns the [GCX1 compact wire format](../../docs/wire-format.md) — round-trippable, ~27% fewer tokens than JSON. Drop it for JSON output; agents using `@gortex/wire` or the Go `github.com/gortexhq/gcx-go` package decode either._
