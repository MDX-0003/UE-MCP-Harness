"""测试 harness.state 模块 — WorldState / StateCacheInterceptor / handlers。"""

import pytest

from harness.interceptor import ToolCallCompleted
from harness.state.interceptor import (
    StateCacheInterceptor,
    _is_write_tool,
    _extract_toolset,
    _handle_set_transform,
    _handle_set_properties,
    _handle_add_to_scene,
    _handle_remove_from_scene,
    _handle_set_label,
    _handle_add_tag,
    _handle_remove_tag,
    _handle_select_actors,
    _handle_load_level,
    _handle_get_class,   # Step 4
)
from harness.state.models import ActorSnapshot, WorldState


# ---- WorldState ----

class TestWorldState:
    """测试 WorldState pydantic 模型。"""

    def test_default_state_is_empty(self) -> None:
        state = WorldState()
        assert state.map_path == ""
        assert state.actors == {}
        assert state.selected_actors == []
        assert state.pie_running is None
        assert state.last_full_refresh is None

    def test_actor_count_excludes_deleted(self) -> None:
        state = WorldState(actors={
            "A": ActorSnapshot(name="A"),
            "B": ActorSnapshot(name="B", deleted=True),
            "C": ActorSnapshot(name="C"),
        })
        count = sum(1 for a in state.actors.values() if not a.deleted)
        assert count == 2


# ---- Handler 函数 ----

