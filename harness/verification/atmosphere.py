"""Atmosphere mapping subsystem — build_atmosphere_mapping handler + property index/MiMo/markdown helpers。

涉及的 Issue：008（State Cache find_actors 用法）、016（参考图机制）、019（模块提取）、0708（氛围映射设计）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcp.types import CallToolResult, TextContent

from harness.tools import ToolContext, tool_ok, tool_fail, log_local_call
from harness.client import mcp_parse_result, mcp_extract_text, mcp_unwrap_return_text, mcp_unwrap_return_value
from harness.state.normalize import ref_parse_actor_names, ref_extract_full_paths

_log = logging.getLogger("harness.atmosphere")


# ---- Helper Functions ----


def _parse_property_names(parsed_text: str | None) -> list[str]:
    """从 list_properties 的返回文本中提取属性名列表.

    处理三种 UE 返回格式：
      0. JSON 对象格式（actor 级 list_properties，带 returnValue 解包后）:
         "{\"directionalLightComponent\": {\"type\": \"object\", ...}, ...}"
         → 直接取 JSON keys 作为属性名
      1. 多行 name: type 格式（component 级 list_properties）:
         "intensity: float\\nlightColor: FLinearColor\\n..."
      2. 单行逗号分隔格式（actor 级 list_properties 未解包）:
         "[75 fields] directionalLightComponent, lightComponent, bHidden, ..."
    """
    if not parsed_text:
        return []
    # 格式 0: JSON 对象 → 返回 keys
    try:
        data = json.loads(parsed_text)
        if isinstance(data, dict):
            return list(data.keys())
    except (json.JSONDecodeError, TypeError):
        pass
    names: list[str] = []
    for line in parsed_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 格式 1: "name: type" 或 "name (description)"
        for delim in (":", " ("):
            if delim in line:
                name = line.split(delim)[0].strip()
                if name and not name.startswith("#") \
                        and not name.startswith("//"):
                    names.append(name)
                break
        else:
            # 格式 2: 逗号分隔 — "[N fields] a, b, c, ..."
            if "," in line:
                # 去掉 "[N fields]" 前缀
                comma_part = line
                if line.startswith("[") and "fields]" in line:
                    bracket_end = line.index("fields]") + 7
                    comma_part = line[bracket_end:].strip()
                for part in comma_part.split(","):
                    name = part.strip()
                    if name and not name.startswith("#") \
                            and not name.startswith("{"):
                        names.append(name)
            elif not line.startswith("#") and not line.startswith("{") \
                    and len(line) < 100:
                names.append(line)
    return names


async def _resolve_component_properties(
    ue_client: "McpClientSession",
    actor_path: str,
    actor_prop_names: list[str],
) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """从 actor 属性中识别 component 引用字段，递归获取 component 级属性名.

    UE 的 Actor-Component 关系是两层结构：
      - Actor 顶层字段如 ``lightComponent`` 的值是 component refPath
      - 真正的氛围属性（intensity, lightColor 等）在 component 子对象上

    此函数识别以 "Component" 结尾的字段，调 get_properties 解析其 refPath，
    再对每个 component 调 list_properties 获取属性名。

    Returns:
        (direct_props, component_refs, comp_prop_names)
        - direct_props: actor 直接属性名列表（不含 component 指针字段）
        - component_refs: {field_name: component_refpath}
        - comp_prop_names: {field_name: [prop_name, ...]}
    """
    # 疑似 component 引用字段
    suspect_fields = [
        p for p in actor_prop_names
        if p.endswith("Component") or "Component" in p
    ]
    if not suspect_fields:
        return (actor_prop_names, {}, {})

    # 调 get_properties 解析这些字段的实际值（refPath）
    try:
        result_text = await ue_client.call_tool(
            "toolset_registry.toolsets.core.object.ObjectTools.get_properties",
            {"instance": {"refPath": actor_path}, "properties": suspect_fields},
        )
        parsed = mcp_parse_result(result_text)
        text = mcp_extract_text(parsed, result_text) or ""
        rv = mcp_unwrap_return_value(text)
    except Exception:
        return (actor_prop_names, {}, {})

    if rv is None:
        return (actor_prop_names, {}, {})

    # 分离 component refPath vs 普通属性
    component_refs: dict[str, str] = {}
    direct_props: list[str] = []

    for name in actor_prop_names:
        if name in suspect_fields:
            val = rv.get(name)
            if isinstance(val, dict) and val.get("refPath"):
                component_refs[name] = val["refPath"]
                continue
        direct_props.append(name)

    # 递归获取 component 属性名
    comp_prop_names: dict[str, list[str]] = {}
    for comp_field, comp_refpath in component_refs.items():
        try:
            comp_result = await ue_client.call_tool(
                "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                {"instance": {"refPath": comp_refpath}},
            )
            comp_parsed = mcp_parse_result(comp_result)
            comp_text = mcp_extract_text(comp_parsed, comp_result)
            comp_text = mcp_unwrap_return_text(comp_text or "")
            comp_names = _parse_property_names(comp_text)
            comp_prop_names[comp_field] = comp_names
        except Exception as e:
            _log.warning(
                "获取 component %s 属性失败: %s", comp_refpath, e,
            )
            comp_prop_names[comp_field] = []

    return (direct_props, component_refs, comp_prop_names)


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
      - property: exact UE property name (preserved from list_properties)

    Actor-level props get refPath = actor_name.
    Component pointer fields are NOT emitted — their child properties replace them.
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
        if prop in component_refs:
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
            entries.append({
                "index": idx,
                "actor_type": actor_type,
                "actor_name": actor_name,
                "refPath": actor_name,
                "property": prop,
            })
            idx += 1

    return entries, idx


def _build_mimo_prompt(property_index: list[dict]) -> str:
    """Build the MiMo classification prompt using integer property indices.

    MiMo outputs ONLY integer indices, not property names.
    Harness resolves indices back to exact UE property names afterward.
    """
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
            if e["refPath"] == e["actor_name"]:
                level_hint = ""
            else:
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
        Invalid indices (out of range, non-integer) are silently dropped.
    """
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
                continue
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


