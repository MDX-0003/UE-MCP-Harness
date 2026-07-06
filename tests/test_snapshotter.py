"""测试 SnapshotRecorder — 会话级状态快照写入 (Issue 007 扩展)"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from harness.interceptor import ToolCallCompleted
from harness.observability.snapshotter import SnapshotRecorder
from harness.state.models import WorldState

# 1×1 透明 PNG 的 base64 — 可通过 parse_screenshot PIL 解码
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ---- Fixtures ----

@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    return tmp_path / "test-session"


@pytest.fixture
def world_state() -> WorldState:
    return WorldState(map_path="/Temp/Untitled_1")

# ---- Helper: build ToolCallCompleted events ----

def _screenshot_event(name: str = "CaptureEditorImage", b64: str = _TINY_PNG_B64) -> ToolCallCompleted:
    """模拟截图工具调用完成事件（1×1 透明 PNG）。"""
    return ToolCallCompleted(
        name=name,
        args={},
        raw_result={"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]},
        parsed_text="[image: image/png]",
        error=None,
        duration_ms=150.0,
    )


def _context_event(text: str = "test context content") -> ToolCallCompleted:
    return ToolCallCompleted(
        name="get_context",
        args={},
        raw_result={"content": [{"type": "text", "text": text}]},
        parsed_text=text,
        error=None,
        duration_ms=10.0,
    )


def _error_event() -> ToolCallCompleted:
    return ToolCallCompleted(
        name="CaptureEditorImage",
        args={},
        raw_result=None,
        parsed_text=None,
        error=Exception("fail"),
        duration_ms=100.0,
    )


# ---- Tests ----

class TestSnapshotRecorderBasic:
    """基础行为：触发 vs 不触发，目录结构。"""

    async def test_screenshot_saves_png(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        event = _screenshot_event()
        await recorder.post_call(event)

        screenshots = list((snapshot_dir / "screenshots").glob("*.png"))
        assert len(screenshots) == 1
        assert screenshots[0].name.endswith("_CaptureEditorImage.png")

    async def test_error_event_skips_save(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        await recorder.post_call(_error_event())

        screenshots_dir = snapshot_dir / "screenshots"
        assert not screenshots_dir.exists() or not list(screenshots_dir.glob("*.png"))

    async def test_non_screenshot_tool_skips_save(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        event = ToolCallCompleted(
            name="SceneTools.find_actors",
            args={"glob": "*Light*"},
            raw_result={"content": [{"type": "text", "text": "Found"}]},
            parsed_text="Found",
            error=None,
            duration_ms=50.0,
        )
        await recorder.post_call(event)

        screenshots_dir = snapshot_dir / "screenshots"
        assert not screenshots_dir.exists() or not list(screenshots_dir.glob("*.png"))

    async def test_context_saves_text_and_state(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        await recorder.post_call(_context_event("test context text"))

        contexts_dir = snapshot_dir / "contexts"
        txt_files = list(contexts_dir.glob("*_context.txt"))
        json_files = list(contexts_dir.glob("*_state.json"))
        assert len(txt_files) == 1
        assert len(json_files) == 1
        assert "test context text" in txt_files[0].read_text()

    async def test_no_screenshot_data_no_save(self, snapshot_dir, world_state) -> None:
        """截图工具返回无图片数据时不写文件。"""
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        event = ToolCallCompleted(
            name="CaptureEditorImage",
            args={},
            raw_result={"content": [{"type": "text", "text": "no image"}]},
            parsed_text="no image",
            error=None,
            duration_ms=150.0,
        )
        await recorder.post_call(event)

        screenshots_dir = snapshot_dir / "screenshots"
        assert not screenshots_dir.exists() or not list(screenshots_dir.glob("*.png"))


class TestSnapshotRecorderVerdict:
    """Vision verdict 写入。"""

    async def test_verdict_saved_when_present(self, snapshot_dir, world_state) -> None:
        world_state.last_vision_verdict = {
            "pass": True, "reason": "looks good", "adjustment": "none",
            "at": "2026-06-17T00:00:00Z"
        }
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        await recorder.post_call(_screenshot_event())

        verdicts = list((snapshot_dir / "screenshots").glob("*.verdict.json"))
        assert len(verdicts) == 1
        data = json.loads(verdicts[0].read_text())
        assert data["pass"] is True
        assert data["reason"] == "looks good"

    async def test_no_verdict_when_none(self, snapshot_dir, world_state) -> None:
        """WorldState 无 last_vision_verdict 时不写 verdict 文件。"""
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        await recorder.post_call(_screenshot_event())

        verdicts = list((snapshot_dir / "screenshots").glob("*.verdict.json"))
        assert len(verdicts) == 0


class TestSnapshotRecorderSkill:
    """Skill 激活/停用快照。"""

    def test_skill_activated_saves_yaml(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        recorder.on_skill_activated("evening-lighting", "name: evening-lighting\ndescription: test\n")

        yamls = list((snapshot_dir / "skills").glob("*.yaml"))
        assert len(yamls) == 1
        assert "evening-lighting" in yamls[0].name
        assert "name: evening-lighting" in yamls[0].read_text()

    def test_skill_deactivated_writes_marker(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        recorder.on_skill_deactivated()

        markers = list((snapshot_dir / "skills").glob("*_deactivate.txt"))
        assert len(markers) == 1


class TestSnapshotRecorderSession:
    """session.json 元数据。"""

    def test_session_json_written(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        recorder._vision_call_count = 3
        recorder._tool_call_count = 42
        recorder._skills_activated = ["evening-lighting"]

        recorder.write_session_json()

        session_file = snapshot_dir / "session.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text())
        assert data["tool_call_count"] == 42
        assert data["vision_call_count"] == 3
        assert data["skills_activated"] == ["evening-lighting"]
        assert data["map_path"] == "/Temp/Untitled_1"
        assert "started" in data
        assert "ended" in data
        assert "harness_version" in data

    async def test_tool_call_count_increments(self, snapshot_dir, world_state) -> None:
        recorder = SnapshotRecorder(snapshot_dir, world_state)
        assert recorder._tool_call_count == 0
        await recorder.post_call(_context_event("a"))
        assert recorder._tool_call_count == 1
        await recorder.post_call(_screenshot_event("CaptureEditorImage"))
        assert recorder._tool_call_count == 2
        # error event still counts
        await recorder.post_call(_error_event())
        assert recorder._tool_call_count == 3

    async def test_failure_does_not_block(self, snapshot_dir, world_state) -> None:
        """文件 I/O 失败不抛异常。"""
        recorder = SnapshotRecorder(snapshot_dir / "nonexistent" / "deep", world_state)
        # Should not raise — directory auto-created
        await recorder.post_call(_screenshot_event())
        assert (snapshot_dir / "nonexistent" / "deep" / "screenshots").exists()


class TestSnapshotRecorderVisionScreenshot:
    """Harness vision_screenshot 工具 — 通过 get_pending_screenshot 回调获取 Screenshot。"""

    async def test_vision_screenshot_saves_png(self, snapshot_dir, world_state) -> None:
        """vision_screenshot → SnapshotRecorder 通过回调获取 Screenshot → 写 PNG 到磁盘。"""
        from harness.verification.capturer import Screenshot
        recorder = SnapshotRecorder(
            snapshot_dir, world_state,
            get_pending_screenshot=lambda: Screenshot(data_b64=_TINY_PNG_B64, width=1, height=1),
        )
        event = ToolCallCompleted(
            name="vision_screenshot",
            args={},
            raw_result={"content": [{"type": "text", "text": "Screenshot 已获取: 1x1 image/png"}]},
            parsed_text="Screenshot 已获取: 1x1 image/png",
            error=None,
            duration_ms=200.0,
        )
        await recorder.post_call(event)

        screenshots = list((snapshot_dir / "screenshots").glob("*.png"))
        assert len(screenshots) == 1
        assert screenshots[0].name.endswith("_vision_screenshot.png")

    async def test_vision_screenshot_null_callback_skips(self, snapshot_dir, world_state) -> None:
        """回调返回 None → 不写文件。"""
        recorder = SnapshotRecorder(
            snapshot_dir, world_state,
            get_pending_screenshot=lambda: None,
        )
        event = ToolCallCompleted(
            name="vision_screenshot", args={},
            raw_result={}, parsed_text="...", error=None, duration_ms=200.0,
        )
        await recorder.post_call(event)

        screenshots_dir = snapshot_dir / "screenshots"
        assert not screenshots_dir.exists() or not list(screenshots_dir.glob("*.png"))

    async def test_vision_screenshot_with_verdict(self, snapshot_dir, world_state) -> None:
        """vision_screenshot + WorldState 中有 verdict → 同时写 PNG 和 verdict JSON。"""
        from harness.verification.capturer import Screenshot
        world_state.last_vision_verdict = {
            "pass": True, "reason": "光照正确", "adjustment": "无需调整",
            "at": "2026-06-21T00:00:00Z"
        }
        recorder = SnapshotRecorder(
            snapshot_dir, world_state,
            get_pending_screenshot=lambda: Screenshot(data_b64=_TINY_PNG_B64, width=1, height=1),
        )
        event = ToolCallCompleted(
            name="vision_screenshot", args={},
            raw_result={}, parsed_text="...", error=None, duration_ms=200.0,
        )
        await recorder.post_call(event)

        verdicts = list((snapshot_dir / "screenshots").glob("*.verdict.json"))
        assert len(verdicts) == 1
        import json as _json
        data = _json.loads(verdicts[0].read_text(encoding="utf-8"))
        assert data["pass"] is True
        assert data["reason"] == "光照正确"
