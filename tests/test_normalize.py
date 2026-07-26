"""测试 harness.state.normalize — P0-1 共享参数归一化。"""
import pytest

from harness.state.normalize import (
    NormalizedCall,
    normalize_tool_args,
    state_parse_ref_path,
    _extract_payload,
    mcp_tool_short_name,
)


class TestParseRefPath:
    """测试 state_parse_ref_path 从 UE refPath 中提取 actor/component 名。"""

    def test_actor_only(self) -> None:
        assert state_parse_ref_path(
            "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"
        ) == ("SpotLight_0", "")

    def test_actor_with_component(self) -> None:
        assert state_parse_ref_path(
            "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0.LightComponent0"
        ) == ("SpotLight_0", "LightComponent0")

    def test_static_mesh_actor(self) -> None:
        assert state_parse_ref_path(
            "/Game/NewWorld.NewWorld:PersistentLevel.StaticMeshActor_7"
        ) == ("StaticMeshActor_7", "")

    def test_class_ref(self) -> None:
        assert state_parse_ref_path(
            "/Script/Engine.SpotLight"
        ) == ("SpotLight", "")

    def test_empty_path(self) -> None:
        assert state_parse_ref_path("") == ("", "")

    def test_deep_nested(self) -> None:
        assert state_parse_ref_path(
            "/Game/Maps/Level.Level:PersistentLevel.MyActor.SubComp.LightComponent0"
        ) == ("SubComp", "LightComponent0")


class TestExtractPayload:
    """测试 _extract_payload 按工具类型提取操作内容。"""

    def test_set_actor_transform(self) -> None:
        args = {"xform": {"location": {"x": 100, "y": 200, "z": 50}}}
        payload = _extract_payload("set_actor_transform", args)
        assert payload == {"xform": {"location": {"x": 100, "y": 200, "z": 50}}}

    def test_set_properties_values_str(self) -> None:
        """真实 UE 调用：values 是 JSON string。"""
        args = {"values": '{"intensity": 8000, "lightColor": {"r": 1.0}}'}
        payload = _extract_payload("set_properties", args)
        assert payload == {"intensity": 8000, "lightColor": {"r": 1.0}}

    def test_set_properties_json_compat(self) -> None:
        """向下兼容：老测试用的 json 键。"""
        args = {"json": '{"LightColor": "(1,0,0)"}'}
        payload = _extract_payload("set_properties", args)
        assert payload == {"LightColor": "(1,0,0)"}

    def test_set_properties_values_dict(self) -> None:
        """values 已经是 dict 的情况。"""
        args = {"values": {"intensity": 8000}}
        payload = _extract_payload("set_properties", args)
        assert payload == {"intensity": 8000}

    def test_set_label(self) -> None:
        payload = _extract_payload("set_label", {"label": "MainLight"})
        assert payload == {"label": "MainLight"}

    def test_add_tag(self) -> None:
        payload = _extract_payload("add_tag", {"tag": "Important"})
        assert payload == {"tag": "Important"}

    def test_load_level(self) -> None:
        payload = _extract_payload("load_level", {"path": "/Game/NewMap"})
        assert payload == {"path": "/Game/NewMap"}

    def test_select_actors(self) -> None:
        payload = _extract_payload("SelectActors", {"actors": ["A", "B"]})
        assert payload == {"actors": ["A", "B"]}


