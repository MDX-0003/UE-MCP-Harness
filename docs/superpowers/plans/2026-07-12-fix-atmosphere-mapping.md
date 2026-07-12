# Fix build_atmosphere_mapping & Harness Tool Chain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 7 confirmed defects across 3 layers: parse layer (`_extract_actor_names` doesn't unwrap MCP content format), parameter layer (`list_properties` uses wrong param + actor-level vs component-level), and experience layer (L2 readback false positives, dead-loop hints).

**Architecture:** Layer 1 fixes the root cause — `_extract_actor_names` crashes on real MCP responses because it treats `{"content":[{"type":"text","text":"..."}]}` as a flat dict. Layer 2 fixes parameter contracts and property depth. Layer 3 fixes polish issues. Layers are sequential — each builds on the previous.

**Tech Stack:** Python 3.12+, pytest + pytest-asyncio, mcp SDK

---

## File Structure

| File | Responsibility | Change |
|------|---------------|--------|
| `harness/server.py` | `_extract_actor_names`, `build_atmosphere_mapping` handler, `match_reference` hint | Modify |
| `harness/verification/interceptor.py` | `_diff_properties`, `_values_equal` — epsilon threading | Modify |
| `tests/test_build_atmosphere_mapping.py` | Update mock to match real MCP wire format | Modify |

---

### Task 1: Fix `_extract_actor_names` — unwrap MCP `content` array

**Files:**
- Modify: `harness/server.py:1221-1236`

**Root Cause:** `ue_client.call_tool()` returns `{"content": [{"type": "text", "text": "{\"returnValue\": [...]}"}]}`. `_extract_actor_names` looks for `returnValue` in the top-level dict → never finds it → fallback path (`str(parsed)` → split by `\n` → filter lines starting with `{`) returns `[]`.

**Fix:** Add MCP `content` array unwrapping at the top of `_extract_actor_names`, matching the pattern already used by `_unwrap_mcp_text` in `harness/verification/interceptor.py:311` and `_parse_actor_list` in `harness/state/refresher.py:83`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_atmosphere_mapping.py — add to existing test class or new test

def test_extract_actor_names_unwraps_mcp_content():
    """_extract_actor_names should unwrap MCP content array to find returnValue."""
    from harness.server import _extract_actor_names

    # Simulate real ue_client.call_tool() return value
    mcp_wrapped = {
        "content": [
            {
                "type": "text",
                "text": '{"returnValue": [{"refPath": "/Game/DirLight"}, {"refPath": "/Game/SkyAtmo"}]}'
            }
        ]
    }
    result = _extract_actor_names(mcp_wrapped)
    assert result == ["/Game/DirLight", "/Game/SkyAtmo"], f"got: {result}"


def test_extract_actor_names_unwraps_empty_list():
    """Empty returnValue should give empty list, not garbage."""
    from harness.server import _extract_actor_names

    mcp_wrapped = {
        "content": [
            {"type": "text", "text": '{"returnValue": []}'}
        ]
    }
    result = _extract_actor_names(mcp_wrapped)
    assert result == [], f"got: {result}"
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_extract_actor_names_unwraps_mcp_content -v`
Expected: FAIL with `AssertionError`

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_extract_actor_names_unwraps_mcp_content tests/test_build_atmosphere_mapping.py::test_extract_actor_names_unwraps_empty_list -v`
Expected: 2 FAILED

- [ ] **Step 3: Fix `_extract_actor_names`**

Replace the function at `harness/server.py:1221-1236`:

