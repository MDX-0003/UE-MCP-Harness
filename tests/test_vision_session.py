"""Issue 015 — VisionSession + VisionSessionManager 单元测试"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.config import Config
from harness.verification.session import (
    VisionSession,
    VisionSessionManager,
    ScreenshotRef,
    ContextBlock,
    ContextSource,
    record_write,
    get_recent_writes,
    _format_write_description,
    _check_session_warning,
    _cap_context,
    build_scene_context,
    build_full_prompt_context,
)


# ---- VisionSession 基础 ----

class TestVisionSession:
    def test_create_session(self):
        config = Config()
        s = VisionSession(id="test1", created_at=datetime.now(timezone.utc), config=config)
        assert s.id == "test1"
        assert s.is_active is True
        assert s.screenshot_count == 0
        assert s.question_count == 0

    def test_add_screenshots(self):
        config = Config()
        s = VisionSession(id="test1", created_at=datetime.now(timezone.utc), config=config)
        s.screenshots.append(ScreenshotRef(b64="aaa", meta={"width": 1024, "height": 768}))
        s.screenshots.append(ScreenshotRef(b64="bbb", meta={"width": 800, "height": 600}))
        assert s.screenshot_count == 2
        assert s.latest_screenshot.b64 == "bbb"

    def test_touch_updates_count(self):
        config = Config()
        s = VisionSession(id="test1", created_at=datetime.now(timezone.utc), config=config)
        assert s.question_count == 0
        s.touch()
        s.touch()
        assert s.question_count == 2


# ---- Recent Writes Buffer ----

class TestRecentWrites:
    def test_record_and_get(self):
        record_write("set_actor_transform", {
            "actor": {"name": "Cube_1"},
            "xform": {"location": {"x": 0, "y": 0, "z": 100}},
        })
        recent = get_recent_writes(limit=3)
        assert any("Cube_1" in r for r in recent)
        assert any("set_actor_transform" in r for r in recent)

    def test_format_set_transform(self):
        desc = _format_write_description("set_actor_transform", {
            "actor": {"name": "Cube_1"},
            "xform": {"location": {"x": 100, "y": 200, "z": 50}},
        })
        assert "Cube_1" in desc
        assert "100" in desc
        assert "200" in desc
        assert "50" in desc

    def test_format_set_properties(self):
        desc = _format_write_description("set_properties", {
            "actor": {"name": "Light_0"},
            "json": '{"LightColor": "(1,0.5,0.3)", "Intensity": "8"}',
        })
        assert "Light_0" in desc
        assert "LightColor" in desc

    def test_format_add_to_scene(self):
        desc = _format_write_description("add_to_scene_from_class", {
            "actor_type": "PointLight",
            "label": "新光源",
        })
        assert "PointLight" in desc
        assert "新光源" in desc

    def test_format_remove_from_scene(self):
        desc = _format_write_description("remove_from_scene", {
            "actor": {"name": "Cube_5"},
        })
        assert "Cube_5" in desc
        assert "remove_from_scene" in desc

    def test_deque_limit(self):
        for i in range(15):
            record_write("set_label", {
                "actor": {"name": f"Actor_{i}"},
                "label": f"Label_{i}",
            })
        recent = get_recent_writes(limit=100)
        assert len(recent) <= 10


# ---- Session Warning ----

class TestSessionWarning:
    def _make_session(self, age_minutes: float, question_count: int) -> VisionSession:
        ago = datetime.now(timezone.utc).timestamp() - age_minutes * 60
        s = VisionSession(
            id="test", config=Config(),
            created_at=datetime.fromtimestamp(ago, tz=timezone.utc),
        )
        s.question_count = question_count
        return s

    def test_no_warning_fresh(self):
        s = self._make_session(2, 2)
        assert _check_session_warning(s) is None

    def test_l1_reminder_by_time(self):
        s = self._make_session(9, 2)
        w = _check_session_warning(s)
        assert w is not None
        assert "💡" in w

    def test_l1_reminder_by_count(self):
        s = self._make_session(2, 6)
        w = _check_session_warning(s)
        assert w is not None
        assert "💡" in w

    def test_l2_warning_by_time(self):
        s = self._make_session(16, 2)
        w = _check_session_warning(s)
        assert w is not None
        assert "⚠" in w

    def test_l2_warning_by_count(self):
        s = self._make_session(2, 9)
        w = _check_session_warning(s)
        assert w is not None
        assert "⚠" in w

    def test_l3_critical(self):
        s = self._make_session(31, 2)
        w = _check_session_warning(s)
        assert w is not None
        assert "🚨" in w

    def test_l3_critical_by_count(self):
        s = self._make_session(2, 16)
        w = _check_session_warning(s)
        assert w is not None
        assert "🚨" in w


# ---- Token Cap ----

class TestTokenCap:
    def test_all_fits_no_cap(self):
        blocks = [(1, "Hello"), (1, "World"), (2, "Extra")]
        result = _cap_context(blocks, max_tokens=100)
        assert "Hello" in result
        assert "World" in result
        assert "Extra" in result

    def test_critical_preserved(self):
        # P1 blocks preserved, P2 truncated when total exceeds cap
        critical = "A" * 80
        optional = "B" * 200
        blocks = [(1, critical), (2, optional)]
        result = _cap_context(blocks, max_tokens=50)  # 50*3=150 chars cap
        # critical (80 chars) fits, optional partially truncated → marker added
        assert critical in result
        assert len(result) < 80 + 200
        # The result should contain truncated critical + marker
        assert "..." in result

    def test_critical_exceeds_cap_truncated(self):
        # When critical alone exceeds cap, it is truncated with marker
        critical = "X" * 300
        blocks = [(1, critical)]
        result = _cap_context(blocks, max_tokens=50)  # 50*3=150 chars cap
        # Result is truncated + omission marker
        assert len(result) < 300
        assert "..." in result

    def test_empty_blocks(self):
        assert _cap_context([], max_tokens=100) == ""


# ---- VisionSessionManager ----

class TestVisionSessionManager:
    @pytest.fixture
    def mgr(self, tmp_path):
        config = Config()
        return VisionSessionManager(config, log_dir=tmp_path)

    def test_start_creates_session(self, mgr):
        session = mgr.start()
        assert session is not None
        assert mgr.get_active() is session
        assert session.id

    def test_reset_archives_and_creates_new(self, mgr):
        s1 = mgr.start()
        s1_id = s1.id
        s2 = mgr.reset()
        assert s2.id != s1_id
        assert not s1.is_active
        assert mgr.get_active() is s2

    def test_get_active_none_initially(self, mgr):
        assert mgr.get_active() is None

    def test_tell_injects_context(self, mgr):
        session = mgr.start()
        mgr.tell("测试上下文")
        manual_blocks = [
            b for b in session.context_blocks
            if b.source == ContextSource.MANUAL
        ]
        assert len(manual_blocks) == 1
        assert manual_blocks[0].content == "测试上下文"

    def test_tell_no_session_no_error(self, mgr):
        mgr.tell("无 Session")
        # Should not raise, just no-op

    def test_status_text(self, mgr):
        mgr.start()
        status = mgr.status_text()
        assert "Vision Session" in status
        assert "截图" in status
        assert "提问" in status

    def test_status_text_no_session(self, mgr):
        status = mgr.status_text()
        assert "没有活跃" in status

    def test_check_warning_none_initially(self, mgr):
        mgr.start()
        # Fresh session should have no warning
        # (created_at is recent)
        w = mgr.check_warning()
        assert w is None

    def test_reset_writes_archive_json(self, mgr):
        session = mgr.start()
        session.question_count = 5
        session.screenshots.append(ScreenshotRef(b64="test", meta={}))
        mgr.reset()

        # Check archive JSON was written
        vs_dir = mgr._log_dir / "vision_sessions"
        json_files = list(vs_dir.glob("*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["id"] == session.id
        assert data["screenshot_count"] == 1
        assert data["question_count"] == 5


# ---- Scene Context Builder ----

class TestBuildSceneContext:
    def test_empty_world_state(self):
        assert build_scene_context(None) == ""
        from harness.state.models import WorldState
        assert build_scene_context(WorldState()) == ""

    def test_with_actors(self):
        from harness.state.models import WorldState, ActorSnapshot
        state = WorldState()
        state.map_path = "/Game/Maps/Test"
        state.actors["Cube_1"] = ActorSnapshot(
            name="Cube_1", class_name="StaticMeshActor",
            transform={"location": {"x": 0, "y": 0, "z": 100}},
        )
        state.actors["Light_0"] = ActorSnapshot(
            name="Light_0", class_name="DirectionalLight",
        )

        ctx = build_scene_context(state)
        assert "Test" in ctx
        assert "2 个" in ctx
        assert "Cube_1" in ctx
        assert "StaticMeshActor" in ctx

    def test_question_mention_extraction(self):
        from harness.state.models import WorldState, ActorSnapshot
        state = WorldState()
        state.actors["Cube_3"] = ActorSnapshot(
            name="Cube_3", class_name="StaticMeshActor",
        )
        state.actors["Cube_1"] = ActorSnapshot(
            name="Cube_1", class_name="StaticMeshActor",
        )

        ctx = build_scene_context(state, question="Cube_3 是否对齐？")
        assert "问题中提到的 Actor" in ctx
        assert "Cube_3" in ctx

    def test_dirty_actors_highlighted(self):
        from harness.state.models import WorldState, ActorSnapshot
        state = WorldState()
        state.actors["Cube_1"] = ActorSnapshot(name="Cube_1")
        state.dirty_actors.add("Cube_1")

        ctx = build_scene_context(state)
        assert "最近修改的 Actor" in ctx
        assert "dirty" in ctx.lower() or "Cube_1" in ctx


# ---- Full Prompt Context ----

class TestBuildFullPromptContext:
    def test_empty_world_state(self):
        config = Config()
        session = VisionSession(id="test", created_at=datetime.now(timezone.utc), config=config)
        result = build_full_prompt_context(None, session, "测试问题")
        # No world state context, no recent writes — should return empty or writes only
        # Record a write first
        record_write("set_label", {"actor": {"name": "X"}, "label": "Y"})
        result = build_full_prompt_context(None, session, "测试问题")
        assert "最近执行的操作" in result or result == ""

    def test_includes_manual_tell(self):
        config = Config()
        session = VisionSession(id="test", created_at=datetime.now(timezone.utc), config=config)
        session.context_blocks.append(ContextBlock(
            source=ContextSource.MANUAL,
            content="LLM 意图：傍晚光照",
        ))
        result = build_full_prompt_context(None, session, "问题")
        assert "傍晚光照" in result
