"""测试 VisionInterceptor — 截图工具调用后自动触发 Vision 分析 (Issue 007)

验证 4 个核心场景:
  1. 截图工具调用 → post_call 触发 Vision 分析
  2. Vision 结果正确写入 WorldState.last_vision_verdict
  3. 非截图工具调用 → 不触发 Vision 分析
  4. Vision 分析失败 → 不影响主流程（error 容忍）
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from harness.interceptor import ToolCallCompleted
from harness.state.models import WorldState
from harness.verification.capturer import Screenshot
from harness.verification.interceptor import (
    ReadbackInterceptor,
    VisionInterceptor,
)
from harness.verification.vision_agent import VisionSubAgent, VisionVerdict

# 1×1 透明 PNG 的 base64 — 可通过 parse_screenshot PIL 解码
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ---- Fixtures ----

@pytest.fixture
def world_state() -> WorldState:
    return WorldState()


@pytest.fixture
def mock_vision_agent() -> VisionSubAgent:
    """创建一个 mock VisionSubAgent，check() 返回预设判决。"""
    from harness.config import Config
    agent = VisionSubAgent(Config(vision_api_key="test-key"))
    return agent


@pytest.fixture
def passing_verdict() -> VisionVerdict:
    return VisionVerdict(
        answer="光照角度正确，阴影长度符合预期",
        confidence="high",
        caveats=[],
        observations=[
            {"what": "光照角度", "finding": "方向光角度约为 30 度", "confidence": "high"},
        ],
    )


@pytest.fixture
def failing_verdict() -> VisionVerdict:
    return VisionVerdict(
        answer="亮度过高，方向光角度仍接近正午",
        confidence="high",
        caveats=["当前截图曝光度较高，可能影响亮度判断"],
        observations=[
            {"what": "亮度", "finding": "过曝，高光区域丢失细节", "confidence": "high"},
        ],
    )


# ---- Helper: build ToolCallCompleted ----

def _screenshot_event(name: str, image_b64: str = _TINY_PNG_B64) -> ToolCallCompleted:
    """构建模拟的截图工具调用完成事件。"""
    return ToolCallCompleted(
        name=name,
        args={},
        raw_result={
            "content": [
                {"type": "image", "data": image_b64, "mimeType": "image/png"}
            ]
        },
        parsed_text="[image: image/png]",
        error=None,
        duration_ms=150.0,
    )


def _non_screenshot_event(name: str = "SceneTools.find_actors") -> ToolCallCompleted:
    """构建模拟的非截图工具调用完成事件。"""
    return ToolCallCompleted(
        name=name,
        args={"glob": "*Light*"},
        raw_result={"content": [{"type": "text", "text": "Found 3 actors"}]},
        parsed_text="Found 3 actors",
        error=None,
        duration_ms=50.0,
    )


def _error_event(name: str = "ToolsetRegistry.EditorAppToolset.CaptureEditorImage") -> ToolCallCompleted:
    """构建模拟的工具调用错误事件。"""
    return ToolCallCompleted(
        name=name,
        args={},
        raw_result=None,
        parsed_text=None,
        error=Exception("Screenshot failed"),
        duration_ms=100.0,
    )


# ---- Tests ----

class TestVisionInterceptorBasic:
    """基础行为：触发 vs 不触发。"""

    async def test_screenshot_tool_triggers_vision(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """截图工具调用成功后，应触发 Vision 分析。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            # image_b64 是第一个位置参数（parse_screenshot 对真实 PNG 做 PIL 解码+重编码，base64 值与输入可能不同）
            assert mock_check.call_args.args[0] is not None

    async def test_non_screenshot_tool_skips_vision(
        self, mock_vision_agent, world_state
    ) -> None:
        """非截图工具调用不应触发 Vision 分析。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _non_screenshot_event()
            await interceptor.post_call(event)

            mock_check.assert_not_called()

    async def test_error_event_skips_vision(
        self, mock_vision_agent, world_state
    ) -> None:
        """工具调用失败（error 非 None）时不触发 Vision 分析。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _error_event()
            await interceptor.post_call(event)

            mock_check.assert_not_called()

    async def test_no_image_data_skips_vision(
        self, mock_vision_agent, world_state
    ) -> None:
        """截图工具的返回结果中无图片数据时，不触发 Vision 分析。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = ToolCallCompleted(
                name="ToolsetRegistry.EditorAppToolset.CaptureEditorImage",
                args={},
                raw_result={"content": [{"type": "text", "text": "no image here"}]},
                parsed_text="no image here",
                error=None,
                duration_ms=150.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_not_called()


class TestVisionInterceptorResultWriting:
    """Vision 结果写入 WorldState。"""

    async def test_passing_verdict_written_to_cache(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            assert world_state.last_vision_verdict is not None
            assert world_state.last_vision_verdict["confidence"] == "high"
            assert "光照" in world_state.last_vision_verdict["answer"]
            assert "at" in world_state.last_vision_verdict

    async def test_failing_verdict_written_to_cache(
        self, mock_vision_agent, world_state, failing_verdict
    ) -> None:
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = failing_verdict

            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            assert world_state.last_vision_verdict is not None
            assert world_state.last_vision_verdict["confidence"] == "high"
            assert "亮度" in world_state.last_vision_verdict["answer"]
            assert len(world_state.last_vision_verdict.get("caveats", [])) > 0

    async def test_vision_verdict_overwrites_previous(
        self, mock_vision_agent, world_state, passing_verdict, failing_verdict
    ) -> None:
        """重复截图时，新判决覆盖旧判决。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            # First call: passing
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            await interceptor.post_call(
                _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            )
            assert world_state.last_vision_verdict["confidence"] == "high"
            assert "光照" in world_state.last_vision_verdict["answer"]

            # Second call: failing (overwrites)
            mock_check.return_value = failing_verdict
            await interceptor.post_call(
                _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            )
            assert world_state.last_vision_verdict["confidence"] == "high"
            assert "亮度" in world_state.last_vision_verdict["answer"]