```python
def _extract_actor_names(parsed: Any) -> list[str]:
    """从 find_actors 返回值中提取 actor 名称列表.

    处理两种格式：
      1. MCP content 包裹: {"content": [{"type": "text", "text": "..."}]}
         其中 text 内层含 {"returnValue": [...]}
      2. 直接 dict: {"returnValue": [...]}  (向后兼容旧测试 mock)
    """
    # ---- 解包 MCP content 数组 ----
    if isinstance(parsed, dict):
        content = parsed.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        try:
                            inner = json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # 递归处理内层（可能是 returnValue 包装或直接列表）
                        if isinstance(inner, dict) and "returnValue" in inner:
                            rv = inner["returnValue"]
                            if isinstance(rv, list):
                                return [_item_to_name(item) for item in rv if item]
                        if isinstance(inner, list):
                            return [_item_to_name(item) for item in inner if item]
    # ---- 向后兼容：直接 dict/list 格式 ----
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    if isinstance(parsed, dict):
        for key in ("actors", "result", "data"):
            val = parsed.get(key)
            if isinstance(val, list):
                return [_item_to_name(item) for item in val if item]
        rv = parsed.get("returnValue")
        if isinstance(rv, list):
            return [_item_to_name(item) for item in rv if item]
    # ---- 最终 fallback ----
    text = str(parsed)
    lines = text.strip().split("\n")
    return [line.strip() for line in lines if line.strip()
            and not line.startswith("{")]
```

- [ ] **Step 4: Run tests to verify fix**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all existing tests + 2 new tests PASS

- [ ] **Step 5: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "fix: _extract_actor_names unwrap MCP content array to find returnValue

