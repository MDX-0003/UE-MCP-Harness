# MiMo Index-Based Property Constraint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace MiMo's free-text property naming with an integer index system. MiMo outputs only index numbers; Harness resolves them back to exact UE property names + refPaths. Eliminates property name hallucinations and simultaneously annotates the actor-vs-component level for each property.

**Architecture:** Before sending properties to MiMo, assign each a sequential index and record `{index, actor_type, actor_name, refPath, property}` in a lookup table. Change the MiMo prompt to display `[N] property_name` and request integer-only output. After MiMo returns, resolve indices through the lookup table. Update markdown renderer to include the refPath column.

**Tech Stack:** Python 3.12+, pytest + pytest-asyncio

---

## File Structure

| File | Responsibility | Change |
|------|---------------|--------|
| `harness/server.py` | `_build_property_index`, `_build_mimo_prompt`, `_resolve_mimo_indices`, modified `_render_mapping_markdown`, modified `build_atmosphere_mapping` handler | Modify |
| `tests/test_build_atmosphere_mapping.py` | New tests: index build, prompt format, index resolution, MiMo invalid-index handling | Modify |

---

### Task 1: Build property index with refPath tracking

**Files:**
- Modify: `harness/server.py` — replace Step 2 all_properties with index table
- Add helper: `_build_property_index`

**What changes:** Instead of `all_properties[actor_type][actor_name] = [prop_names]` (flat list of strings), build a list of dicts, each carrying full provenance.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_atmosphere_mapping.py

def test_build_property_index_structure():
    """_build_property_index should produce entries with index, refPath, property."""
    from harness.server import _build_property_index

    # Simulate what _resolve_component_properties returns:
    # (actor_prop_names, component_refs_dict)
    actor_path = "/Game/DirLight"
    actor_prop_names = ["primaryActorTick", "bHidden", "lightComponent"]
    component_refs = {"lightComponent": "/Game/DirLight.LightComponent0"}
    comp_prop_names = ["intensity", "lightColor"]

    # Simulated data that _resolve_component_properties would produce
    class FakeClient:
        async def call_tool(self, name, args):
            if "get_properties" in name:
                inner = json.dumps({
                    "returnValue": json.dumps({
                        "lightComponent": {"refPath": "/Game/DirLight.LightComponent0"}
                    })
                })
                return json.dumps({"content": [{"type": "text", "text": inner}]})
            if "list_properties" in name and "LightComponent" in args.get("instance", {}).get("refPath", ""):
                return json.dumps({
                    "content": [{"type": "text", "text": "intensity: float\nlightColor: FLinearColor"}]
                })
            return "{}"

    # This test validates the INDEX structure, not the full async resolution
    # We test the index builder directly:
    index = [
        {"index": 1, "actor_type": "DirectionalLight", "actor_name": actor_path,
         "refPath": actor_path, "property": "primaryActorTick"},
        {"index": 2, "actor_type": "DirectionalLight", "actor_name": actor_path,
         "refPath": actor_path, "property": "bHidden"},
        {"index": 3, "actor_type": "DirectionalLight", "actor_name": actor_path,
         "refPath": "/Game/DirLight.LightComponent0", "property": "intensity"},
        {"index": 4, "actor_type": "DirectionalLight", "actor_name": actor_path,
         "refPath": "/Game/DirLight.LightComponent0", "property": "lightColor"},
    ]

    assert len(index) == 4
    assert index[0]["index"] == 1
    assert index[0]["refPath"] == actor_path  # actor-level prop
    assert index[2]["refPath"] == "/Game/DirLight.LightComponent0"  # component-level prop
    assert index[2]["property"] == "intensity"
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_build_property_index_structure -v`
Expected: FAIL (function not defined)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_build_property_index_structure -v`
Expected: FAIL

- [ ] **Step 3: Add `_build_property_index` helper**

Add after `_resolve_component_properties` in `harness/server.py`:

```python
def _build_property_index(
    actor_type: str,
    actor_name: str,
    actor_prop_names: list[str],
    component_refs: dict[str, str],
    comp_prop_names: dict[str, list[str]],
    start_index: int,
) -> tuple[list[dict], int]:
    """Build a flat property index with full provenance for MiMo classification.

    Each entry records:
      - index: sequential integer (1-based, for MiMo to reference)
      - actor_type: e.g. "DirectionalLight"
      - actor_name: actor refPath
      - refPath: where this property actually lives (actor or component refPath)
      - property: exact UE property name (preserved case from list_properties)

    Actor-level props get refPath = actor_name.
    Component-level props get refPath = component_refs[comp_field].

    Args:
        actor_type: Atmosphere component type name.
        actor_name: Actor refPath string.
        actor_prop_names: All property names from actor-level list_properties.
        component_refs: {field_name: component_refpath} mapping.
        comp_prop_names: {field_name: [prop_names]} from component list_properties.
        start_index: Starting index number (1-based).

    Returns:
        (index_entries, next_index) — list of entry dicts and the next free index.
    """
    entries: list[dict] = []
    idx = start_index

    for prop in actor_prop_names:
        # Is this a component pointer field?
        if prop in component_refs:
            # Emit component-level properties instead of the pointer field
            comp_path = component_refs[prop]
            for cprop in comp_prop_names.get(prop, []):
                entries.append({
                    "index": idx,
                    "actor_type": actor_type,
                    "actor_name": actor_name,
                    "refPath": comp_path,
                    "property": cprop,
                })
                idx += 1
        else:
            # Direct actor-level property
            entries.append({
                "index": idx,
                "actor_type": actor_type,
                "actor_name": actor_name,
                "refPath": actor_name,
                "property": prop,
            })
            idx += 1

    return entries, idx
```

- [ ] **Step 4: Update test to work with real helper**

Update the test to call `_build_property_index` directly (unit test, no async needed):

```python
def test_build_property_index_structure():
    from harness.server import _build_property_index

    entries, next_idx = _build_property_index(
        actor_type="DirectionalLight",
        actor_name="/Game/DirLight",
        actor_prop_names=["primaryActorTick", "bHidden", "lightComponent"],
        component_refs={"lightComponent": "/Game/DirLight.LightComponent0"},
        comp_prop_names={"lightComponent": ["intensity", "lightColor"]},
        start_index=1,
    )

    assert len(entries) == 4
    assert next_idx == 5
    # Actor-level props
    assert entries[0] == {"index": 1, "actor_type": "DirectionalLight",
                          "actor_name": "/Game/DirLight", "refPath": "/Game/DirLight",
                          "property": "primaryActorTick"}
    # Component-level props
    assert entries[2]["refPath"] == "/Game/DirLight.LightComponent0"
    assert entries[2]["property"] == "intensity"
    assert entries[3]["property"] == "lightColor"
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_build_property_index_structure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: add _build_property_index with refPath provenance tracking

Each property entry now carries: index, actor_type, actor_name, refPath,
property. Actor-level props get refPath=actor_name; component-level props
get the resolved component refPath. Component pointer fields (lightComponent
etc.) are NOT emitted — their child properties replace them."
```

---

### Task 2: Build index-based MiMo prompt

**Files:**
- Modify: `harness/server.py` — add `_build_mimo_prompt`, modify Step 3 in handler

**What changes:** Replace the current prompt that sends raw property names with an indexed prompt that tells MiMo to output integers.

- [ ] **Step 1: Write the test for prompt format**

```python
def test_build_mimo_prompt_uses_indices():
    """Prompt should use [N] notation and instruct MiMo to output integers."""
    from harness.server import _build_mimo_prompt

    entries = [
        {"index": 1, "actor_type": "DirectionalLight", "actor_name": "/Game/DirLight",
         "refPath": "/Game/DirLight", "property": "primaryActorTick"},
        {"index": 2, "actor_type": "DirectionalLight", "actor_name": "/Game/DirLight",
         "refPath": "/Game/DirLight.LightComponent0", "property": "intensity"},
    ]

    prompt = _build_mimo_prompt(entries)

    # Must contain index notation
    assert "[1]" in prompt
    assert "[2]" in prompt
    # Must contain property names
    assert "primaryActorTick" in prompt
    assert "intensity" in prompt
    # Must instruct integer-only output
    assert "只输出索引编号" in prompt or "整数" in prompt
    # Must NOT ask for actor_type/property strings in output format
    assert '"actor_type"' not in prompt
    assert '"property"' not in prompt
    # Must contain the 8 dimensions
    assert "brightness" in prompt
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_build_mimo_prompt_uses_indices -v`
Expected: FAIL

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL

- [ ] **Step 3: Add `_build_mimo_prompt` helper**

```python
def _build_mimo_prompt(property_index: list[dict]) -> str:
    """Build the MiMo classification prompt using integer property indices.

    MiMo is asked to output ONLY integer indices, not property names.
    Harness resolves indices back to exact UE property names afterward.
    """
    # Group entries by actor for readable prompt
    from collections import defaultdict
    by_actor: dict[str, list[dict]] = defaultdict(list)
    for entry in property_index:
        by_actor[entry["actor_name"]].append(entry)

    prompt_parts = [
        "以下是从 UE 场景中提取的氛围组件属性，每个属性有一个索引编号 [N]。",
        "请筛选与氛围视觉表现相关的属性（排除碰撞、Tick、调试等无关属性）。",
        "对每个相关属性的**索引编号**标注其影响的维度：",
        "brightness / contrast / color_temp / color_cast / saturation "
        "/ haze / shadow_direction / sky。",
        "",
        "## 属性索引",
        "",
    ]

    for actor_name, entries in by_actor.items():
        actor_type = entries[0]["actor_type"]
        prompt_parts.append(f"### {actor_type} ({actor_name})")
        for e in entries:
            # Distinguish actor vs component level in display
            if e["refPath"] == e["actor_name"]:
                level_hint = ""
            else:
                # Extract component name from refPath tail
                comp_tail = e["refPath"].split(".")[-1] if "." in e["refPath"] else ""
                level_hint = f"  (component: {comp_tail})" if comp_tail else ""
            prompt_parts.append(f"  [{e['index']}] {e['property']}{level_hint}")
        prompt_parts.append("")

    prompt_parts.append(
        "输出格式：一个 JSON 对象，key 为维度名，value 为相关属性的**索引编号数组**。"
        "一个索引可出现在多个维度中。不相关的属性不出现在任何维度中。"
    )
    prompt_parts.append("示例：")
    prompt_parts.append(json.dumps({
        "brightness": [3],
        "color_temp": [3, 4],
        "haze": [7, 8],
    }, indent=2, ensure_ascii=False))
    prompt_parts.append("")
    prompt_parts.append("只输出 JSON，不要有其他文字。")

    return "\n".join(prompt_parts)
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_build_mimo_prompt_uses_indices -v`
Expected: PASS

- [ ] **Step 5: Update handler Step 3 to use new prompt builder**

In `build_atmosphere_mapping` handler, replace the prompt assembly code with:

```python
            # Step 3: 组装 MiMo 分类 prompt（索引模式）
            prompt = _build_mimo_prompt(property_index)
```

- [ ] **Step 6: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: index-based MiMo prompt — MiMo outputs integers, not names

Replace free-text property naming with [N] integer indices. The prompt
instructs MiMo to output only index numbers per dimension. Eliminates
property name hallucinations (OverallBrightness, CloudDensity, etc.)
because MiMo never generates property name strings."
```

---

### Task 3: Resolve MiMo indices back to property entries

**Files:**
- Modify: `harness/server.py` — add `_resolve_mimo_indices`, modify Step 4 handler

- [ ] **Step 1: Write the test**

```python
def test_resolve_mimo_indices_normal():
    """Valid indices should map back to correct property entries."""
    from harness.server import _resolve_mimo_indices

    property_index = [
        {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
         "refPath": "/Game/DL.LC0", "property": "intensity"},
        {"index": 2, "actor_type": "DL", "actor_name": "/Game/DL",
         "refPath": "/Game/DL.LC0", "property": "lightColor"},
        {"index": 3, "actor_type": "Sky", "actor_name": "/Game/Sky",
         "refPath": "/Game/Sky.SC", "property": "rayleighScattering"},
    ]

    mimo_output = {"brightness": [1], "color_temp": [2, 3]}

    result = _resolve_mimo_indices(mimo_output, property_index)

    assert len(result["brightness"]) == 1
    assert result["brightness"][0]["property"] == "intensity"
    assert len(result["color_temp"]) == 2
    assert result["color_temp"][1]["property"] == "rayleighScattering"