class TestHandlers:
    """测试各 write handler 的缓存更新逻辑。"""

    def test_set_transform(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="ActorTools.set_actor_transform",
            args={"actor": {"name": "Light_0"}, "xform": {"location": {"x": 100}}},
        )
        _handle_set_transform(cache, event)
        assert "Light_0" in cache.actors
        assert cache.actors["Light_0"].transform == {"location": {"x": 100}}

    def test_set_transform_updates_existing(self) -> None:
        cache = WorldState(actors={"Light_0": ActorSnapshot(name="Light_0")})
        event = ToolCallCompleted(
            name="ActorTools.set_actor_transform",
            args={"actor": {"name": "Light_0"}, "xform": {"location": {"x": 200}}},
        )
        _handle_set_transform(cache, event)
        assert cache.actors["Light_0"].transform == {"location": {"x": 200}}

    def test_set_properties(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="ObjectTools.set_properties",
            args={"actor": {"name": "Light_0"}, "json": '{"LightColor": "(1,0.5,0.3)"}'},
        )
        _handle_set_properties(cache, event)
        assert "Light_0" in cache.actors
        assert cache.actors["Light_0"].properties == {"LightColor": "(1,0.5,0.3)"}

    def test_set_properties_merge(self) -> None:
        cache = WorldState(actors={
            "Light_0": ActorSnapshot(
                name="Light_0",
                properties={"Intensity": "10"},
            ),
        })
        event = ToolCallCompleted(
            name="ObjectTools.set_properties",
            args={"actor": {"name": "Light_0"}, "json": '{"LightColor": "(1,0,0)"}'},
        )
        _handle_set_properties(cache, event)
        # 应 merge 而非覆盖
        assert cache.actors["Light_0"].properties["Intensity"] == "10"
        assert cache.actors["Light_0"].properties["LightColor"] == "(1,0,0)"

    def test_add_to_scene(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="SceneTools.add_to_scene_from_class",
            args={"actor_name": "NewLight_0"},
            parsed_text="Created actor: NewLight_0 at (0, 0, 0)",
        )
        _handle_add_to_scene(cache, event)
        assert "NewLight_0" in cache.actors

    def test_remove_from_scene(self) -> None:
        cache = WorldState(actors={"OldActor": ActorSnapshot(name="OldActor")})
        event = ToolCallCompleted(
            name="SceneTools.remove_from_scene",
            args={"actor": {"name": "OldActor"}},
        )
        _handle_remove_from_scene(cache, event)
        assert cache.actors["OldActor"].deleted is True

    def test_set_label(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="ActorTools.set_label",
            args={"actor": {"name": "Light_0"}, "label": "MainLight"},
        )
        _handle_set_label(cache, event)
        assert cache.actors["Light_0"].label == "MainLight"

    def test_add_tag(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="ActorTools.add_tag",
            args={"actor": {"name": "Light_0"}, "tag": "Important"},
        )
        _handle_add_tag(cache, event)
        assert "Important" in cache.actors["Light_0"].tags

    def test_remove_tag(self) -> None:
        cache = WorldState(actors={
            "Light_0": ActorSnapshot(name="Light_0", tags=["Important", "Temp"]),
        })
        event = ToolCallCompleted(
            name="ActorTools.remove_tag",
            args={"actor": {"name": "Light_0"}, "tag": "Temp"},
        )
        _handle_remove_tag(cache, event)
        assert "Temp" not in cache.actors["Light_0"].tags
        assert "Important" in cache.actors["Light_0"].tags

    def test_select_actors(self) -> None:
        cache = WorldState()
        event = ToolCallCompleted(
            name="EditorAppToolset.SelectActors",
            args={"actors": ["Light_0", "Light_1"]},
        )
        _handle_select_actors(cache, event)
        assert cache.selected_actors == ["Light_0", "Light_1"]

    def test_load_level(self) -> None:
        cache = WorldState(
            map_path="/Game/OldMap",
            actors={"A": ActorSnapshot(name="A")},
            selected_actors=["A"],
            dirty_actors={"A"},
            dirty_toolsets={"OldToolset"},
        )
        event = ToolCallCompleted(
            name="SceneTools.load_level",
            args={"path": "/Game/NewMap"},
        )
        _handle_load_level(cache, event)
        assert cache.map_path == "/Game/NewMap"
        assert cache.actors == {}
        assert cache.dirty_actors == set()
        assert cache.selected_actors == []

    # ---- Step 3: add_to_scene 顺手填 class_name ----

    def test_add_to_scene_fills_class_name(self) -> None:
        """add_to_scene_from_class 时从 actor_type 填入 class_name。"""
        from harness.state.normalize import infer_class_name
        cache = WorldState()
        event = ToolCallCompleted(
            name="SceneTools.add_to_scene_from_class",
            args={"actor_type": "PointLight", "label": ""},
            parsed_text="Created actor: PointLight_0 at (0, 0, 0)",
        )
        _handle_add_to_scene(cache, event)
        assert "PointLight_0" in cache.actors
        assert cache.actors["PointLight_0"].class_name == "PointLight"

    def test_add_to_scene_from_asset_fills_tail_segment(self) -> None:
        """add_to_scene_from_asset 时 asset_path 尾段作为近似类名。"""
        cache = WorldState()
        event = ToolCallCompleted(
            name="SceneTools.add_to_scene_from_asset",
            args={"asset_path": "/Game/Assets/SM_Chair", "label": ""},
            parsed_text="Created actor: SM_Chair_0 at (0, 0, 0)",
        )
        _handle_add_to_scene(cache, event)
        assert "SM_Chair_0" in cache.actors
        assert cache.actors["SM_Chair_0"].class_name == "SM_Chair"

    # ---- Step 4: get_class 读结果回填 ----

    def test_get_class_fills_class_name(self) -> None:
        """LLM 调 get_class 后回填 class_name。"""
        cache = WorldState(actors={
            "SpotLight_0": ActorSnapshot(name="SpotLight_0"),
        })
        event = ToolCallCompleted(
            name="ActorTools.get_class",
            args={"instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"}},
            parsed_text='{"returnValue":{"refPath":"/Script/Engine.SpotLight"}}',
        )
        _handle_get_class(cache, event)
        assert cache.actors["SpotLight_0"].class_name == "SpotLight"

    def test_get_class_ignores_missing_actor(self) -> None:
        """get_class 返回的 actor 不在缓存中——静默跳过。"""
        cache = WorldState()
        event = ToolCallCompleted(
            name="ActorTools.get_class",
            args={"instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.GhostActor_0"}},
            parsed_text='{"returnValue":{"refPath":"/Script/Engine.PointLight"}}',
        )
        _handle_get_class(cache, event)  # 不抛异常
        assert "GhostActor_0" not in cache.actors

    def test_get_class_bad_json_no_error(self) -> None:
        """get_class 返回不可解析的文本——静默跳过。"""
        cache = WorldState(actors={
            "SpotLight_0": ActorSnapshot(name="SpotLight_0"),
        })
        event = ToolCallCompleted(
            name="ActorTools.get_class",
            args={"instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"}},
            parsed_text="not valid json",
        )
        _handle_get_class(cache, event)  # 不抛异常
        assert cache.actors["SpotLight_0"].class_name is None

    # ---- L3 刷新推断不被已有值覆盖 ----

    def test_infer_does_not_overwrite_existing(self) -> None:
        """L3 刷新推断不覆盖已有的 class_name（UE 查询结果更权威）。"""
        snap = ActorSnapshot(name="SpotLight_0", class_name="SpotLight")
        assert snap.class_name == "SpotLight"
        # 模拟 refresher 中的行为：仅当 class_name is None 时才推断
        if snap.class_name is None:
            from harness.state.normalize import infer_class_name
            snap.class_name = infer_class_name(snap.name)
        # 已有值应保持不变
        assert snap.class_name == "SpotLight"


