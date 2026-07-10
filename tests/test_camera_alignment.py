"""测试 match_reference 视角自动对齐 — 验证相机修改 + 重截图."""

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
from harness.verification.capturer import Screenshot


_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_SCREENSHOT = Screenshot(data_b64=_TINY_PNG_B64, width=10, height=10)


@pytest.fixture
def viewpoint_pitch_45() -> MagicMock:
    """VisionSubAgent.check() → 参考图 pitch=-5, 当前截图 pitch=-55 (diff=50>30)."""
    agent = MagicMock()
    count = [0]

    async def _check(image_b64, question):
        count[0] += 1
        result = MagicMock()
        result.answer = (
            '{"pitch": -5, "height_offset": 170}'
            if count[0] == 1
            else '{"pitch": -55, "height_offset": 3000}'
        )
        return result

    agent.check = _check
    return agent


@pytest.fixture
def viewpoint_pitch_5() -> MagicMock:
    """VisionSubAgent.check() → diff=5 < 30, 不触发修正."""
    agent = MagicMock()
    count = [0]

    async def _check(image_b64, question):
        count[0] += 1
        result = MagicMock()
        result.answer = (
            '{"pitch": -15, "height_offset": 170}'
            if count[0] == 1
            else '{"pitch": -20, "height_offset": 200}'
        )
        return result

    agent.check = _check
    return agent


@pytest.fixture
def compare_verdict() -> MagicMock:
    """8 维度对比返回."""
    agent = MagicMock()
    agent.compare_with_reference = AsyncMock(return_value=MagicMock(
        answer="所有维度 similar",
        confidence="medium",
        caveats=[],
        observations=[],
    ))
    return agent


@pytest.fixture
def ue_with_landscape() -> AsyncMock:
    """UE client: Landscape bounds + 相机位置."""
    client = AsyncMock()
    client.list_tools = AsyncMock(return_value=[])

    async def _ct(name: str, args: dict) -> str:
        if "find_actors" in name and "Landscape" in args.get("glob", ""):
            return json.dumps({
                "returnValue": [{"refPath": "/Game/Landscape_0"}]
            })
        if "get_actor_bounds" in name:
            return json.dumps({
                "returnValue": {
                    "origin": {"x": 0, "y": 0, "z": 500},
                    "boxExtent": {"x": 10000, "y": 10000, "z": 100},
                }
            })
        if "GetCameraTransform" in name:
            return json.dumps({
                "returnValue": {
                    "location": {"x": 2000, "y": 3000, "z": 4000},
                    "rotation": {"pitch": -55, "yaw": 45, "roll": 0},
                }
            })
        if "SetCameraTransform" in name:
            return json.dumps({"returnValue": None})
        if "find_actors" in name:
            return json.dumps({"returnValue": []})
        return json.dumps({})

    client.call_tool = AsyncMock(side_effect=_ct)
    return client


class TestCameraAlignment:
    """验证 match_reference 视角自动对齐."""

    @pytest.mark.asyncio
    async def test_camera_corrected_when_pitch_diff_exceeds_threshold(
        self,
        ue_with_landscape: AsyncMock,
        viewpoint_pitch_45: MagicMock,
        compare_verdict: MagicMock,
        tmp_path: Path,
    ) -> None:
        """pitch 差 50° → SetCameraTransform + 重截图."""
        from PIL import Image as PILImage
        ref_path = tmp_path / "ref.png"
        PILImage.new("RGB", (10, 10), (100, 100, 100)).save(ref_path)

        server = build_server(
            config=Config(),
            ue_client=ue_with_landscape,
            interceptors=[DebugPreCallInterceptor()],
            skills_dir=Path("skills"),
        )

        from mcp.types import CallToolRequest as CTR
        handler_fn = server.request_handlers[CTR]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="match_reference", arguments={"path": str(ref_path)},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        # _analyze_viewpoint 用模块级 import → patch server module
        # call_tool 内用本地 import → patch vision_agent module
        # 注意: 两个 patch 各消耗自己的 side_effect 列表，不能共用
        with (
            patch("harness.server.VisionSubAgent",
                  side_effect=[viewpoint_pitch_45, viewpoint_pitch_45]),
            patch("harness.verification.vision_agent.VisionSubAgent",
                  return_value=compare_verdict),
            patch("harness.verification.capturer.capture",
                  return_value=_SCREENSHOT),
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        text = result.root.content[0].text

        # ---- 验证 1: SetCameraTransform 被调用 ----
        set_cam_calls = [
            c for c in ue_with_landscape.call_tool.mock_calls
            if "SetCameraTransform" in str(c.args)
        ]
        assert len(set_cam_calls) >= 1, (
            f"视角偏差 50° > 30° 阈值应触发 SetCameraTransform，"
            f"实际调用: {[c.args[0] for c in ue_with_landscape.call_tool.mock_calls]}"
        )

        # ---- 验证 2: pitch 为参考图的 -5° ----
        call_str = str(set_cam_calls[0].args)
        assert "-5" in call_str or "-5.0" in call_str, (
            f"相机 pitch 应为 -5°，实际: {set_cam_calls[0].args}"
        )

        # ---- 验证 3: z = landscape_surface + height_offset = 600 + 170 = 770 ----
        assert "770" in call_str or "770.0" in call_str, (
            f"相机 z 应为 770 (600+170), 实际: {call_str}"
        )

        # ---- 验证 4: 返回体含视角修正摘要 ----
        assert "视角已自动修正" in text, (
            f"返回体应包含视角修正摘要，实际: {text[:300]}"
        )

    @pytest.mark.asyncio
    async def test_no_correction_when_pitch_diff_within_threshold(
        self,
        ue_with_landscape: AsyncMock,
        viewpoint_pitch_5: MagicMock,
        compare_verdict: MagicMock,
        tmp_path: Path,
    ) -> None:
        """偏差 5° < 30° → 不触发修正."""
        from PIL import Image as PILImage
        ref_path = tmp_path / "ref.png"
        PILImage.new("RGB", (10, 10), (100, 100, 100)).save(ref_path)

        server = build_server(
            config=Config(),
            ue_client=ue_with_landscape,
            interceptors=[DebugPreCallInterceptor()],
            skills_dir=Path("skills"),
        )

        from mcp.types import CallToolRequest as CTR
        handler_fn = server.request_handlers[CTR]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="match_reference", arguments={"path": str(ref_path)},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-2", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        with (
            patch("harness.server.VisionSubAgent",
                  side_effect=[viewpoint_pitch_5, viewpoint_pitch_5]),
            patch("harness.verification.vision_agent.VisionSubAgent",
                  return_value=compare_verdict),
            patch("harness.verification.capturer.capture",
                  return_value=_SCREENSHOT),
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        text = result.root.content[0].text
        assert "视角已自动修正" not in text, (
            f"偏差 5° < 30° 不应触发修正，实际: {text[:200]}"
        )