class TestVisionInterceptorFailureTolerance:
    """Vision 分析失败不应阻断主流程。"""

    async def test_vision_api_error_does_not_raise(
        self, mock_vision_agent, world_state
    ) -> None:
        """Vision API 抛异常时，post_call 不应向上传播异常。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.side_effect = RuntimeError("Vision API timeout")

            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")

            # Should not raise
            await interceptor.post_call(event)

            # last_vision_verdict should be unchanged (or set to error marker)
            # Either None or an error entry — both are acceptable as long as no crash

    async def test_vision_failure_keeps_previous_verdict(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """Vision 成功后再次截图失败，保留上一次的成功判决。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            # First: success
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)
            await interceptor.post_call(
                _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            )
            assert world_state.last_vision_verdict is not None
            previous = world_state.last_vision_verdict

            # Second: failure
            mock_check.side_effect = RuntimeError("Vision API timeout")
            await interceptor.post_call(
                _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            )

            # Should still have the previous verdict
            assert world_state.last_vision_verdict == previous


class TestVisionInterceptorToolDetection:
    """截图工具名检测：各种变体都能正确识别。"""

    SCREENSHOT_NAMES = [
        "ToolsetRegistry.EditorAppToolset.CaptureEditorImage",
        "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
        "Screenshot",
        "CaptureEditorImage",
        "CaptureAssetImage",
    ]

    NON_SCREENSHOT_NAMES = [
        "SceneTools.find_actors",
        "ActorTools.set_actor_transform",
        "ObjectTools.set_properties",
        "load_level",
        "SetCameraTransform",
        "GetVisibleActors",
    ]

    async def test_all_screenshot_names_detected(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """所有截图工具名变体都应被识别并触发 Vision。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)

            for name in self.SCREENSHOT_NAMES:
                mock_check.reset_mock()
                event = _screenshot_event(name)
                await interceptor.post_call(event)
                mock_check.assert_called_once(), f"Failed to detect: {name}"

    async def test_non_screenshot_names_not_detected(
        self, mock_vision_agent, world_state
    ) -> None:
        """非截图工具名都不应触发 Vision。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            interceptor = VisionInterceptor(mock_vision_agent, world_state)

            for name in self.NON_SCREENSHOT_NAMES:
                mock_check.reset_mock()
                event = _non_screenshot_event(name)
                await interceptor.post_call(event)
                mock_check.assert_not_called(), f"Incorrectly detected: {name}"


class TestVisionInterceptorSkillIntegration:
    """与 Skill 系统的集成：从活跃 Skill 提取 verification.expected。"""

    async def test_expected_from_active_skill(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """活跃 Skill 的 verification.expected 应传递给 Vision。"""
        active_skill = {
            "name": "evening-lighting",
            "verification": {
                "type": "screenshot",
                "expected": "场景具有温暖的低角度光照和长阴影",
                "tolerance": 0.7,
            },
        }

        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_active_skill=lambda: active_skill,
            )
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            call_kwargs = mock_check.call_args.kwargs
            assert call_kwargs["expected"] == "场景具有温暖的低角度光照和长阴影"
            assert call_kwargs["tolerance"] == 0.7

    async def test_no_active_skill_passes_none_expected(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """无活跃 Skill 时，expected 应传 None（走自由描述模式）。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_active_skill=lambda: None,
            )
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            call_kwargs = mock_check.call_args.kwargs
            assert call_kwargs["expected"] is None

    async def test_skill_without_verification_section(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """Skill 无 verification 字段时，expected 应为 None。"""
        active_skill = {"name": "simple-skill", "steps": "1. do something"}

        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_active_skill=lambda: active_skill,
            )
            event = _screenshot_event("ToolsetRegistry.EditorAppToolset.CaptureEditorImage")
            await interceptor.post_call(event)

            call_kwargs = mock_check.call_args.kwargs
            assert call_kwargs["expected"] is None


class TestVisionInterceptorImageExtraction:
    """从不同格式的工具返回中提取图片 base64。

    _parse_and_resize → parse_screenshot（重构后）对真实 PNG 做 PIL 解码+重编码，
    输出 b64 与输入不完全相同（PNG re-encoding），因此断言验证 base64 非空即可。
    """

    # 1×1 透明 PNG 的 base64 编码
    TINY_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    async def test_image_from_content_image_block(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """MCP content array 中的 image block → 提取 base64。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)

            event = ToolCallCompleted(
                name="CaptureEditorImage",
                args={},
                raw_result={
                    "content": [
                        {"type": "image", "data": self.TINY_PNG_B64, "mimeType": "image/png"}
                    ]
                },
                parsed_text="[image: image/png]",
                error=None,
                duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            assert mock_check.call_args.args[0]  # 非空 base64
            assert len(mock_check.call_args.args[0]) > 50

    async def test_image_from_nested_text_block(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """文本块中嵌套的 returnValue.data → 提取 base64。"""
        import json
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)

            inner = json.dumps({
                "returnValue": {"mimeType": "image/png", "data": self.TINY_PNG_B64}
            })
            event = ToolCallCompleted(
                name="CaptureEditorImage",
                args={},
                raw_result={
                    "content": [{"type": "text", "text": inner}]
                },
                parsed_text=inner,
                error=None,
                duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            assert mock_check.call_args.args[0]  # 非空 base64

    async def test_image_from_data_uri(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """data:image URI 格式 → 提取 base64。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict
            interceptor = VisionInterceptor(mock_vision_agent, world_state)

            data_uri = f"data:image/png;base64,{self.TINY_PNG_B64}"
            event = ToolCallCompleted(
                name="CaptureEditorImage",
                args={},
                raw_result={
                    "content": [{"type": "text", "text": data_uri}]
                },
                parsed_text=data_uri,
                error=None,
                duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            assert mock_check.call_args.args[0]  # 非空 base64


class TestVisionInterceptorVisionScreenshot:
    """Harness vision_screenshot 工具 — 通过 get_pending_screenshot 回调注入 Screenshot。"""

    async def test_vision_screenshot_triggers_vision(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """vision_screenshot 工具 → VisionInterceptor 通过回调获取 Screenshot → 调 Vision。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            screenshot = Screenshot(data_b64=_TINY_PNG_B64, width=1, height=1)
            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_pending_screenshot=lambda: screenshot,
            )
            event = ToolCallCompleted(
                name="vision_screenshot",
                args={},
                raw_result={"content": [{"type": "text", "text": "Screenshot 已获取: 1x1 image/png"}]},
                parsed_text="Screenshot 已获取: 1x1 image/png",
                error=None,
                duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            assert mock_check.call_args.args[0]  # 非空 base64

    async def test_vision_screenshot_null_callback_skips(
        self, mock_vision_agent, world_state
    ) -> None:
        """get_pending_screenshot 返回 None → 跳过 Vision 分析。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_pending_screenshot=lambda: None,
            )
            event = ToolCallCompleted(
                name="vision_screenshot",
                args={},
                raw_result={"content": [{"type": "text", "text": "截图失败: ..."}]},
                parsed_text="截图失败: ...",
                error=None,
                duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_not_called()

    async def test_vision_screenshot_with_skill_expected(
        self, mock_vision_agent, world_state, passing_verdict
    ) -> None:
        """vision_screenshot + 活跃 Skill 的 verification.expected → 传给 Vision。"""
        active_skill = {
            "name": "evening-lighting",
            "verification": {
                "type": "screenshot",
                "expected": "黄昏光照",
                "tolerance": 0.85,
            },
        }
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = passing_verdict

            screenshot = Screenshot(data_b64=_TINY_PNG_B64, width=1, height=1)
            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_active_skill=lambda: active_skill,
                get_pending_screenshot=lambda: screenshot,
            )
            event = ToolCallCompleted(
                name="vision_screenshot", args={},
                raw_result={}, parsed_text="...", error=None, duration_ms=200.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_called_once()
            call_kwargs = mock_check.call_args.kwargs
            assert call_kwargs["expected"] == "黄昏光照"
            assert call_kwargs["tolerance"] == 0.85

    async def test_vision_screenshot_error_event_skips(
        self, mock_vision_agent, world_state
    ) -> None:
        """vision_screenshot 出错时（error 非 None）→ 不触发 Vision。"""
        with patch.object(mock_vision_agent, "check", new_callable=AsyncMock) as mock_check:
            screenshot = Screenshot(data_b64=_TINY_PNG_B64, width=1, height=1)
            interceptor = VisionInterceptor(
                mock_vision_agent, world_state,
                get_pending_screenshot=lambda: screenshot,
            )
            event = ToolCallCompleted(
                name="vision_screenshot", args={},
                raw_result=None, parsed_text=None,
                error=Exception("UE 连接超时"), duration_ms=5000.0,
            )
            await interceptor.post_call(event)

            mock_check.assert_not_called()


# ---- ReadbackInterceptor Tests (Issue 016 Part A) ----


@pytest.fixture
def mock_ue_client() -> MagicMock:
    """创建一个 mock McpClientSession，call_tool 返回预设值。"""
    client = MagicMock()
    client.call_tool = AsyncMock()
    return client


def _write_event(
    tool_name: str,
    args: dict,
    result_text: str = "ok",
) -> ToolCallCompleted:
    """构建模拟的写工具调用完成事件。"""
    return ToolCallCompleted(
        name=tool_name,
        args=args,
        raw_result={"content": [{"type": "text", "text": result_text}]},
        parsed_text=result_text,
        error=None,
        duration_ms=50.0,
    )


def _readback_result_json(value: dict | list | str) -> str:
    """构建模拟的 UE readback 工具返回值（JSON-RPC result 字符串）。"""
    inner = value if isinstance(value, str) else json.dumps(value)
    outer = {
        "content": [{"type": "text", "text": inner}],
    }
    return json.dumps(outer)


class TestReadbackInterceptor:
    """L2 读回验证的基础行为。"""

    async def test_white_listed_tool_triggers_readback(
        self, mock_ue_client, world_state,
    ) -> None:
        """白名单内的写工具 → 触发读回，diff 通过 → 静默。"""
        actual_transform = {
            "translation": {"x": 100.0, "y": 200.0, "z": 0.0},
            "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
            "scale3d": {"x": 1.0, "y": 1.0, "z": 1.0},
        }
        mock_ue_client.call_tool.return_value = _readback_result_json(
            json.dumps(actual_transform)
        )

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform",
            {
                "actor": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "xform": {
                    "translation": {"x": 100.0, "y": 200.0, "z": 0.0},
                    "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "scale3d": {"x": 1.0, "y": 1.0, "z": 1.0},
                },
            },
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_called_once()
        call_name = mock_ue_client.call_tool.call_args.args[0]
        assert "get_actor_transform" in call_name
        # diff 通过时不应注入徽章
        assert event.parsed_text == "ok"

    async def test_white_listed_tool_readback_mismatch_injects_badge(
        self, mock_ue_client, world_state,
    ) -> None:
        """白名单内写工具 → 读回失配 → 注入徽章警告。"""
        actual_transform = {
            "translation": {"x": 100.0, "y": 200.0, "z": 0.0},
            "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
            "scale3d": {"x": 1.0, "y": 1.0, "z": 1.0},
        }
        mock_ue_client.call_tool.return_value = _readback_result_json(
            json.dumps(actual_transform)
        )

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform",
            {
                "actor": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "xform": {
                    "translation": {"x": 100.0, "y": 200.0, "z": 0.0},
                    "rotation": {"x": 15.0, "y": 0.0, "z": 0.0},
                    "scale3d": {"x": 1.0, "y": 1.0, "z": 1.0},
                },
            },
        )
        await interceptor.post_call(event)

        assert event.parsed_text is not None
        assert "L2 读回失配" in event.parsed_text

    async def test_non_white_listed_tool_skips(
        self, mock_ue_client, world_state,
    ) -> None:
        """白名单外的工具 → 零开销跳过。"""
        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "SceneTools.find_actors",
            {"glob": "*Light*"},
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_not_called()

    async def test_error_event_skips(
        self, mock_ue_client, world_state,
    ) -> None:
        """工具调用失败时不触发读回。"""
        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = ToolCallCompleted(
            name="toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform",
            args={"actor": {"refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"}},
            raw_result=None,
            parsed_text=None,
            error=Exception("UE 连接超时"),
            duration_ms=5000.0,
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_not_called()

    async def test_readback_call_failure_injects_badge(
        self, mock_ue_client, world_state,
    ) -> None:
        """读回调用自身失败 → 注入失败徽章，不抛异常。"""
        mock_ue_client.call_tool.side_effect = Exception("连接超时")

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform",
            {
                "actor": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "xform": {
                    "translation": {"x": 100.0, "y": 200.0, "z": 0.0},
                    "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "scale3d": {"x": 1.0, "y": 1.0, "z": 1.0},
                },
            },
        )
        # 不应抛异常
        await interceptor.post_call(event)

        assert event.parsed_text is not None
        assert "L2 读回失败" in event.parsed_text

    async def test_set_properties_triggers_readback(
        self, mock_ue_client, world_state,
    ) -> None:
        """set_properties → get_properties 读回，diff 通过。"""
        mock_ue_client.call_tool.return_value = _readback_result_json(
            json.dumps({"LightColor": "(1,0,0)", "Intensity": "8000"})
        )

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "values": '{"LightColor": "(1,0,0)", "Intensity": "8000"}',
            },
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_called_once()
        call_name = mock_ue_client.call_tool.call_args.args[0]
        assert "get_properties" in call_name
        call_args = mock_ue_client.call_tool.call_args.args[1]
        assert "properties" in call_args
        assert set(call_args["properties"]) == {"LightColor", "Intensity"}

    async def test_set_properties_mismatch_injects_badge(
        self, mock_ue_client, world_state,
    ) -> None:
        """set_properties → 属性值失配 → 徽章。"""
        mock_ue_client.call_tool.return_value = _readback_result_json(
            json.dumps({"LightColor": "(0,0,1)", "Intensity": "8000"})
        )

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "values": '{"LightColor": "(1,0,0)", "Intensity": "8000"}',
            },
        )
        await interceptor.post_call(event)

        assert event.parsed_text is not None
        assert "L2 读回失配" in event.parsed_text

    async def test_set_label_skips_not_in_white_list(
        self, mock_ue_client, world_state,
    ) -> None:
        """set_label 不在白名单中 → 跳过。"""
        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.actor.ActorTools.set_label",
            {
                "actor": {
                    "refPath": "/Game/Map.Map:PersistentLevel.SpotLight_0"
                },
                "label": "NewLabel",
            },
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_not_called()

    async def test_no_actor_name_skips(
        self, mock_ue_client, world_state,
    ) -> None:
        """无法提取 actor 名时 → 跳过。"""
        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform",
            {"xform": {"translation": {"x": 100, "y": 200, "z": 0}}},
        )
        await interceptor.post_call(event)

        mock_ue_client.call_tool.assert_not_called()

    async def test_returnvalue_wrapper_format(
        self, mock_ue_client, world_state,
    ) -> None:
        """ToolsetRegistry 的 returnValue 包装格式能被正确解包。"""
        # UE 实机中 get_properties 返回值格式:
        # {"content": [{"type": "text", "text": "{\"returnValue\":\"{\\\"intensity\\\":2}\"}"}]}
        inner_value = json.dumps({"intensity": 2, "LightColor": "(1,0,0)"})
        wrapper = json.dumps({"returnValue": inner_value})
        mock_ue_client.call_tool.return_value = json.dumps({
            "content": [{"type": "text", "text": wrapper}],
        })

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.PointLight_1"
                },
                "values": '{"intensity": "2", "LightColor": "(1,0,0)"}',
            },
        )
        await interceptor.post_call(event)

        # returnValue 正确解包 → diff 通过 → 不注入徽章
        assert event.parsed_text == "ok"

    async def test_returnvalue_wrapper_mismatch(
        self, mock_ue_client, world_state,
    ) -> None:
        """returnValue 格式 + 值失配 → 正确报告失配。"""
        inner_value = json.dumps({"intensity": 5000})  # 实际被 clamp
        wrapper = json.dumps({"returnValue": inner_value})
        mock_ue_client.call_tool.return_value = json.dumps({
            "content": [{"type": "text", "text": wrapper}],
        })

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.PointLight_1"
                },
                "values": '{"intensity": "8000"}',  # 意图 8000
            },
        )
        await interceptor.post_call(event)

        assert event.parsed_text is not None
        assert "L2 读回失配" in event.parsed_text
        assert "intensity" in event.parsed_text

    async def test_nested_dict_subset_comparison(
        self, mock_ue_client, world_state,
    ) -> None:
        """嵌套 dict: 只比对 intent 中的 key，readback 返回的额外字段不触发告警。"""
        full_settings = {
            "ColorContrast": {"x": 0.5, "y": 0.5, "z": 0.5, "w": 0.0},
            "VignetteIntensity": 0.9,
            "BloomIntensity": 0.8,  # readback 额外字段
        }
        inner_value = json.dumps({"settings": full_settings})
        wrapper = json.dumps({"returnValue": inner_value})
        mock_ue_client.call_tool.return_value = json.dumps({
            "content": [{"type": "text", "text": wrapper}],
        })

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.PostProcessVolume_0"
                },
                "values": json.dumps({
                    "settings": {
                        "ColorContrast": {"x": 0.5, "y": 0.5, "z": 0.5, "w": 0.0},
                        "VignetteIntensity": 0.9,
                    },
                }),
            },
        )
        await interceptor.post_call(event)

        assert event.parsed_text == "ok"

    async def test_nested_dict_partial_mismatch(
        self, mock_ue_client, world_state,
    ) -> None:
        """嵌套 dict: 部分值不匹配 → 正确报告。"""
        full_settings = {
            "ColorContrast": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 0.0},
            "VignetteIntensity": 0.9,
        }
        inner_value = json.dumps({"settings": full_settings})
        wrapper = json.dumps({"returnValue": inner_value})
        mock_ue_client.call_tool.return_value = json.dumps({
            "content": [{"type": "text", "text": wrapper}],
        })

        interceptor = ReadbackInterceptor(mock_ue_client, world_state)
        event = _write_event(
            "toolset_registry.toolsets.core.object.ObjectTools.set_properties",
            {
                "instance": {
                    "refPath": "/Game/Map.Map:PersistentLevel.PostProcessVolume_0"
                },
                "values": json.dumps({
                    "settings": {
                        "ColorContrast": {"x": 0.5, "y": 0.5, "z": 0.5, "w": 0.0},
                        "VignetteIntensity": 0.9,
                    },
                }),
            },
        )
        await interceptor.post_call(event)

        assert event.parsed_text is not None
        assert "L2 读回失配" in event.parsed_text
        assert "settings.ColorContrast" in event.parsed_text


# ---- Reference Image Tools (Plan 0708) ----


class TestReferenceImageMetrics:
    """compute_match_metrics 的快速冒烟（详细测试在 test_metrics.py）."""

    def test_identical_images(self):
        from harness.verification.metrics import compute_match_metrics
        from PIL import Image
        ref = Image.new("RGB", (50, 40), (100, 150, 200))
        cur = Image.new("RGB", (50, 40), (100, 150, 200))
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] == pytest.approx(1.0, abs=0.002)
        assert result["luminance"]["delta_pct"] == pytest.approx(0.0, abs=0.2)

    def test_different_images_detected(self):
        from harness.verification.metrics import compute_match_metrics
        from PIL import Image
        ref = Image.new("RGB", (50, 40), (255, 255, 255))
        cur = Image.new("RGB", (50, 40), (0, 0, 0))
        result = compute_match_metrics(ref, cur)
        assert result["luminance"]["delta_pct"] < -99
        assert result["histogram_correlation"] < 0.1


class TestVisionCompareWithReference:
    """VisionSubAgent.compare_with_reference() 双图对比."""

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        from harness.config import Config
        cfg = MagicMock(spec=Config)
        cfg.vision_api_key = "test-key"
        cfg.vision_api_base_url = "https://test.example.com"
        cfg.vision_model = "test-model"
        return cfg

    async def test_compare_returns_verdict(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = json.dumps({
                "answer": "亮度相似",
                "confidence": "high",
                "caveats": [],
                "observations": [
                    {"what": "亮度比较", "finding": "similar", "confidence": "high"}
                ],
            })
            verdict = await agent.compare_with_reference(
                _TINY_PNG_B64, _TINY_PNG_B64, "比较亮度",
            )

        assert verdict.answer == "亮度相似"
        assert verdict.confidence == "high"
        assert agent.history_length == 0

    async def test_compare_api_error_returns_fallback(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.side_effect = RuntimeError("API 不可达")
            verdict = await agent.compare_with_reference(
                _TINY_PNG_B64, _TINY_PNG_B64, "test",
            )

        assert "失败" in verdict.answer
        assert verdict.confidence == "low"
        assert len(verdict.caveats) > 0


class TestVisionClassify:
    """VisionSubAgent.classify() 纯文本 MiMo 分类."""

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        from harness.config import Config
        cfg = MagicMock(spec=Config)
        cfg.vision_api_key = "test-key"
        cfg.vision_api_base_url = "https://test.example.com"
        cfg.vision_model = "test-model"
        return cfg

    async def test_classify_returns_dict(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        expected = {
            "brightness": [
                {"actor_type": "DirectionalLight", "property": "Intensity"},
            ],
            "color_temp": [
                {"actor_type": "DirectionalLight", "property": "LightColor"},
            ],
        }

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = (
                "以下为结果：\n"
                + json.dumps(expected, ensure_ascii=False)
                + "\n 完成。"
            )
            result = await agent.classify("测试")

        assert result == expected
        assert agent.history_length == 0

    async def test_classify_no_json_raises_value_error(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            # Text without curly braces triggers "未找到 JSON" path
            mock_api.return_value = "no json here just plain text without any braces"
            with pytest.raises(ValueError, match="未找到 JSON"):
                await agent.classify("测试")

    async def test_classify_malformed_json_raises_value_error(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = '{"broken": }'
            with pytest.raises(ValueError, match="JSON 解析失败"):
                await agent.classify("测试")


class TestRenderMappingMarkdown:
    """_render_mapping_markdown() 维度分组 JSON → Markdown 表格."""

    def test_basic_rendering(self):
        from harness.verification.atmosphere import _render_mapping_markdown
        mapping = {
            "brightness": [
                {"actor_type": "DirectionalLight", "refPath": "/Game/DL.LC0",
                 "property": "Intensity"},
                {"actor_type": "SkyAtmosphere", "refPath": "/Game/Sky.SC",
                 "property": "SunIntensity"},
            ],
            "color_temp": [
                {"actor_type": "DirectionalLight", "refPath": "/Game/DL.LC0",
                 "property": "LightColor"},
            ],
        }
        md = _render_mapping_markdown(mapping)
        assert "## 亮度 (Brightness)" in md
        assert "| DirectionalLight |" in md
        assert "Intensity" in md
        assert "SunIntensity" in md
        assert "DL.LC0" in md
        assert "## 色温 (Color Temperature)" in md
        assert "LightColor" in md
        assert "共 3 个氛围相关属性" in md

    def test_empty_dimension_skipped(self):
        from harness.verification.atmosphere import _render_mapping_markdown
        mapping: dict = {
            "brightness": [],
            "contrast": [],
        }
        md = _render_mapping_markdown(mapping)
        assert "亮度" not in md
        assert "对比度" not in md

    def test_missing_dimension_key_skipped(self):
        from harness.verification.atmosphere import _render_mapping_markdown
        mapping = {
            "brightness": [
                {"actor_type": "DirectionalLight", "property": "Intensity"},
            ],
        }
        md = _render_mapping_markdown(mapping)
        assert "## 色温" not in md
        assert "共 1 个氛围相关属性" in md