Previously _extract_actor_names looked for 'returnValue' in the top-level
dict, but ue_client.call_tool() returns MCP CallToolResult format:
{\"content\": [{\"type\": \"text\", \"text\": \"...\"}]}.
The returnValue was nested inside content[0].text, never found,
causing build_atmosphere_mapping to report all atmosphere components
as 'not found' regardless of actual scene state.

Add content-array unwrapping matching the pattern already used by
_unwrap_mcp_text and _parse_actor_list."
```

---

### Task 2: Fix test mock to match real MCP wire format

**Files:**
- Modify: `tests/test_build_atmosphere_mapping.py:108-134`

**Why:** The test mock short-circuits the MCP protocol by returning `{"returnValue": [...]}` directly. After Task 1, `_extract_actor_names` handles both formats, but the mock should reflect reality so future changes don't regress.

- [ ] **Step 1: Update the mock `_mock_call_tool` return format**

Replace the `find_actors` return in the mock (line ~108-112):

```python
# Before:
# return json.dumps({"returnValue": _actor_reply(class_ref)})

# After:
if name in (_FIND, _FIND_SHORT):
    class_ref = args.get("actor_type", {}).get("refPath", "")
    inner = json.dumps({"returnValue": _actor_reply(class_ref)})
    return json.dumps({
        "content": [{"type": "text", "text": inner}]
    })
```

And replace the `list_properties` return (line ~125-130):

```python
# Before:
# return json.dumps({"content": [{"type": "text", "text": props_text}]})

# Keep as-is — list_properties already uses content format in mock
```

- [ ] **Step 2: Update `_mock_call_tool` for `list_properties` to use `instance` parameter**

The mock currently reads `args.get("actor_name")`. Change to match real UE API:

```python
elif name in (_LIST, _LIST_SHORT):
    instance = args.get("instance", {})
    actor_path = instance.get("refPath", "") if isinstance(instance, dict) else str(instance)
    prop_key = ""
    if "DirLight" in actor_path:
        prop_key = "DirLight"
    elif "SkyAtmo" in actor_path:
        prop_key = "SkyAtmo"
    elif "Fog" in actor_path:
        prop_key = "Fog"
    elif "Cloud" in actor_path:
        prop_key = "Cloud"
    props_text = _component_properties.get(
        prop_key,
        "LightColor: FLinearColor\nIntensity: float\nTemperature: float\n",
    )
    return json.dumps({
        "content": [{"type": "text", "text": props_text}]
    })
```

- [ ] **Step 3: Run all tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_build_atmosphere_mapping.py
git commit -m "test: update mock to use real MCP content format and instance param

find_actors mock now returns MCP CallToolResult format with content array.
list_properties mock reads 'instance' param (matching real UE ObjectTools API)
instead of the non-existent 'actor_name'."
```

---

### Task 3: Fix `list_properties` parameter name in `build_atmosphere_mapping`

**Files:**
- Modify: `harness/server.py:889`

**Root Cause:** Handler passes `{"actor_name": actor_name}` but UE ObjectTools (`list_properties`, `get_properties`, `set_properties`) all use `{"instance": {"refPath": "..."}}`. Confirmed by `normalize_tool_args` which only looks for `instance` and `actor` keys, never `actor_name`.

- [ ] **Step 1: Fix the parameter**

In `harness/server.py:889`, change:

```python
# Before:
{"actor_name": actor_name},

# After:
{"instance": {"refPath": actor_name}},
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all tests PASS (mock was updated in Task 2 to use `instance`)

- [ ] **Step 3: Commit**

```bash
git add harness/server.py
git commit -m "fix: use 'instance' param for list_properties, not 'actor_name'

UE ObjectTools (list_properties/get_properties/set_properties) accept
{instance: {refPath: ...}}, not {actor_name: ...}. Confirmed by
normalize_tool_args which only handles 'instance' and 'actor' keys."
```

---

### Task 4: Add component-level property resolution

**Files:**
- Modify: `harness/server.py:885-901` (Step 2 of build_atmosphere_mapping)
- Modify: `tests/test_build_atmosphere_mapping.py` (mock data + assertions)

**Root Cause:** `list_properties` on an actor (e.g. `DirectionalLight_0`) returns actor-top-level fields like `directionalLightComponent` (a component refPath), `primaryActorTick`, `bHidden`. The actual atmosphere properties (`intensity`, `lightColor`, `fogDensity`) live on the **component sub-objects** (e.g. `DirectionalLight_0.LightComponent0`). The handler never descends into components.

**Fix:** After Step 2 gets actor-level properties, detect which fields are component refPaths by calling `get_properties` on the actor to resolve actual refPath values, then recursively call `list_properties` on those component refPaths and merge into `all_properties`.

- [ ] **Step 1: Add helper to resolve component refPaths**

Add after `_extract_property_names` in `harness/server.py` (~line 1258):

```python
async def _resolve_component_refpaths(
    ue_client: "McpClientSession",
    actor_path: str,
    actor_prop_names: list[str],
) -> tuple[list[str], dict[str, str]]:
    """从 actor 属性中分辨 component 引用字段，解析其 refPath。

    Returns:
        (direct_props, component_refpaths)
        - direct_props: 非 component 引用的属性名（如 primaryActorTick, bHidden）
        - component_refpaths: {field_name: component_refpath} 映射
    """
    # 疑似 component 引用的字段名特征：以 Component 结尾，或匹配已知模式
    COMPONENT_FIELD_PATTERNS = (
        "Component", "component", "LightComponent",
        "FogComponent", "CloudComponent", "AtmosphereComponent",
    )
    suspect_fields = [
        p for p in actor_prop_names
        if any(pat in p for pat in COMPONENT_FIELD_PATTERNS)
    ]
    if not suspect_fields:
        return (actor_prop_names, {})

    # 调 get_properties 解析这些字段的实际值（refPath）
    try:
        result_text = await ue_client.call_tool(
            "toolset_registry.toolsets.core.object.ObjectTools.get_properties",
            {"instance": {"refPath": actor_path}, "properties": suspect_fields},
        )
        parsed = _parse_raw_result(result_text)
        # 从 MCP content 解包
        text = _extract_parsed_text(parsed, result_text)
        if text:
            rv = _unwrap_return_value(text)
            if rv is None:
                try:
                    rv = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    rv = {}
        else:
            rv = {}
    except Exception:
        return (actor_prop_names, {})

    component_refs: dict[str, str] = {}
    direct_props: list[str] = []

    for name in actor_prop_names:
        if name in suspect_fields and isinstance(rv, dict):
            val = rv.get(name)
            if isinstance(val, dict) and val.get("refPath"):
                component_refs[name] = val["refPath"]
                continue
        direct_props.append(name)

    return (direct_props, component_refs)
```

Note: need to import `_unwrap_return_value` at the top of the usage site, or inline the logic. Since `_unwrap_return_value` is in `harness/verification/interceptor.py`, add a local helper instead:

```python
def _try_unwrap_return_value(text: str) -> dict | None:
    """尝试解包 returnValue JSON 包装."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and "returnValue" in parsed:
        rv = parsed["returnValue"]
        if isinstance(rv, str):
            try:
                inner = json.loads(rv)
                return inner if isinstance(inner, dict) else None
            except (json.JSONDecodeError, TypeError):
                return None
        if isinstance(rv, dict):
            return rv
    return None
```

- [ ] **Step 2: Modify Step 2 of `build_atmosphere_mapping` to recurse into components**

Replace the loop at `harness/server.py:881-901`:

```python
            # Step 2: 获取属性名（含 component 子对象递归）
            for actor_type, actor_names in actors_found.items():
                if not actor_names:
                    continue
                all_properties[actor_type] = {}
                for actor_name in actor_names[:1]:
                    try:
                        # 2a. 获取 actor 顶层属性名
                        props_result = await ue_client.call_tool(
                            "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                            {"instance": {"refPath": actor_name}},
                        )
                        props_parsed = _parse_raw_result(props_result)
                        props_text = _extract_parsed_text(
                            props_parsed, props_result,
                        )
                        actor_prop_names = _extract_property_names(props_text)

                        # 2b. 解析 component 引用
                        direct_props, component_refs = (
                            await _resolve_component_refpaths(
                                ue_client, actor_name, actor_prop_names,
                            )
                        )

                        # 2c. 对每个 component 递归获取属性
                        all_names = list(direct_props)
                        for comp_field, comp_refpath in component_refs.items():
                            try:
                                comp_result = await ue_client.call_tool(
                                    "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                                    {"instance": {"refPath": comp_refpath}},
                                )
                                comp_parsed = _parse_raw_result(comp_result)
                                comp_text = _extract_parsed_text(
                                    comp_parsed, comp_result,
                                )
                                comp_names = _extract_property_names(comp_text)
                                all_names.extend(comp_names)
                            except Exception as e:
                                logger.warning(
                                    "获取 component %s 属性失败: %s",
                                    comp_refpath, e,
                                )

                        all_properties[actor_type][actor_name] = all_names
                    except Exception as e:
                        logger.warning(
                            "获取 %s 属性列表失败: %s", actor_name, e,
                        )
                        all_properties[actor_type][actor_name] = []
```

- [ ] **Step 3: Update test mock data to include component structure**

In `tests/test_build_atmosphere_mapping.py`, update the `_mock_call_tool` to handle `get_properties` for component refPath resolution. Add a `_component_refpath_data` dict and extend the mock:

```python
# Add component refPath mock data (after _component_properties)
_component_refpaths: dict[str, dict] = {
    "DirLight": {
        "directionalLightComponent": {"refPath": "/Game/DirLight.LightComponent0"},
        "lightComponent": {"refPath": "/Game/DirLight.LightComponent0"},
    },
    "SkyAtmo": {
        "skyAtmosphereComponent": {"refPath": "/Game/SkyAtmo.SkyAtmosphereComponent"},
    },
    "Fog": {
        "component": {"refPath": "/Game/Fog.HeightFogComponent0"},
    },
    "Cloud": {
        "volumetricCloudComponent": {"refPath": "/Game/Cloud.VolumetricCloudComponent"},
    },
}

# Extend _mock_call_tool to handle get_properties for component ref resolution
_GET = "toolset_registry.toolsets.core.object.ObjectTools.get_properties"
_GET_SHORT = "get_properties"

async def _mock_call_tool(name: str, args: dict) -> str:
    # ... existing find_actors and list_properties handling ...
    if name in (_GET, _GET_SHORT):
        instance = args.get("instance", {})
        actor_path = instance.get("refPath", "") if isinstance(instance, dict) else str(instance)
        prop_key = ""
        if "DirLight" in actor_path:
            prop_key = "DirLight"
        elif "SkyAtmo" in actor_path:
            prop_key = "SkyAtmo"
        elif "Fog" in actor_path:
            prop_key = "Fog"
        elif "Cloud" in actor_path:
            prop_key = "Cloud"
        ref_data = _component_refpaths.get(prop_key, {})
        inner = json.dumps(ref_data)
        return json.dumps({
            "content": [{"type": "text", "text": json.dumps({"returnValue": inner})}]
        })
```

- [ ] **Step 4: Update assertions in `test_uses_correct_tool_names`**

The test currently expects 4 `list_properties` calls (one per actor type). With component recursion, it should expect additional `list_properties` calls for components + `get_properties` calls for component refPath resolution. Update the assertion:

```python
# After component recursion is added, list_properties is called:
# - 4 times for actors
# - N times for components (one per actor that has components)
# get_properties is called 4 times (once per actor to resolve component refs)
list_calls_count = len([c for c in tool_names if c == _LIST])
get_calls_count = len([c for c in tool_names if c in (_GET, "get_properties")])
assert list_calls_count >= 4, (
    f"至少 4 次 list_properties（actor 级），实际 {list_calls_count} 次"
)
assert get_calls_count >= 4, (
    f"至少 4 次 get_properties（解析 component refPath），实际 {get_calls_count} 次"
)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: resolve component-level properties in build_atmosphere_mapping

After listing actor-top-level properties, detect component reference fields
(e.g. lightComponent → LightComponent0 refPath) and recursively list
properties on each component. Atmosphere properties (intensity, lightColor,
fogDensity, etc.) live on components, not actors — without this step the
mapping was useless for LLM-guided atmosphere adjustment."
```

---

### Task 5: Thread epsilon through `_diff_properties` to `_values_equal`

**Files:**
- Modify: `harness/verification/interceptor.py:394-472`

**Root Cause:** `ReadbackInterceptor` is configured with `epsilon=5e-3` for color value tolerance, but `_diff_values` calls `_diff_properties(intent, actual)` without passing epsilon. `_diff_properties` calls `_values_equal` which has a hardcoded `1e-6` tolerance — 1000× too tight for UE FLinearColor round-trip precision (e.g. `0.88 → 0.87843143`, diff `0.00157 > 1e-6`). Additionally, `_diff_properties` only handles ONE level of nesting — `settings.colorSaturation.x` fails because `colorSaturation` is a nested dict whose sub-values are compared with `_values_equal(dict, dict)` which falls through to string comparison (`"{'x': 2.0}" != "{'x': 2}"`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verification_interceptor.py — add to TestReadbackInterceptor

def test_epsilon_threaded_to_nested_float_comparison(self):
    """Nested dict float comparison should use epsilon, not str equality."""
    from harness.verification.interceptor import _diff_properties

    # Simulate: intent has 2.0 (Python float), UE returns 2 (JSON int)
    intent = {"settings": {"colorSaturation": {"x": 2.0, "y": 2.0}}}
    actual = {"settings": {"colorSaturation": {"x": 2, "y": 2}}}
    mismatches = _diff_properties(intent, actual)
    assert len(mismatches) == 0, f"2.0 vs 2 should match, got: {mismatches}"

def test_epsilon_threaded_to_color_float_comparison(self):
    """Color round-trip: 0.88 vs 0.878431 should match with 5e-3 epsilon."""
    from harness.verification.interceptor import _diff_properties

    intent = {"lightColor": {"g": 0.88}}
    actual = {"lightColor": {"g": 0.8784314393997192}}
    mismatches = _diff_properties(intent, actual)
    assert len(mismatches) == 0, f"color should match within epsilon, got: {mismatches}"
```

Run: `uv run pytest tests/test_verification_interceptor.py::TestReadbackInterceptor::test_epsilon_threaded_to_nested_float_comparison tests/test_verification_interceptor.py::TestReadbackInterceptor::test_epsilon_threaded_to_color_float_comparison -v`
Expected: FAIL

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verification_interceptor.py::TestReadbackInterceptor::test_epsilon_threaded_to_nested_float_comparison -v`
Expected: FAIL

- [ ] **Step 3: Fix `_diff_values` to pass epsilon**

At `harness/verification/interceptor.py:406`:

```python
# Before:
elif short == "set_properties":
    mismatches.extend(_diff_properties(intent, actual))

# After:
elif short == "set_properties":
    mismatches.extend(_diff_properties(intent, actual, epsilon))
```

- [ ] **Step 4: Fix `_diff_properties` signature and make it recursive with epsilon**

Replace `harness/verification/interceptor.py:439-461`:

```python
def _diff_properties(intent: dict, actual: dict, epsilon: float = 1e-6) -> list[str]:
    """按属性名逐一比较。嵌套 dict 递归比较，数值使用 epsilon 容差。"""
    mismatches: list[str] = []
    for key, intent_val in intent.items():
        if key not in actual:
            mismatches.append(f"{key} 读回结果中缺失")
            continue
        actual_val = actual[key]
        # 嵌套 dict: 递归比较
        if isinstance(intent_val, dict) and isinstance(actual_val, dict):
            sub_mismatches = _diff_properties(intent_val, actual_val, epsilon)
            for sm in sub_mismatches:
                mismatches.append(f"{key}.{sm}")
            continue
        # 嵌套 list: 逐元素比较
        if isinstance(intent_val, list) and isinstance(actual_val, list):
            if len(intent_val) != len(actual_val):
                mismatches.append(
                    f"{key} 意图长度={len(intent_val)} 实际长度={len(actual_val)}"
                )
                continue
            for i, (iv, av) in enumerate(zip(intent_val, actual_val)):
                if isinstance(iv, dict) and isinstance(av, dict):
                    sub = _diff_properties(iv, av, epsilon)
                    for sm in sub:
                        mismatches.append(f"{key}[{i}].{sm}")
                elif not _values_equal(iv, av, epsilon):
                    mismatches.append(f"{key}[{i}] 意图={iv} 实际={av}")
            continue
        if not _values_equal(intent_val, actual_val, epsilon):
            mismatches.append(f"{key} 意图={intent_val} 实际={actual_val}")
    return mismatches
```

- [ ] **Step 5: Fix `_values_equal` to accept epsilon parameter**

Replace `harness/verification/interceptor.py:464-472`:

```python
def _values_equal(intent_val: object, actual_val: object, epsilon: float = 1e-6) -> bool:
    """比较两个值是否等价（数值使用 epsilon 容差，字符串精确）。

    意图值 2.0 (float) 与实际值 2 (int) 通过 float 转换比较。
    意图值 0.88 与实际值 0.878431 (UE FLinearColor 精度损失) 在 epsilon=5e-3 下通过。
    """
    try:
        if abs(float(intent_val) - float(actual_val)) <= epsilon:
            return True
        return False
    except (ValueError, TypeError):
        pass
    return str(intent_val) == str(actual_val)
```

- [ ] **Step 6: Update existing test expectations**

Check `test_nested_dict_partial_mismatch` and `test_returnvalue_wrapper_mismatch` in `tests/test_verification_interceptor.py` — they may need epsilon-related adjustments if they test exact comparison behavior.

Run: `uv run pytest tests/test_verification_interceptor.py -v`
Expected: all tests PASS (or adjust as needed)

- [ ] **Step 7: Commit**

```bash
git add harness/verification/interceptor.py tests/test_verification_interceptor.py
git commit -m "fix: thread epsilon through _diff_properties to _values_equal

_diffs_values received epsilon=5e-3 from ReadbackInterceptor but dropped
it when calling _diff_properties, which used hardcoded 1e-6 in _values_equal.
UE FLinearColor round-trip loses precision (0.88 → 0.878431, diff 0.00157),
triggering false L2 readback mismatch warnings.

Also made _diff_properties fully recursive for arbitrarily nested dicts,
fixing false positive on settings.colorSaturation sub-keys where
2.0 (float) vs 2 (int) was compared via str() equality."
```

---

### Task 6: Fix `match_reference` dead-loop hint

**Files:**
- Modify: `harness/server.py:824`

**Root Cause:** `match_reference` unconditionally appends "下一步：如尚未生成参数映射，请调 build_atmosphere_mapping()". But in the standard workflow, `build_atmosphere_mapping` is always called BEFORE `match_reference` (and already failed). The hint sends LLM into a dead loop.

- [ ] **Step 1: Track whether mapping was already generated**

In the `build_atmosphere_mapping` handler (after successful MiMo classification, ~line 975), set a session-level flag:

```python
# After successful mapping generation, before returning result:
if snapshot_recorder is not None:
    # ... existing snapshot code ...
    pass

# Set flag so match_reference doesn't dead-loop
_session_mapping_generated = True
```

And in `match_reference` (~line 824), gate the hint:

```python
# Before:
lines.append("下一步：如尚未生成参数映射，请调 build_atmosphere_mapping()。")

# After:
if not _session_mapping_generated:
    lines.append("下一步：请先调 build_atmosphere_mapping() 生成参数映射。")
else:
    lines.append("对照映射和差异调整各组件。交叉参考 MiMo 分析和量化指标——")
    lines.append("两者一致则高置信，不一致则以 MiMo 为主、量化指标为参考修正。")
```

Need to declare `nonlocal _session_mapping_generated` at the top of `call_tool` and initialize it:

```python
# Near the top of call_tool, alongside other nonlocal declarations:
nonlocal _session_reference, _session_mapping_generated

# Add initialization in build_server or call_tool:
# _session_mapping_generated = False
```

- [ ] **Step 2: Fix the duplication**

Currently the "对照映射..." line appears unconditionally after the hint. Move it into the else branch to avoid duplication.

- [ ] **Step 3: Run related tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py tests/test_camera_alignment.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add harness/server.py
git commit -m "fix: match_reference no longer dead-loops on build_atmosphere_mapping hint

Track _session_mapping_generated flag. If build_atmosphere_mapping already ran
(regardless of success), match_reference skips the redundant hint and goes
straight to 'adjust components using the mapping'."
```

---

### Task 7: Fix `build_atmosphere_mapping` error guidance

**Files:**
- Modify: `harness/server.py:869-872`

**Root Cause:** When `find_actors` returns empty (now fixed by Task 1), the handler's message "未找到 → 请调 add_to_scene_from_class 创建" led LLM to incorrectly create actors. Even with the parse fix, the handler should not prescribe destructive actions — just report what was found.

- [ ] **Step 1: Change the guidance text**

At `harness/server.py:869-872`:

```python
# Before:
scan_lines.append(
    f"  {actor_type}: 未找到 → "
    f"请调 add_to_scene_from_class 创建"
)

# After:
scan_lines.append(
    f"  {actor_type}: 未找到"
)
```

After the scan loop, add a neutral summary instead of per-type action suggestions:

```python
# After the for loop, before Step 2:
missing_types = [
    at for at, names in actors_found.items() if not names
]
if missing_types:
    scan_lines.append("")
    scan_lines.append(
        f"提示: {len(missing_types)} 类组件未找到 ({', '.join(missing_types)})。"
        f"如需创建，使用 add_to_scene_from_class。"
    )
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: update assertion for "未找到" text if needed, all PASS

- [ ] **Step 3: Commit**

```bash
git add harness/server.py
git commit -m "fix: neutral guidance when atmosphere components not found

Replace per-type '→ 请调 add_to_scene_from_class 创建' with a single
summary hint at the end. Avoids LLM incorrectly creating actors when
the parse layer has already failed silently."
```

---

## Execution Order

```
Task 1 (parse fix) → Task 2 (test mock) → Task 3 (param fix) → Task 4 (component recursion) → Task 5 (L2 epsilon) → Task 6 (dead-loop hint) → Task 7 (error guidance)
```

Tasks 1-4 are strictly sequential (each depends on previous). Tasks 5, 6, 7 are independent of each other but should run after Task 4 to avoid merge conflicts on `harness/server.py`.

## Self-Review

**1. Spec coverage:**
- [x] `_extract_actor_names` MCP content unwrap → Task 1
- [x] Test mock real format → Task 2
- [x] `actor_name` → `instance` param → Task 3
- [x] Component-level property recursion → Task 4
- [x] L2 epsilon false positives → Task 5
- [x] `match_reference` dead-loop hint → Task 6
- [x] Error guidance neutral tone → Task 7

**2. Placeholder scan:** No TBD, TODO, "add appropriate error handling", or "similar to Task N" patterns. Every step has concrete code.

**3. Type consistency:**
- `_extract_actor_names` returns `list[str]` throughout
- `_diff_properties` signature: `(dict, dict, float) -> list[str]`
- `_values_equal` signature: `(object, object, float) -> bool`
- All MCP content format: `{"content": [{"type": "text", "text": "..."}]}`
- `instance` param: `{"instance": {"refPath": "..."}}`