# ---- 氛围属性白名单（替代 MiMo 分类，确定性 + 零延迟） ----
# 当 VisionSubAgent.classify() 不可用时（API 未配置、纯文本调用不被代理支持等），
# 走此白名单直接筛选，避免 fallback 倾倒全部 1200+ 属性。
ATMOSPHERE_WHITELIST: dict[str, list[str]] = {
    "brightness": [
        "intensity", "indirectLightingIntensity",
        "volumetricScatteringIntensity", "diffuseScale",
        "skyLuminanceFactor", "autoExposureBias",
    ],
    "contrast": [
        "colorContrast", "colorGamma",
        "bloomScale", "bloomThreshold",
    ],
    "color_temp": [
        "lightColor", "temperature", "bUseTemperature",
        "whiteTemp", "whiteTint",
        "atmosphereSunDiskColorScale",
    ],
    "color_cast": [
        "sceneColorTint",
        "fogInscatteringColor", "fogInscatteringLuminance",
        "directionalInscatteringLuminance",
        "modulatedShadowColor", "groundAlbedo",
    ],
    "saturation": [
        "colorSaturation",
    ],
    "haze": [
        "fogDensity", "fogHeightFalloff", "fogMaxOpacity",
        "startDistance", "endDistance", "fogCutoffDistance",
        "directionalInscatteringExponent",
        "volumetricFogScatteringDistribution",
        "volumetricFogAlbedo", "volumetricFogExtinctionScale",
        "volumetricFogDistance", "bEnableVolumetricFog",
        "layerBottomAltitude", "layerHeight",
        "tracingMaxDistance", "tracingStartMaxDistance",
        "rayleighScattering", "rayleighScatteringScale",
        "rayleighExponentialDistribution",
        "mieScattering", "mieScatteringScale",
        "mieAbsorption", "mieAnisotropy", "mieExponentialDistribution",
        "multiScatteringFactor",
        "bAtmosphereSunLight", "heightFogContribution",
    ],
    "shadow_direction": [
        "relativeRotation",
    ],
    "sky": [
        "skyLuminanceFactor", "heightFogContribution",
        "atmosphereSunDiskColorScale",
        "cloudScatteredLuminanceScale",
        "bloomTint",
    ],
}