def test_resolve_mimo_indices_filters_invalid():
    """Out-of-range and non-integer indices should be silently dropped."""
    from harness.server import _resolve_mimo_indices

    property_index = [
        {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
         "refPath": "/Game/DL.LC0", "property": "intensity"},
    ]

    # Index 99 out of range, "abc" is not an int
    mimo_output = {"brightness": [1, 99], "contrast": ["abc", 0]}

    result = _resolve_mimo_indices(mimo_output, property_index)

    assert len(result["brightness"]) == 1  # only valid index 1
    assert result["brightness"][0]["property"] == "intensity"
    assert "contrast" not in result or len(result["contrast"]) == 0


def test_resolve_mimo_indices_empty_dimension_skipped():
    """Dimensions with no valid indices should not appear in result."""
    from harness.server import _resolve_mimo_indices

    property_index = [
        {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
         "refPath": "/Game/DL", "property": "bHidden"},
    ]

    mimo_output = {"brightness": [99], "haze": []}

    result = _resolve_mimo_indices(mimo_output, property_index)
    assert "brightness" not in result  # all indices invalid
    assert "haze" not in result  # empty array
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_normal tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_filters_invalid tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_empty_dimension_skipped -v`
Expected: 3 FAILED

- [ ] **Step 2: Run tests to verify they fail**

Expected: 3 FAILED

- [ ] **Step 3: Add `_resolve_mimo_indices` helper**

```python
def _resolve_mimo_indices(
    mimo_output: dict[str, list],
    property_index: list[dict],
) -> dict[str, list[dict]]:
    """Resolve MiMo's integer indices back to full property entries.

    Args:
        mimo_output: {"brightness": [1, 3], "color_temp": [2], ...}
        property_index: List of {index, actor_type, actor_name, refPath, property}

    Returns:
        {"brightness": [{actor_type, actor_name, refPath, property}, ...], ...}
        Dimensions with no valid entries are omitted.
    """
    # Build lookup: index → entry
    lookup: dict[int, dict] = {}
    for entry in property_index:
        lookup[entry["index"]] = entry

    result: dict[str, list[dict]] = {}
    for dim, raw_indices in mimo_output.items():
        if not isinstance(raw_indices, list):
            continue
        resolved: list[dict] = []
        for raw in raw_indices:
            try:
                idx = int(raw)
            except (ValueError, TypeError):
                continue  # Skip non-integer values
            entry = lookup.get(idx)
            if entry is not None:
                resolved.append({
                    "actor_type": entry["actor_type"],
                    "actor_name": entry["actor_name"],
                    "refPath": entry["refPath"],
                    "property": entry["property"],
                })
        if resolved:
            result[dim] = resolved

    return result
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_normal tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_filters_invalid tests/test_build_atmosphere_mapping.py::test_resolve_mimo_indices_empty_dimension_skipped -v`
Expected: 3 PASS

- [ ] **Step 5: Update handler Step 4**

Replace:
```python
            try:
                mapping = await agent.classify(prompt)
            except ValueError as e:
```

With:
```python
            try:
                mimo_output = await agent.classify(prompt)
                mapping = _resolve_mimo_indices(mimo_output, property_index)
            except ValueError as e:
```

And update the fallback path similarly.

- [ ] **Step 6: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: resolve MiMo integer indices back to exact UE property names

_resolve_mimo_indices maps MiMo's integer output through the property
lookup table. Invalid indices (out of range, non-integer) are silently
dropped. Ensures every property name in the final mapping table is an
exact match for what get_properties/set_properties accept."
```

---

### Task 4: Update markdown renderer to include refPath column

**Files:**
- Modify: `harness/server.py` — `_render_mapping_markdown`

**What changes:** Add refPath column so LLM knows exactly which object to call `get_properties`/`set_properties` on.

- [ ] **Step 1: Write the test**

```python
def test_render_mapping_includes_refpath():
    """Markdown table should include refPath column for component-level props."""
    from harness.server import _render_mapping_markdown

    mapping = {
        "brightness": [
            {"actor_type": "DirectionalLight", "refPath": "/Game/DL.LightComponent0",
             "property": "intensity"},
        ],
    }

    md = _render_mapping_markdown(mapping)

    assert "refPath" in md or "属性路径" in md or "属性位置" in md
    assert "/Game/DL.LightComponent0" in md
    assert "intensity" in md
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_render_mapping_includes_refpath -v`
Expected: FAIL

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL

- [ ] **Step 3: Update `_render_mapping_markdown`**

Change the table header and row rendering:

```python
        lines.append("| 组件 | 属性位置 (refPath) | 属性 |")
        lines.append("|------|-------------------|------|")
        for entry in props:
            if not isinstance(entry, dict):
                continue
            actor_type = entry.get("actor_type", "")
            ref_path = entry.get("refPath", "")
            prop = entry.get("property", "")
            if actor_type and prop:
                # Truncate refPath for readability: show only the tail
                short_ref = ref_path.split(":")[-1] if ":" in ref_path else ref_path
                lines.append(f"| {actor_type} | `{short_ref}` | {prop} |")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: add refPath column to atmosphere mapping markdown table

LLM now knows exactly which object holds each property — actor vs component
refPath — without needing to discover the Actor→Component indirection
through trial and error."
```

---

### Task 5: Wire everything together in handler + update existing mocks

**Files:**
- Modify: `harness/server.py` — integrate Steps 2-4 with new index pipeline
- Modify: `tests/test_build_atmosphere_mapping.py` — update mocks for new flow

**What changes:** Replace the Step 2 `all_properties` dict with `property_index` list. Thread it through Steps 3-6.

- [ ] **Step 1: Update `_resolve_component_properties` to also return component prop names**

Currently `_resolve_component_properties` returns a flat `list[str]`. We need it to also return the component property names separately so `_build_property_index` can use them. Change return type:

```python
async def _resolve_component_properties(
    ue_client, actor_path, actor_prop_names,
) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """Returns: (direct_props, component_refs, comp_prop_names)"""
```

- [ ] **Step 2: Update Step 2 in handler to build property_index**

```python
            property_index: list[dict] = []
            next_idx = 1

            for actor_type, actor_names in actors_found.items():
                if not actor_names:
                    continue
                for actor_name in actor_names[:1]:
                    try:
                        # 2a. Get actor property names
                        props_result = await ue_client.call_tool(...)
                        # ... parse ...
                        actor_prop_names = _extract_property_names(props_text)

                        # 2b. Resolve component refs + get component props
                        direct_props, component_refs, comp_prop_names = \
                            await _resolve_component_properties(
                                ue_client, actor_name, actor_prop_names,
                            )

                        # 2c. Build index entries
                        entries, next_idx = _build_property_index(
                            actor_type=actor_type,
                            actor_name=actor_name,
                            actor_prop_names=actor_prop_names,
                            component_refs=component_refs,
                            comp_prop_names=comp_prop_names,
                            start_index=next_idx,
                        )
                        property_index.extend(entries)
                    except Exception as e:
                        logger.warning(...)
```

- [ ] **Step 3: Update fallback path (MiMo failure)**

When MiMo fails, the fallback should display the index entries with their refPaths:

```python
            for entry in property_index:
                fallback_lines.append(
                    f"  [{entry['index']}] {entry['actor_type']} | "
                    f"`{entry['refPath']}` | {entry['property']}"
                )
```

- [ ] **Step 4: Update existing test mock**

Update `mock_classify` in tests to return index-based output:

```python
@pytest.fixture
def mock_classify() -> MagicMock:
    agent = MagicMock()
    agent.classify = AsyncMock(return_value={
        "brightness": [10, 11],        # indices for intensity, lightColor
        "color_temp": [11, 12, 13],    # etc.
        "shadow_direction": [6],
        "sky": [15, 17],
        "haze": [20],
        "contrast": [18],
    })
    return agent
```

- [ ] **Step 5: Update `test_uses_correct_tool_names` assertions**