class TestNormalizeToolArgs:
    """测试 normalize_tool_args 主入口——处理真实 UE 参数形状。"""

    # ---- 真实 refPath 格式（P0-1 修复目标） ----

    def test_set_actor_transform_refpath(self) -> None:
        """真实调用：actor 用 refPath 标识。"""
        nc = normalize_tool_args("set_actor_transform", {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"},
            "xform": {"location": {"x": 100, "y": 200, "z": 50}},
        })
        assert nc.actor_name == "SpotLight_0"
        assert nc.payload == {"xform": {"location": {"x": 100, "y": 200, "z": 50}}}

    def test_set_properties_refpath(self) -> None:
        """真实调用: instance + values (JSON string)。"""
        nc = normalize_tool_args("set_properties", {
            "instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0.LightComponent0"},
            "values": '{"intensity": 8000, "lightColor": {"r": 1.0, "g": 0.1, "b": 0.05}}',
        })
        assert nc.actor_name == "SpotLight_0"
        assert nc.component_name == "LightComponent0"
        assert nc.payload == {"intensity": 8000, "lightColor": {"r": 1.0, "g": 0.1, "b": 0.05}}

    def test_set_label_refpath(self) -> None:
        """真实调用：actor 用 refPath。"""
        nc = normalize_tool_args("set_label", {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.StaticMeshActor_7"},
            "label": "Cube_Pillar",
        })
        assert nc.actor_name == "StaticMeshActor_7"
        assert nc.payload == {"label": "Cube_Pillar"}

    def test_get_label_refpath(self) -> None:
        """读取工具也使用相同的 refPath 格式。"""
        nc = normalize_tool_args("get_label", {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.StaticMeshActor_7"},
        })
        assert nc.actor_name == "StaticMeshActor_7"

    def test_remove_from_scene_refpath(self) -> None:
        nc = normalize_tool_args("remove_from_scene", {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.OldActor"},
        })
        assert nc.actor_name == "OldActor"

    # ---- 向下兼容：旧 name 格式 ----

    def test_set_transform_name_compat(self) -> None:
        """向下兼容旧测试格式: actor = {name: ...}。"""
        nc = normalize_tool_args("set_actor_transform", {
            "actor": {"name": "Light_0"},
            "xform": {"location": {"x": 100}},
        })
        assert nc.actor_name == "Light_0"

    def test_set_properties_json_compat(self) -> None:
        """向下兼容旧测试格式: actor = {name: ...} + json 键。"""
        nc = normalize_tool_args("set_properties", {
            "actor": {"name": "Light_0"},
            "json": '{"LightColor": "(1,0.5,0.3)"}',
        })
        assert nc.actor_name == "Light_0"
        assert nc.payload == {"LightColor": "(1,0.5,0.3)"}

    def test_string_actor_compat(self) -> None:
        """向下兼容: actor 直接是字符串。"""
        nc = normalize_tool_args("set_label", {
            "actor": "Light_0",
            "label": "Test",
        })
        assert nc.actor_name == "Light_0"

    # ---- 无 actor 目标的工具 ----

    def test_load_level_no_actor(self) -> None:
        nc = normalize_tool_args("load_level", {
            "path": "/Game/NewMap",
        })
        assert nc.actor_name == ""
        assert nc.payload == {"path": "/Game/NewMap"}

    def test_select_actors_no_single_actor(self) -> None:
        nc = normalize_tool_args("SelectActors", {
            "actors": ["A", "B", "C"],
        })
        assert nc.payload == {"actors": ["A", "B", "C"]}

    # ---- refPath 包含组件路径 ----

    def test_component_refpath_extracts_owner_actor(self) -> None:
        """组件路径应归属到 owner actor。"""
        nc = normalize_tool_args("set_properties", {
            "instance": {"refPath": "/Game/Map.Map:PersistentLevel.MyLight.LightComponent0"},
            "values": '{"intensity": 5000}',
        })
        assert nc.actor_name == "MyLight"
        assert nc.component_name == "LightComponent0"

    # ---- raw_args 保留 ----

    def test_raw_args_preserved(self) -> None:
        args = {
            "actor": {"refPath": "/Game/.../SpotLight_0"},
            "xform": {"location": {"x": 100}},
            "worldspace": True,
        }
        nc = normalize_tool_args("set_actor_transform", args)
        assert nc.raw_args is args  # 同一引用


class TestExtractShortName:
    """测试 mcp_tool_short_name。"""

    def test_python_toolset(self) -> None:
        assert mcp_tool_short_name(
            "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform"
        ) == "set_actor_transform"

    def test_cpp_toolset(self) -> None:
        assert mcp_tool_short_name(
            "ToolsetRegistry.EditorAppToolset.GetSelectedActors"
        ) == "GetSelectedActors"

    def test_already_short(self) -> None:
        assert mcp_tool_short_name("vision_screenshot") == "vision_screenshot"


