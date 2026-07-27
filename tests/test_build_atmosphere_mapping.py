"""测试 build_atmosphere_mapping handler — 验证工具名及 MiMo 分类流程."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp.server.lowlevel.server import request_ctx
from mcp.types import CallToolRequest, CallToolRequestParams
from mcp.shared.context import RequestContext

from harness.config import Config
from harness.server import build_server
from harness.interceptor import DebugPreCallInterceptor


@pytest.fixture
def mock_ue_client() -> AsyncMock:
    """返回一个假的 ue_client，find_actors 和 list_properties 返回预设数据."""
    client = AsyncMock()
    client.list_tools = AsyncMock(return_value=[])

    def _actor_reply(class_ref: str) -> list[dict]:
        actor_map = {
            "/Script/Engine.DirectionalLight": [{"refPath": "/Game/DirLight"}],
            "/Script/Engine.SkyAtmosphere": [{"refPath": "/Game/SkyAtmo"}],
            "/Script/Engine.ExponentialHeightFog": [{"refPath": "/Game/Fog"}],
            "/Script/Engine.VolumetricCloud": [{"refPath": "/Game/Cloud"}],
            "/Script/Engine.PostProcessVolume": [],
        }
        return actor_map.get(class_ref, [])

    # 各组件类型的真实属性集（精简版，保留氛围相关 + 无关属性以验证 MiMo 筛选）
    _component_properties: dict[str, str] = {
        "DirLight": (
            "shadowCascadeBiasDistribution: float\n"
            "bEnableLightShaftOcclusion: bool\n"
            "occlusionMaskDarkness: float\n"
            "lightSourceAngle: float\n"
            "lightSourceSoftAngle: float\n"
            "dynamicShadowDistanceMovableLight: float\n"
            "intensity: float\n"
            "lightColor: FLinearColor\n"
            "bUseTemperature: bool\n"
            "temperature: float\n"
            "bAtmosphereSunLight: bool\n"
            "specularScale: float\n"
            "indirectLightingIntensity: float\n"
            "volumetricScatteringIntensity: float\n"
        ),
        "SkyAtmo": (
            "transformMode: enum\n"
            "bottomRadius: float\n"
            "groundAlbedo: FLinearColor\n"
            "atmosphereHeight: float\n"
            "multiScatteringFactor: float\n"
            "rayleighScatteringScale: float\n"
            "rayleighScattering: FLinearColor\n"
            "rayleighExponentialDistribution: float\n"
            "mieScatteringScale: float\n"
            "mieScattering: FLinearColor\n"
            "mieAbsorptionScale: float\n"
            "mieAbsorption: FLinearColor\n"
            "mieAnisotropy: float\n"
            "mieExponentialDistribution: float\n"
            "skyLuminanceFactor: FLinearColor\n"
            "aerialPespectiveViewDistanceScale: float\n"
        ),
        "Fog": (
            "fogDensity: float\n"
            "fogHeightFalloff: float\n"
            "fogInscatteringLuminance: FLinearColor\n"
            "skyAtmosphereAmbientContributionColorScale: FLinearColor\n"
            "inscatteringColorCubemap: object\n"
            "fullyDirectionalInscatteringColorDistance: float\n"
            "nonDirectionalInscatteringColorDistance: float\n"
            "directionalInscatteringExponent: float\n"
            "directionalInscatteringStartDistance: float\n"
            "directionalInscatteringLuminance: FLinearColor\n"
            "fogMaxOpacity: float\n"
            "startDistance: float\n"
            "fogCutoffDistance: float\n"
            "bEnableVolumetricFog: bool\n"
        ),
        "Cloud": (
            "layerBottomAltitude: float\n"
            "layerHeight: float\n"
            "tracingStartMaxDistance: float\n"
            "tracingMaxDistance: float\n"
            "planetRadius: float\n"
            "groundAlbedo: FLinearColor\n"
            "material: object\n"
            "bUsePerSampleAtmosphericLightTransmittance: bool\n"
            "skyLightCloudBottomOcclusion: float\n"
            "viewSampleCountScale: float\n"
            "shadowViewSampleCountScale: float\n"
            "shadowTracingDistance: float\n"
            "stopTracingTransmittanceThreshold: float\n"
        ),
    }

    # component refPath 映射（模拟 get_properties 解析 actor 的 component 指针字段）
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

    _FIND = "toolset_registry.toolsets.core.scene.SceneTools.find_actors"
    _FIND_SHORT = "find_actors"
    _LIST = "toolset_registry.toolsets.core.object.ObjectTools.list_properties"
    _LIST_SHORT = "list_properties"
    _GET = "toolset_registry.toolsets.core.object.ObjectTools.get_properties"
    _GET_SHORT = "get_properties"

    _COMPONENT_PROPS: dict[str, str] = {
        "LightComponent0": (
            "intensity: float\n"
            "lightColor: FLinearColor\n"
            "bUseTemperature: bool\n"
            "temperature: float\n"
            "bAtmosphereSunLight: bool\n"
            "specularScale: float\n"
            "indirectLightingIntensity: float\n"
            "volumetricScatteringIntensity: float\n"
            "shadowCascadeBiasDistribution: float\n"
            "bEnableLightShaftOcclusion: bool\n"
        ),
        "SkyAtmosphereComponent": (
            "transformMode: enum\n"
            "bottomRadius: float\n"
            "groundAlbedo: FLinearColor\n"
            "atmosphereHeight: float\n"
            "multiScatteringFactor: float\n"
            "rayleighScatteringScale: float\n"
            "rayleighScattering: FLinearColor\n"
            "rayleighExponentialDistribution: float\n"
            "mieScatteringScale: float\n"
            "mieScattering: FLinearColor\n"
            "mieAbsorptionScale: float\n"
            "mieAbsorption: FLinearColor\n"
            "mieAnisotropy: float\n"
            "mieExponentialDistribution: float\n"
            "skyLuminanceFactor: FLinearColor\n"
            "aerialPespectiveViewDistanceScale: float\n"
        ),
        "HeightFogComponent0": (
            "fogDensity: float\n"
            "fogHeightFalloff: float\n"
            "fogInscatteringLuminance: FLinearColor\n"
            "skyAtmosphereAmbientContributionColorScale: FLinearColor\n"
            "inscatteringColorCubemap: object\n"
            "fullyDirectionalInscatteringColorDistance: float\n"
            "nonDirectionalInscatteringColorDistance: float\n"
            "directionalInscatteringExponent: float\n"
            "directionalInscatteringStartDistance: float\n"
            "directionalInscatteringLuminance: FLinearColor\n"
            "fogMaxOpacity: float\n"
            "startDistance: float\n"
            "fogCutoffDistance: float\n"
            "bEnableVolumetricFog: bool\n"
        ),
        "VolumetricCloudComponent": (
            "layerBottomAltitude: float\n"
            "layerHeight: float\n"
            "tracingStartMaxDistance: float\n"
            "tracingMaxDistance: float\n"
            "planetRadius: float\n"
            "groundAlbedo: FLinearColor\n"
            "material: object\n"
            "bUsePerSampleAtmosphericLightTransmittance: bool\n"
            "skyLightCloudBottomOcclusion: float\n"
            "viewSampleCountScale: float\n"
            "shadowViewSampleCountScale: float\n"
            "shadowTracingDistance: float\n"
            "stopTracingTransmittanceThreshold: float\n"
        ),
    }

    async def _mock_call_tool(name: str, args: dict) -> str:
        if name in (_FIND, _FIND_SHORT):
            # 使用 actor_type (class refPath) 进行查找
            class_ref = args.get("actor_type", {}).get("refPath", "")
            inner = json.dumps({"returnValue": _actor_reply(class_ref)})
            return json.dumps({"content": [{"type": "text", "text": inner}]})
        elif name in (_GET, _GET_SHORT):
            # get_properties: 解析 component refPath 指针字段
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
            inner = json.dumps({"returnValue": json.dumps(ref_data)})
            return json.dumps({"content": [{"type": "text", "text": inner}]})
        elif name in (_LIST, _LIST_SHORT):
            instance = args.get("instance", {})
            actor_path = instance.get("refPath", "") if isinstance(instance, dict) else str(instance)
            prop_key = ""
            # actor 级属性
            if "DirLight" in actor_path and "LightComponent" not in actor_path:
                prop_key = "DirLight"
            elif "SkyAtmo" in actor_path and "SkyAtmosphereComponent" not in actor_path:
                prop_key = "SkyAtmo"
            elif "Fog" in actor_path and "HeightFogComponent" not in actor_path:
                prop_key = "Fog"
            elif "Cloud" in actor_path and "VolumetricCloudComponent" not in actor_path:
                prop_key = "Cloud"
            # component 级属性
            elif "LightComponent" in actor_path:
                props_text = _COMPONENT_PROPS.get("LightComponent0", "")
                return json.dumps({"content": [{"type": "text", "text": props_text}]})
            elif "SkyAtmosphereComponent" in actor_path:
                props_text = _COMPONENT_PROPS.get("SkyAtmosphereComponent", "")
                return json.dumps({"content": [{"type": "text", "text": props_text}]})
            elif "HeightFogComponent" in actor_path:
                props_text = _COMPONENT_PROPS.get("HeightFogComponent0", "")
                return json.dumps({"content": [{"type": "text", "text": props_text}]})
            elif "VolumetricCloudComponent" in actor_path:
                props_text = _COMPONENT_PROPS.get("VolumetricCloudComponent", "")
                return json.dumps({"content": [{"type": "text", "text": props_text}]})
            props_text = _component_properties.get(
                prop_key,
                "LightColor: FLinearColor\nIntensity: float\nTemperature: float\n",
            )
            return json.dumps({"content": [{"type": "text", "text": props_text}]})
        return "{}"

    client.call_tool = AsyncMock(side_effect=_mock_call_tool)
    return client


@pytest.fixture
def mock_classify() -> MagicMock:
    """返回一个假的 VisionSubAgent.classify()，返回基于索引的维度映射.

    索引编号对应 mock_ue_client 中 _component_properties 的属性顺序：
      DirLight(1-14) → SkyAtmo(15-30) → Fog(31-44) → Cloud(45-57)
    """
    agent = MagicMock()
    agent.classify = AsyncMock(return_value={
        "brightness": [7],                      # intensity
        "color_temp": [8, 10],                  # lightColor, temperature
        "shadow_direction": [11],               # bAtmosphereSunLight
        "sky": [21, 29],                        # rayleighScattering, skyLuminanceFactor
        "haze": [31],                           # fogDensity
        "contrast": [19],                       # multiScatteringFactor
    })
    return agent


class TestBuildAtmosphereMapping:
    """验证 build_atmosphere_mapping handler 的正确性."""

    @pytest.mark.asyncio
    async def test_uses_correct_tool_names(
        self, mock_ue_client: AsyncMock, mock_classify: MagicMock,
    ) -> None:
        """核心测试：handler 应使用 'find_actors' 和 'list_properties' 而非带前缀的版本."""
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
            params=CallToolRequestParams(
                name="build_atmosphere_mapping", arguments={},
            ),
            jsonrpc="2.0",
            id=1,
        )

        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        with patch(
            "harness.verification.vision_agent.VisionSubAgent",
            return_value=mock_classify,
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        # ServerResult wraps CallToolResult in .root
        text = result.root.content[0].text

        # ---- 验证 1: 使用完全限定名 ----
        _FIND = "toolset_registry.toolsets.core.scene.SceneTools.find_actors"
        _LIST = "toolset_registry.toolsets.core.object.ObjectTools.list_properties"
        tool_names = [
            c.args[0] for c in mock_ue_client.call_tool.mock_calls
        ]
        assert _FIND in tool_names, (
            f"应使用完全限定名 {_FIND}，实际调用: {tool_names}"
        )
        assert _LIST in tool_names, (
            f"应使用完全限定名 {_LIST}，实际调用: {tool_names}"
        )

        # ---- 验证 2: 每个氛围类型都调了 find_actors ----
        find_calls = [
            c for c in tool_names if c == _FIND
        ]
        assert len(find_calls) == 5, (
            f"应对 5 种氛围类型各调一次 find_actors，实际 {len(find_calls)} 次"
        )

        # ---- 验证 3: 对有 Actor 的类型调了 list_properties ----
        list_calls = [
            c for c in tool_names if c == _LIST
        ]
        # 4 种有 actor (DirectionalLight, SkyAtmosphere, Fog, Cloud)，1 种无 (PostProcessVolume)
        assert len(list_calls) == 4, (
            f"应对 4 个有 actor 的类型各调一次 list_properties，实际 {len(list_calls)} 次"
        )

        # ---- 验证 4: MiMo classify() 被调用且 prompt 包含组件信息 ----
        assert mock_classify.classify.called, "MiMo classify() 未被调用"
        prompt = mock_classify.classify.call_args[0][0]
        assert "DirectionalLight" in prompt, "prompt 应包含 DirectionalLight"
        assert "SkyAtmosphere" in prompt, "prompt 应包含 SkyAtmosphere"
        assert "ExponentialHeightFog" in prompt, "prompt 应包含 Fog"
        assert "VolumetricCloud" in prompt, "prompt 应包含 Cloud"
        assert "[7] intensity" in prompt, "prompt 应包含索引属性"
        assert "[31] fogDensity" in prompt, "prompt 应包含 fog 属性"
        assert "brightness" in prompt.lower(), "prompt 应包含维度指导"
        assert "[2] bEnableLightShaftOcclusion" in prompt, (
            "prompt 应包含无关属性以验证 MiMo 筛选"
        )
        # ---- 验证 4b: 使用 actor_type 而非 glob ----
        find_call_args = [
            c.args for c in mock_ue_client.call_tool.mock_calls
            if c.args[0] in ("find_actors", _FIND)
        ]
        for call_args in find_call_args:
            params = call_args[1]
            assert "actor_type" in params, (
                f"应使用 actor_type 参数, 实际: {params}"
            )
            assert "refPath" in params.get("actor_type", {}), (
                f"actor_type 应包含 class refPath, 实际: {params}"
            )

        # ---- 验证 5: 返回体包含扫描摘要和映射表 ----
        assert "DirectionalLight: 1 个" in text, "扫描摘要应列出找到的组件"
        assert "PostProcessVolume: 未找到" in text, "应标记未找到的组件"
        assert "## 亮度 (Brightness)" in text, "映射表应包含维度章节"
        assert "## 色温 (Color Temperature)" in text, "映射表应包含色温维度"
        assert "## 大气密度 (Haze)" in text, "映射表应包含大气密度维度"
        assert "| DirectionalLight |" in text, "映射表应包含 DirectionalLight 的属性"
        assert "| ExponentialHeightFog |" in text, "映射表应包含 Fog 的属性"
        assert "| SkyAtmosphere |" in text, "映射表应包含 SkyAtmosphere 的属性"
        assert "氛围相关属性" in text, "应显示属性计数"

    @pytest.mark.asyncio
    async def test_fallback_on_mimo_failure(
        self, mock_ue_client: AsyncMock,
    ) -> None:
        """MiMo 分类失败时，应返回原始属性列表作为降级."""
        server = build_server(
            config=Config(),
            ue_client=mock_ue_client,
            interceptors=[DebugPreCallInterceptor()],
            skills_dir=Path("skills"),
        )

        mock_failing_agent = MagicMock()
        mock_failing_agent.classify = AsyncMock(
            side_effect=ValueError("MiMo 不可用"),
        )

        from mcp.types import CallToolRequest as CTR
        handler_fn = server.request_handlers[CTR]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="build_atmosphere_mapping", arguments={},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-2", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        with patch(
            "harness.verification.vision_agent.VisionSubAgent",
            return_value=mock_failing_agent,
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        text = result.root.content[0].text
        # MiMo 失败时，白名单降级应正常生成映射表
        assert "氛围相关属性" in text, "应显示属性计数"
        # 白名单中的氛围属性应出现在输出中
        assert "lightColor" in text, "白名单应包含 lightColor"
        assert "intensity" in text, "白名单应包含 intensity"
        assert "fogDensity" in text, "白名单应包含 fogDensity"

    @pytest.mark.asyncio
    async def test_all_actors_empty_scenario(
        self, mock_ue_client: AsyncMock,
    ) -> None:
        """场景中无任何氛围组件时，返回空映射."""
        # 覆盖 mock：全部返回空
        _FIND = "toolset_registry.toolsets.core.scene.SceneTools.find_actors"
        mock_ue_client.call_tool.side_effect = (
            lambda name, args: (
                json.dumps({"returnValue": []})
                if name == _FIND
                else "{}"
            )
        )

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
            params=CallToolRequestParams(
                name="build_atmosphere_mapping", arguments={},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-3", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        token = request_ctx.set(ctx)
        try:
            result = await handler_fn(req)
        finally:
            request_ctx.reset(token)

        text = result.root.content[0].text
        assert "共 0 个氛围相关属性" in text
        assert "未找到" in text  # 所有组件都标记为未找到


# 固定输出目录——测试生成物不参与 git，仅用于人工验收
_OUTPUT_DIR = Path(__file__).parent / "output"


class TestBuildAtmosphereMappingFileOutput:
    """验证 mapping.md 文件落盘."""

    @pytest.mark.asyncio
    async def test_writes_mapping_file(
        self, mock_ue_client: AsyncMock, mock_classify: MagicMock,
    ) -> None:
        """验证 build_atmosphere_mapping 将 mapping.md 写入 tests/output/."""
        from harness.state.models import WorldState
        from harness.observability.snapshotter import SnapshotRecorder

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cache = WorldState()
        recorder = SnapshotRecorder(snapshot_dir=_OUTPUT_DIR, cache=cache)

        server = build_server(
            config=Config(log_dir=_OUTPUT_DIR),
            ue_client=mock_ue_client,
            interceptors=[DebugPreCallInterceptor()],
            skills_dir=Path("skills"),
            snapshot_recorder=recorder,
        )

        from mcp.types import CallToolRequest as CTR
        handler_fn = server.request_handlers[CTR]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="build_atmosphere_mapping", arguments={},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        with patch(
            "harness.verification.vision_agent.VisionSubAgent",
            return_value=mock_classify,
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        text = result.root.content[0].text

        # ---- 验证 1: mapping.md 文件已创建 ----
        mapping_path = _OUTPUT_DIR / "atmosphere-mapping.md"
        assert mapping_path.exists(), (
            f"mapping.md 未生成于 {_OUTPUT_DIR}，"
            f"目录内容: {list(_OUTPUT_DIR.iterdir())}"
        )

        # ---- 验证 2: 文件内容与 tool response 中的映射表一致 ----
        file_content = mapping_path.read_text(encoding="utf-8")
        assert "Atmosphere Mapping" in file_content
        assert "## 亮度 (Brightness)" in file_content
        assert "## 色温 (Color Temperature)" in file_content
        assert "| DirectionalLight |" in file_content
        assert "氛围相关属性" in file_content

        # ---- 验证 3: tool response 包含文件路径 ----
        mapping_path_str = str(mapping_path)
        assert mapping_path_str in text, (
            f"response 应包含 mapping 文件路径，实际: {text[-200:]}"
        )

        # ---- 验证 4: SnapshotRecorder 记录了 mapping_path ----
        assert recorder._mapping_path is not None
        assert "atmosphere-mapping.md" in recorder._mapping_path


# ============================================================
# MiMo Index Pipeline Tests (Plan 2026-07-12)
# ============================================================


class TestBuildPropertyIndex:
    """Task 1: _build_property_index structure and correctness."""

    def test_build_property_index_structure(self):
        """Each entry should carry index, actor_type, actor_name, refPath, property."""
        from harness.verification.atmosphere import _build_property_index

        entries, next_idx = _build_property_index(
            actor_type="DirectionalLight",
            actor_name="/Game/DirLight",
            actor_prop_names=["primaryActorTick", "bHidden", "lightComponent"],
            component_refs={"lightComponent": "/Game/DirLight.LightComponent0"},
            comp_prop_names={"lightComponent": ["intensity", "lightColor"]},
            start_index=1,
        )

        assert len(entries) == 4, f"2 actor props + 2 component props = 4, got {len(entries)}"
        assert next_idx == 5
        # Actor-level props keep actor_name as refPath
        assert entries[0] == {
            "index": 1, "actor_type": "DirectionalLight",
            "actor_name": "/Game/DirLight", "refPath": "/Game/DirLight",
            "property": "primaryActorTick",
        }
        assert entries[1]["refPath"] == "/Game/DirLight"
        # Component pointer field is NOT emitted; its children replace it
        assert entries[2]["refPath"] == "/Game/DirLight.LightComponent0"
        assert entries[2]["property"] == "intensity"
        assert entries[3]["property"] == "lightColor"
        # Index is sequential
        assert [e["index"] for e in entries] == [1, 2, 3, 4]

    def test_build_property_index_no_components(self):
        """Actor with no component refs: all props get actor refPath."""
        from harness.verification.atmosphere import _build_property_index

        entries, next_idx = _build_property_index(
            actor_type="PostProcessVolume",
            actor_name="/Game/PPV",
            actor_prop_names=["settings", "bUnbound", "priority"],
            component_refs={},
            comp_prop_names={},
            start_index=5,
        )

        assert len(entries) == 3
        assert next_idx == 8
        for e in entries:
            assert e["refPath"] == "/Game/PPV", f"{e['property']} should have actor refPath"


class TestBuildMimoPrompt:
    """Task 2: _build_mimo_prompt format."""

    def test_build_mimo_prompt_uses_indices(self):
        """Prompt should use [N] notation and instruct MiMo to output integers."""
        from harness.verification.atmosphere import _build_mimo_prompt

        entries = [
            {"index": 1, "actor_type": "DirectionalLight", "actor_name": "/Game/DL",
             "refPath": "/Game/DL", "property": "primaryActorTick"},
            {"index": 2, "actor_type": "DirectionalLight", "actor_name": "/Game/DL",
             "refPath": "/Game/DL.LightComponent0", "property": "intensity"},
        ]

        prompt = _build_mimo_prompt(entries)

        assert "[1]" in prompt
        assert "[2]" in prompt
        assert "primaryActorTick" in prompt
        assert "intensity" in prompt
        # Must NOT ask for actor_type/property strings in output example
        assert '"actor_type"' not in prompt
        assert '"property"' not in prompt
        # Must contain the 8 dimensions
        assert "brightness" in prompt

    def test_build_mimo_prompt_shows_component_hint(self):
        """Component-level props should show which component they belong to."""
        from harness.verification.atmosphere import _build_mimo_prompt

        entries = [
            {"index": 1, "actor_type": "DirectionalLight", "actor_name": "/Game/DL",
             "refPath": "/Game/DL.LightComponent0", "property": "intensity"},
        ]

        prompt = _build_mimo_prompt(entries)
        # Should hint that this is on a component
        assert "LightComponent0" in prompt


class TestResolveMimoIndices:
    """Task 3: _resolve_mimo_indices correctness and edge cases."""

    def test_resolve_mimo_indices_normal(self):
        """Valid indices should map back to correct property entries."""
        from harness.verification.atmosphere import _resolve_mimo_indices

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

    def test_resolve_mimo_indices_filters_invalid(self):
        """Out-of-range and non-integer indices should be silently dropped."""
        from harness.verification.atmosphere import _resolve_mimo_indices

        property_index = [
            {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
             "refPath": "/Game/DL.LC0", "property": "intensity"},
        ]

        mimo_output = {"brightness": [1, 99], "contrast": ["abc", 0]}

        result = _resolve_mimo_indices(mimo_output, property_index)

        assert len(result["brightness"]) == 1
        assert result["brightness"][0]["property"] == "intensity"
        assert "contrast" not in result

    def test_resolve_mimo_indices_empty_dimension_skipped(self):
        """Dimensions with no valid indices should not appear in result."""
        from harness.verification.atmosphere import _resolve_mimo_indices

        property_index = [
            {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
             "refPath": "/Game/DL", "property": "bHidden"},
        ]

        mimo_output = {"brightness": [99], "haze": []}
        result = _resolve_mimo_indices(mimo_output, property_index)

        assert "brightness" not in result
        assert "haze" not in result

    def test_resolve_mimo_indices_value_types(self):
        """Float indices (1.0) should work; string "1" should work."""
        from harness.verification.atmosphere import _resolve_mimo_indices

        property_index = [
            {"index": 1, "actor_type": "DL", "actor_name": "/Game/DL",
             "refPath": "/Game/DL", "property": "intensity"},
        ]

        # JSON sometimes parses numbers as float
        result = _resolve_mimo_indices({"brightness": [1.0]}, property_index)
        assert len(result["brightness"]) == 1

        # Or as string
        result = _resolve_mimo_indices({"brightness": ["1"]}, property_index)
        assert len(result["brightness"]) == 1


class TestRenderMappingWithRefPath:
    """Task 4: _render_mapping_markdown includes refPath column."""

    def test_render_mapping_includes_refpath(self):
        """Markdown table should include refPath column."""
        from harness.verification.atmosphere import _render_mapping_markdown

        mapping = {
            "brightness": [
                {"actor_type": "DirectionalLight",
                 "refPath": "/Game/DL.LightComponent0",
                 "property": "intensity"},
            ],
        }

        md = _render_mapping_markdown(mapping)

        assert "属性位置" in md
        assert "/Game/DL.LightComponent0" in md
        assert "intensity" in md