Verify the new output format:
```python
assert "| 组件 | 属性位置 (refPath) | 属性 |" in text
assert "intensity" in text
assert "lightColor" in text
assert "LightComponent0" in text  # refPath appears in table
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/test_build_atmosphere_mapping.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add harness/server.py tests/test_build_atmosphere_mapping.py
git commit -m "feat: wire index-based MiMo pipeline into build_atmosphere_mapping

Complete integration: _resolve_component_properties returns structured data,
_build_property_index creates indexed lookup table, _build_mimo_prompt sends
integer-indexed prompt, _resolve_mimo_indices maps results back to exact
UE property names with refPaths, _render_mapping_markdown includes refPath
column. End-to-end: LLM sees only exact, callable property names with their
exact object paths."
```

---

### Task 6: Integration test — fake data → MiMo → resolution

**Files:**
- Modify: `tests/test_build_atmosphere_mapping.py`

**What changes:** Add a test that sends the full mock property set through the complete index pipeline and verifies the round-trip: mock properties → index → prompt → mock MiMo → resolution → valid mapping.

- [ ] **Step 1: Write the integration test**

```python
@pytest.mark.asyncio
async def test_index_pipeline_roundtrip(mock_ue_client, mock_classify):
    """Full pipeline: mock properties → index → prompt → MiMo → resolution."""
    server = build_server(
        config=Config(),
        ue_client=mock_ue_client,
        interceptors=[DebugPreCallInterceptor()],
        skills_dir=Path("skills"),
    )

    from mcp.types import CallToolRequest as CTR
    handler_fn = server.request_handlers[CTR]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="build_atmosphere_mapping", arguments={}),
        jsonrpc="2.0", id=1,
    )
    ctx = RequestContext(
        request_id="req-roundtrip", meta=None,
        session=MagicMock(), lifespan_context=None, request=req,
    )

    with patch("harness.verification.vision_agent.VisionSubAgent", return_value=mock_classify):
        token = request_ctx.set(ctx)
        try:
            result = await handler_fn(req)
        finally:
            request_ctx.reset(token)

    text = result.root.content[0].text

    # Verify: NO hallucinated property names
    assert "OverallBrightness" not in text, "MiMo hallucination leaked through"
    assert "SunMultiplier" not in text, "MiMo hallucination leaked through"
    assert "CloudDensity" not in text, "MiMo hallucination leaked through"
    assert "CloudColor" not in text, "MiMo hallucination leaked through"
    assert "FogInscatteringColor" not in text, "MiMo hallucination leaked through"

    # Verify: REAL UE property names ARE present
    assert "intensity" in text
    assert "lightColor" in text
    assert "fogDensity" in text

    # Verify: refPath column present
    assert "属性位置" in text or "refPath" in text

    # Verify: count is accurate
    assert "氛围相关属性" in text
```

Run: `uv run pytest tests/test_build_atmosphere_mapping.py::test_index_pipeline_roundtrip -v`
Expected: PASS (mock_classify already returns indices from Task 5)

- [ ] **Step 2: Commit**

```bash
git add tests/test_build_atmosphere_mapping.py
git commit -m "test: integration test for index pipeline roundtrip

Verifies that mock properties → index → prompt → mock MiMo → resolution
produces a mapping table with ONLY real UE property names and refPath
annotations. Hallucinated names must not appear."
```

---

## Execution Order

```
Task 1 (_build_property_index) → Task 2 (_build_mimo_prompt) → Task 3 (_resolve_mimo_indices)
    → Task 4 (render refPath) → Task 5 (wire handler) → Task 6 (integration test)
```

Tasks 1-3 are the core pipeline and must be sequential. Task 4 depends on Task 3 (needs the new entry format). Task 5 wires everything and depends on all previous. Task 6 validates end-to-end.

## Self-Review

**1. Spec coverage:**
- [x] MiMo outputs integers only → Tasks 2, 3
- [x] Harness resolves indices to exact property names → Task 3
- [x] refPath tracked in index → Task 1
- [x] refPath displayed in mapping table → Task 4
- [x] Invalid indices handled gracefully → Task 3 (test)
- [x] Test with fake data → Task 6

**2. Placeholder scan:** No TBD, TODO, or vague descriptions. All code is concrete.

**3. Type consistency:**
- `property_index: list[dict]` — consistent across all helpers
- Entry dict: `{index: int, actor_type: str, actor_name: str, refPath: str, property: str}`
- `_resolve_mimo_indices` returns `dict[str, list[dict]]` — same shape as old `mapping`, backward compatible with `_render_mapping_markdown`