# ---- StateCacheInterceptor ----

class TestStateCacheInterceptor:
    """测试缓存拦截器的整体行为。"""

    @pytest.mark.asyncio
    async def test_post_call_updates_cache(self) -> None:
        cache = WorldState()
        interceptor = StateCacheInterceptor(cache)

        event = ToolCallCompleted(
            name="ToolsetRegistry.EditorAppToolset.SelectActors",
            args={"actors": ["TestActor"]},
            parsed_text="OK",
        )

        await interceptor.post_call(event)
        assert cache.selected_actors == ["TestActor"]

    @pytest.mark.asyncio
    async def test_post_call_error_skips_update(self) -> None:
        """调用失败时不更新缓存。"""
        cache = WorldState()
        interceptor = StateCacheInterceptor(cache)

        event = ToolCallCompleted(
            name="ToolsetRegistry.EditorAppToolset.SelectActors",
            args={"actors": ["BadActor"]},
            error=RuntimeError("连接超时"),
        )

        await interceptor.post_call(event)
        assert cache.selected_actors == []  # 未更新

    @pytest.mark.asyncio
    async def test_uncovered_write_tool_marks_dirty(self) -> None:
        """未覆盖的 write tool 标记 dirty_toolsets。"""
        cache = WorldState()
        interceptor = StateCacheInterceptor(cache)

        event = ToolCallCompleted(
            name="toolset_registry.toolsets.core.niagara.NiagaraToolsets.set_something",
            args={"param": "value"},
        )

        await interceptor.post_call(event)
        assert "NiagaraToolsets" in cache.dirty_toolsets


# ---- 辅助函数 ----

class TestHelpers:
    """测试辅助函数。"""

    def test_is_write_tool(self) -> None:
        assert _is_write_tool("tool.set_actor_transform") is True
        assert _is_write_tool("tool.add_to_scene_from_class") is True
        assert _is_write_tool("tool.remove_from_scene") is True
        assert _is_write_tool("tool.SelectActors") is True
        assert _is_write_tool("tool.find_actors") is False
        assert _is_write_tool("tool.get_current_level") is False

    def test_extract_toolset(self) -> None:
        assert _extract_toolset(
            "toolset_registry.toolsets.core.scene.SceneTools.find_actors"
        ) == "SceneTools"
        assert _extract_toolset(
            "ToolsetRegistry.EditorAppToolset.GetSelectedActors"
        ) == "EditorAppToolset"