def _classify_by_whitelist(property_index: list[dict]) -> dict[str, list[dict]]:
    """按硬编码白名单分类属性到 9 维度，替代 MiMo classify()。

    白名单基于 UE 引擎属性名的确定性知识，不依赖外部 LLM。
    属性名在白名单中 → 归入对应维度。
    """
    result: dict[str, list[dict]] = {dim: [] for dim in ATMOSPHERE_WHITELIST}
    for entry in property_index:
        prop = entry.get("property", "")
        if not prop:
            continue
        for dim, names in ATMOSPHERE_WHITELIST.items():
            if prop in names:
                result[dim].append({
                    "actor_type": entry["actor_type"],
                    "actor_name": entry["actor_name"],
                    "refPath": entry["refPath"],
                    "property": prop,
                })
    # 删除空维度
    return {k: v for k, v in result.items() if v}


def _render_mapping_markdown(mapping: dict[str, Any]) -> str:
    """将维度分组映射 dict 转为 Markdown 表格.

    Args:
        mapping: {"brightness": [{actor_type, refPath, property}, ...], ...}

    Returns:
        渲染后的 Markdown 文本（含属性位置列用于标注 actor/component 层级）
    """
    DIM_LABELS: dict[str, str] = {
        "brightness": "亮度 (Brightness)",
        "contrast": "对比度 (Contrast)",
        "color_temp": "色温 (Color Temperature)",
        "color_cast": "色调偏移 (Color Cast)",
        "saturation": "饱和度 (Saturation)",
        "haze": "大气密度 (Haze)",
        "shadow_direction": "阴影方向 (Shadow Direction)",
        "sky": "天空表现 (Sky)",
    }

    lines = ["# Atmosphere Mapping", ""]
    total = 0

    for dim_key, dim_label in DIM_LABELS.items():
        props = mapping.get(dim_key)
        if not props or not isinstance(props, list) or len(props) == 0:
            continue
        total += len(props)
        lines.append(f"## {dim_label}")
        lines.append("")
        lines.append("| 组件 | 属性位置 (refPath) | 属性 |")
        lines.append("|------|-------------------|------|")
        for entry in props:
            if not isinstance(entry, dict):
                continue
            actor_type = entry.get("actor_type", "")
            ref_path = entry.get("refPath", "")
            prop = entry.get("property", "")
            if actor_type and prop:
                lines.append(f"| {actor_type} | `{ref_path}` | {prop} |")
        lines.append("")

    lines.insert(1, f"共 {total} 个氛围相关属性")
    lines.insert(2, "")
    return "\n".join(lines)


# ---- Handler ----


