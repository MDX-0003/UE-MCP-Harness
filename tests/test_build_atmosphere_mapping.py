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

    def _actor_reply(glob_val: str) -> list[dict]:
        actor_map = {
            "*DirectionalLight*": [{"refPath": "/Game/DirLight"}],
            "*SkyAtmosphere*": [{"refPath": "/Game/SkyAtmo"}],
            "*ExponentialHeightFog*": [{"refPath": "/Game/Fog"}],
            "*VolumetricCloud*": [{"refPath": "/Game/Cloud"}],
            "*PostProcessVolume*": [],
        }
        return actor_map.get(glob_val, [])

    async def _mock_call_tool(name: str, args: dict) -> str:
        if name == "find_actors":
            return json.dumps({"returnValue": _actor_reply(args.get("glob", ""))})
        elif name == "list_properties":
            return json.dumps({
                "content": [{
                    "type": "text",
                    "text": ("LightColor: FLinearColor\n"
                             "Intensity: float\n"
                             "Temperature: float\n"),
                }]
            })
        return "{}"

    client.call_tool = AsyncMock(side_effect=_mock_call_tool)
    return client


@pytest.fixture
def mock_classify() -> MagicMock:
    """返回一个假的 VisionSubAgent.classify()，返回预设维度映射."""
    agent = MagicMock()
    agent.classify = AsyncMock(return_value={
        "brightness": [{"actor_type": "DirectionalLight", "property": "Intensity"}],
        "color_temp": [{"actor_type": "DirectionalLight", "property": "LightColor"}],
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

        # ---- 验证 1: 使用短名，无前缀 ----
        tool_names = [
            c.args[0] for c in mock_ue_client.call_tool.mock_calls
        ]
        assert "find_actors" in tool_names, (
            f"find_actors 未被调用，实际调用: {tool_names}"
        )
        assert "list_properties" in tool_names, (
            f"list_properties 未被调用，实际调用: {tool_names}"
        )
        assert "SceneTools.find_actors" not in tool_names, (
            f"不应使用 SceneTools. 前缀，实际调用: {tool_names}"
        )
        assert "ObjectTools.list_properties" not in tool_names, (
            f"不应使用 ObjectTools. 前缀，实际调用: {tool_names}"
        )

        # ---- 验证 2: 每个氛围类型都调了 find_actors ----
        find_calls = [
            c for c in tool_names if c == "find_actors"
        ]
        assert len(find_calls) == 5, (
            f"应对 5 种氛围类型各调一次 find_actors，实际 {len(find_calls)} 次"
        )

        # ---- 验证 3: 对有 Actor 的类型调了 list_properties ----
        list_calls = [
            c for c in tool_names if c == "list_properties"
        ]
        # 4 种有 actor (DirectionalLight, SkyAtmosphere, Fog, Cloud)，1 种无 (PostProcessVolume)
        assert len(list_calls) == 4, (
            f"应对 4 个有 actor 的类型各调一次 list_properties，实际 {len(list_calls)} 次"
        )

        # ---- 验证 4: MiMo classify() 被调用且 prompt 包含组件信息 ----
        assert mock_classify.classify.called, "MiMo classify() 未被调用"
        prompt = mock_classify.classify.call_args[0][0]
        assert "DirectionalLight" in prompt, "prompt 应包含组件名"
        assert "SkyAtmosphere" in prompt, "prompt 应包含组件名"
        assert "Intensity" in prompt, "prompt 应包含属性名"
        assert "brightness" in prompt.lower(), "prompt 应包含维度名"

        # ---- 验证 5: 返回体包含扫描摘要和映射表 ----
        assert "DirectionalLight: 1 个" in text, "扫描摘要应列出找到的组件"
        assert "PostProcessVolume: 未找到" in text, "应标记未找到的组件"
        assert "## 亮度 (Brightness)" in text, "映射表应包含维度章节"
        assert "| DirectionalLight | Intensity |" in text, "映射表应包含属性行"
        assert "2 个氛围相关属性" in text, "应显示属性计数"

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
        # 降级输出应以警告开头
        assert "MiMo 分类失败" in text, "应显示 MiMo 失败警告"
        # 应包含原始属性名
        assert "LightColor" in text, "降级输出应包含原始属性列表"
        assert "Intensity" in text, "降级输出应包含原始属性列表"

    @pytest.mark.asyncio
    async def test_all_actors_empty_scenario(
        self, mock_ue_client: AsyncMock,
    ) -> None:
        """场景中无任何氛围组件时，返回空映射."""
        # 覆盖 mock：全部返回空
        mock_ue_client.call_tool.side_effect = (
            lambda name, args: (
                json.dumps({"returnValue": []})
                if name == "find_actors"
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


class TestBuildAtmosphereMappingFileOutput:
    """验证 mapping.md 文件落盘."""

    @pytest.mark.asyncio
    async def test_writes_mapping_file(
        self, mock_ue_client: AsyncMock, mock_classify: MagicMock,
        tmp_path: Path,
    ) -> None:
        """验证 build_atmosphere_mapping 将 mapping.md 写入 log 目录."""
        from harness.state.models import WorldState
        from harness.observability.snapshotter import SnapshotRecorder

        cache = WorldState()
        recorder = SnapshotRecorder(snapshot_dir=tmp_path, cache=cache)

        server = build_server(
            config=Config(log_dir=tmp_path),
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
        mapping_path = tmp_path / "atmosphere-mapping.md"
        assert mapping_path.exists(), (
            f"mapping.md 未生成于 {tmp_path}，目录内容: {list(tmp_path.iterdir())}"
        )

        # ---- 验证 2: 文件内容与 tool response 中的映射表一致 ----
        file_content = mapping_path.read_text(encoding="utf-8")
        assert "Atmosphere Mapping" in file_content
        assert "## 亮度 (Brightness)" in file_content
        assert "| DirectionalLight | Intensity |" in file_content
        # 文件内容应与 response 的映射表部分一致
        assert "2 个氛围相关属性" in file_content

        # ---- 验证 3: tool response 包含文件路径 ----
        mapping_path_str = str(mapping_path)
        assert mapping_path_str in text, (
            f"response 应包含 mapping 文件路径，实际: {text[-200:]}"
        )

        # ---- 验证 4: SnapshotRecorder 记录了 mapping_path ----
        assert recorder._mapping_path is not None
        assert "atmosphere-mapping.md" in recorder._mapping_path