class TestInferClassName:
    """测试 infer_class_name — 从 UE 默认命名规则推断 class name。"""

    def test_standard_pattern(self) -> None:
        from harness.state.normalize import infer_class_name
        assert infer_class_name("SpotLight_0") == "SpotLight"
        assert infer_class_name("SpotLight_1") == "SpotLight"
        assert infer_class_name("CineCameraActor_3") == "CineCameraActor"
        assert infer_class_name("StaticMeshActor_7") == "StaticMeshActor"
        assert infer_class_name("PointLight_42") == "PointLight"

    def test_no_suffix_returns_none(self) -> None:
        from harness.state.normalize import infer_class_name
        assert infer_class_name("KeyLight") is None
        assert infer_class_name("MainKey") is None
        assert infer_class_name("RedFillLight") is None

    def test_lowercase_prefix_returns_none(self) -> None:
        from harness.state.normalize import infer_class_name
        assert infer_class_name("lowercase_0") is None

    def test_custom_name_with_number_suffix(self) -> None:
        from harness.state.normalize import infer_class_name
        # 用户自定义名 MyActor_0 → 推断为 MyActor，虽非引擎类名但比 Unknown 有用
        assert infer_class_name("MyActor_0") == "MyActor"

    def test_empty_string(self) -> None:
        from harness.state.normalize import infer_class_name
        assert infer_class_name("") is None

    def test_only_underscore_number(self) -> None:
        from harness.state.normalize import infer_class_name
        assert infer_class_name("_0") is None


class TestIntegration:
    """模拟完整链路的集成测试：LLM 调用 → normalize → handler 写入缓存。"""

    def test_full_write_chain_set_transform(self) -> None:
        """模拟 agent 调 set_actor_transform → normalize → 缓存更新。"""
        from harness.state.models import WorldState, ActorSnapshot

        cache = WorldState()
        args = {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"},
            "xform": {"location": {"x": 100, "y": 200, "z": 50}},
        }

        nc = normalize_tool_args("set_actor_transform", args)
        assert nc.actor_name == "SpotLight_0"

        # 模拟 handler 行为
        name = nc.actor_name
        if name:
            if name not in cache.actors:
                cache.actors[name] = ActorSnapshot(name=name)
            cache.actors[name].transform = nc.payload.get("xform", {})
            cache.dirty_actors.add(name)

        assert "SpotLight_0" in cache.actors
        assert "SpotLight_0" in cache.dirty_actors
        assert cache.actors["SpotLight_0"].transform == {
            "location": {"x": 100, "y": 200, "z": 50},
        }

    def test_full_write_chain_set_properties(self) -> None:
        """模拟 agent 调 set_properties → normalize → 缓存更新。"""
        from harness.state.models import WorldState, ActorSnapshot

        cache = WorldState()
        args = {
            "instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0.LightComponent0"},
            "values": '{"intensity": 8000, "lightColor": {"r": 1.0, "g": 0.1, "b": 0.05}}',
        }

        nc = normalize_tool_args("set_properties", args)
        assert nc.actor_name == "SpotLight_0"

        # 模拟 handler 行为
        name = nc.actor_name
        if name:
            if name not in cache.actors:
                cache.actors[name] = ActorSnapshot(name=name)
            cache.dirty_actors.add(name)
            if nc.payload:
                cache.actors[name].properties.update(nc.payload)

        assert "SpotLight_0" in cache.actors
        assert "SpotLight_0" in cache.dirty_actors
        assert cache.actors["SpotLight_0"].properties == {
            "intensity": 8000,
            "lightColor": {"r": 1.0, "g": 0.1, "b": 0.05},
        }

    def test_dirty_actors_not_empty_with_refpath(self) -> None:
        """关键断言：refPath 格式的写入应正确填充 dirty_actors。"""
        from harness.state.models import WorldState, ActorSnapshot

        cache = WorldState()

        # 1. set_actor_transform（使用真实 refPath 格式）
        nc1 = normalize_tool_args("set_actor_transform", {
            "actor": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"},
            "xform": {"location": {"x": 100}},
        })
        if nc1.actor_name:
            cache.actors[nc1.actor_name] = ActorSnapshot(name=nc1.actor_name)
            cache.dirty_actors.add(nc1.actor_name)

        # 2. set_properties
        nc2 = normalize_tool_args("set_properties", {
            "instance": {"refPath": "/Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0"},
            "values": '{"lightColor": "(1,0,0)"}',
        })
        if nc2.actor_name:
            if nc2.actor_name not in cache.actors:
                cache.actors[nc2.actor_name] = ActorSnapshot(name=nc2.actor_name)
            cache.dirty_actors.add(nc2.actor_name)

        assert len(cache.dirty_actors) > 0, "dirty_actors 不应为空！"
        assert "SpotLight_0" in cache.dirty_actors