async def handle_build_atmosphere_mapping(ctx: ToolContext, arguments: dict) -> CallToolResult:
    from harness.verification.vision_agent import VisionSubAgent
    t0 = time.monotonic()

    # 5 类氛围组件的 UE 类引用
    # 使用 actor_type (class refPath) 而非 glob:
    # UE find_actors 的 glob 匹配对长模式不可靠,
    # "*DirectionalLight*" 返回空但 "*Light*" 能找到,
    # class 引用是精确匹配, 无此问题.
    # 详见 docs/handoff/find_actors_glob_issue.md
    ATMOSPHERE_TYPES: dict[str, str] = {
        "DirectionalLight": "/Script/Engine.DirectionalLight",
        "SkyAtmosphere": "/Script/Engine.SkyAtmosphere",
        "ExponentialHeightFog": "/Script/Engine.ExponentialHeightFog",
        "VolumetricCloud": "/Script/Engine.VolumetricCloud",
        "PostProcessVolume": "/Script/Engine.PostProcessVolume",
    }

    # Step 1: 扫描 5 类组件
    scan_lines: list[str] = []
    actors_found: dict[str, list[str]] = {}

    for actor_type, class_path in ATMOSPHERE_TYPES.items():
        try:
            result_text = await ctx.ue_client.call_tool(
                "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
                {"tag": "", "actor_type": {"refPath": class_path}},
            )
            parsed = mcp_parse_result(result_text)
            full_paths = ref_extract_full_paths(parsed)
            short_names = ref_parse_actor_names(parsed)
            actors_found[actor_type] = full_paths
            count = len(full_paths)
            if count == 1:
                scan_lines.append(f"  {actor_type}: 1 个 ({short_names[0]})")
            elif count > 1:
                scan_lines.append(
                    f"  {actor_type}: {count} 个 "
                    f"({', '.join(short_names[:3])}"
                    f"{'...' if count > 3 else ''}) ⚠ 多实例，需确认"
                )
            else:
                scan_lines.append(
                    f"  {actor_type}: 未找到"
                )
        except Exception as e:
            scan_lines.append(f"  {actor_type}: 查询失败 ({e})")

    # 缺失组件汇总提示（一次性，不给 LLM 逐个下达操作指令的机会）
    missing_types = [
        at for at, names in actors_found.items() if not names
    ]
    if missing_types:
        scan_lines.append("")
        scan_lines.append(
            f"提示: {len(missing_types)} 类组件未找到"
            f"（{', '.join(missing_types)}）。"
            f"如需创建，使用 add_to_scene_from_class。"
        )

    # Step 2: 构建属性索引（含 component 子对象递归 + refPath 溯源）
    property_index: list[dict] = []
    next_idx = 1

    for actor_type, actor_names in actors_found.items():
        if not actor_names:
            continue
        for actor_name in actor_names[:1]:
            try:
                # 2a. 获取 actor 顶层属性名
                props_result = await ctx.ue_client.call_tool(
                    "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                    {"instance": {"refPath": actor_name}},
                )
                props_parsed = mcp_parse_result(props_result)
                props_text = mcp_extract_text(
                    props_parsed, props_result,
                )
                # 解包可能的 returnValue 包装
                props_text = mcp_unwrap_return_text(props_text or "")
                actor_prop_names = _parse_property_names(props_text)

                # 2b. 解析 component 引用，递归获取 component 级属性
                direct_props, component_refs, comp_prop_names = \
                    await _resolve_component_properties(
                        ctx.ue_client, actor_name, actor_prop_names,
                    )

                # 2c. 构建索引条目（component 指针字段被其子属性替换）
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
                _log.warning(
                    "获取 %s 属性列表失败: %s", actor_name, e,
                )

    # Step 3: 组装 MiMo 分类 prompt（索引模式——MiMo 只输出整数）
    prompt = _build_mimo_prompt(property_index)

    # Step 4: MiMo 分类（优先）→ 白名单降级
    agent = VisionSubAgent(ctx.config)
    try:
        mimo_output = await agent.classify(prompt)
        mapping = _resolve_mimo_indices(mimo_output, property_index)
    except (ValueError, Exception) as e:
        _log.warning("MiMo 分类失败，降级到白名单: %s", e)
        mapping = _classify_by_whitelist(property_index)
        if mapping:
            _log.info("白名单降级完成: %d 个维度, %d 个属性",
                       len(mapping), sum(len(v) for v in mapping.values()))

    # Step 5: JSON → Markdown 表格
    md_content = _render_mapping_markdown(mapping)
    total_props = sum(
        len(props) for props in mapping.values()
        if isinstance(props, list)
    )

    # Step 6: 写入文件（fallback 路径）
    mapping_path = ""
    if ctx.snapshot_recorder is not None:
        try:
            from pathlib import Path as _Path
            log_base = ctx.config.log_dir
            session_name = getattr(
                ctx.snapshot_recorder, "_snapshot_dir", None,
            )
            if session_name is not None:
                session_name = _Path(getattr(
                    session_name, "name", "",  # noqa — defensive
                ))
                # snapshot_recorder._snapshot_dir is a Path
                pass
            mapping_path = str(log_base / "atmosphere-mapping.md")
            _Path(mapping_path).write_text(
                md_content, encoding="utf-8",
            )
            ctx.snapshot_recorder.set_mapping_path(mapping_path)
        except Exception as e:
            _log.warning("写入 atmosphere-mapping.md 失败: %s", e)

    # Step 7: 组装返回——内联完整映射
    ctx.ref_mapping_generated = True
    duration_ms = (time.monotonic() - t0) * 1000
    result_text = (
        "氛围组件扫描完成：\n"
        + "\n".join(scan_lines)
        + f"\n\n映射已生成：{total_props} 个氛围相关属性"
        + (f" → {mapping_path}" if mapping_path else "")
        + "\n\n---\n\n"
        + md_content
    )

    log_local_call(ctx, "build_atmosphere_mapping", arguments, result_text, t0)
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
