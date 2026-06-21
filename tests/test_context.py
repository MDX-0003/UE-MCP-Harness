"""测试 harness.context 模块 — filter + providers + assembler。"""

import pytest

from harness.context.filter import apply_filter, is_escape_hatch, ESCAPE_HATCH_TOOLS
from harness.context.prompt import (
    SystemContextProvider,
    TaskContextProvider,
    ToolReferenceProvider,
    assemble_system_prompt,
)
from harness.context.provider import ContextProvider
from harness.state.models import ActorSnapshot, WorldState


# ---- filter ----

class TestApplyFilter:
    """测试工具列表过滤。"""

    RAW_TOOLS = [
        {"name": "list_toolsets", "description": "列出所有工具集"},
        {"name": "describe_toolset", "description": "描述指定工具集"},
        {"name": "ToolsetRegistry.EditorAppToolset.GetSelectedActors", "description": "获取选中"},
        {"name": "ToolsetRegistry.EditorAppToolset.SelectActors", "description": "选择"},
        {"name": "toolset_registry.toolsets.core.scene.SceneTools.find_actors", "description": "查找"},
        {"name": "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform", "description": "变换"},
        {"name": "toolset_registry.toolsets.core.niagara.NiagaraToolsets.spawn", "description": "生成"},
        {"name": "ToolsetRegistry.LogsToolset.GetLogEntries", "description": "获取日志"},
    ]

    ALLOWLIST = (
        "EditorAppToolset.",
        "scene.SceneTools.",
        "actor.ActorTools.",
        "object.ObjectTools.",
    )

    def test_escape_hatch_always_visible(self) -> None:
        """逃生通道工具始终可见。"""
        result = apply_filter(self.RAW_TOOLS, self.ALLOWLIST)
        names = [t["name"] for t in result]
        assert "list_toolsets" in names
        assert "describe_toolset" in names

    def test_allowlist_match(self) -> None:
        """allowlist 匹配的工具被保留。"""
        result = apply_filter(self.RAW_TOOLS, self.ALLOWLIST)
        names = [t["name"] for t in result]
        assert "ToolsetRegistry.EditorAppToolset.GetSelectedActors" in names
        assert "ToolsetRegistry.EditorAppToolset.SelectActors" in names
        assert "toolset_registry.toolsets.core.scene.SceneTools.find_actors" in names
        assert "toolset_registry.toolsets.core.actor.ActorTools.set_actor_transform" in names

    def test_non_matching_filtered_out(self) -> None:
        """不匹配 allowlist 的工具被过滤。"""
        result = apply_filter(self.RAW_TOOLS, self.ALLOWLIST)
        names = [t["name"] for t in result]
        assert "ToolsetRegistry.LogsToolset.GetLogEntries" not in names
        assert "toolset_registry.toolsets.core.niagara.NiagaraToolsets.spawn" not in names

    def test_empty_allowlist(self) -> None:
        """空 allowlist 只保留逃生通道。"""
        result = apply_filter(self.RAW_TOOLS, ())
        names = [t["name"] for t in result]
        assert set(names) == {"list_toolsets", "describe_toolset"}

    def test_extra_allowed(self) -> None:
        """extra_allowed 可以放行额外工具。"""
        result = apply_filter(
            self.RAW_TOOLS, self.ALLOWLIST,
            extra_allowed=frozenset({"GetLogEntries"}),
        )
        names = [t["name"] for t in result]
        assert "ToolsetRegistry.LogsToolset.GetLogEntries" in names

    def test_is_escape_hatch(self) -> None:
        """判断工具名是否为逃生通道。"""
        assert is_escape_hatch("list_toolsets") is True
        assert is_escape_hatch("some.package.list_toolsets") is True
        assert is_escape_hatch("describe_toolset") is True
        assert is_escape_hatch("find_actors") is False


# ---- SystemContextProvider ----

class TestSystemContextProvider:
    """测试 Tier 1 — Agent 身份 + 状态快照。"""

    def test_null_state_outputs_placeholder(self) -> None:
        provider = SystemContextProvider()
        text = provider.render(None, None)
        assert "缓存未初始化" in text
        assert "Unreal Engine" in text  # Agent identity still present

    def test_empty_state(self) -> None:
        provider = SystemContextProvider()
        state = WorldState()
        text = provider.render(state, None)
        assert "地图：未知" in text
        assert "场景 Actor 数：0" in text
        assert "选中 Actor：无" in text

    def test_populated_state(self) -> None:
        provider = SystemContextProvider()
        state = WorldState(
            map_path="/Game/Maps/Main",
            actors={
                "Light_0": ActorSnapshot(name="Light_0", class_name="DirectionalLight"),
                "Light_1": ActorSnapshot(name="Light_1", class_name="PointLight"),
                "DeletedActor": ActorSnapshot(name="DeletedActor", deleted=True),
            },
            selected_actors=["Light_0"],
            pie_running=False,
        )
        text = provider.render(state, None)
        assert "/Game/Maps/Main" in text
        assert "场景 Actor 数：2" in text  # DeletedActor 不计数
        assert "选中 Actor：Light_0" in text
        assert "PIE：已停止" in text

    def test_dirty_warnings(self) -> None:
        provider = SystemContextProvider()
        state = WorldState(
            dirty_actors={"Light_0", "Light_1"},
            dirty_toolsets={"NiagaraToolsets", "BlueprintTools"},
        )
        text = provider.render(state, None)
        assert "缓存可能过时" in text
        assert "未受 State Cache 追踪" in text

    def test_pie_unknown(self) -> None:
        provider = SystemContextProvider()
        state = WorldState(pie_running=None)
        text = provider.render(state, None)
        assert "PIE：未知" in text


# ---- TaskContextProvider ----

class TestTaskContextProvider:
    """测试 Tier 2 — Skill 上下文。"""

    def test_null_skill_returns_empty(self) -> None:
        provider = TaskContextProvider()
        text = provider.render(None, None)
        assert text == ""

    def test_skill_with_steps(self) -> None:
        provider = TaskContextProvider()
        skill = {
            "name": "evening-lighting",
            "tools_allowlist": ["find_actors", "set_actor_transform"],
            "steps": "1. 找到 DirectionalLight\n2. 调整角度\n3. 截图",
        }
        text = provider.render(None, skill)
        assert "evening-lighting" in text
        assert "find_actors" in text
        assert "1. 找到" in text


# ---- ToolReferenceProvider ----

class TestToolReferenceProvider:
    """测试 Tier 3 — 工具参考。"""

    def test_render(self) -> None:
        tools = [
            {"name": "SceneTools.find_actors", "description": "查找 Actor"},
            {"name": "ActorTools.set_actor_transform", "description": "设置变换"},
        ]
        provider = ToolReferenceProvider(tools)
        text = provider.render(None, None)
        assert "可用工具" in text
        assert "SceneTools.find_actors" in text
        assert "ActorTools.set_actor_transform" in text

    def test_long_description_truncated(self) -> None:
        tools = [
            {"name": "test.tool", "description": "A" * 200},
        ]
        provider = ToolReferenceProvider(tools)
        text = provider.render(None, None)
        # 描述应被截断（120 字符 + "..."）
        assert "..." in text


# ---- Assembler ----

class TestAssembler:
    """测试 ContextProvider 组装。"""

    def test_empty_providers(self) -> None:
        text = assemble_system_prompt([], None, None)
        assert text == ""

    def test_single_provider(self) -> None:
        class TestProvider(ContextProvider):
            tier = 1
            def render(self, state, skill):
                return "test output"

        text = assemble_system_prompt([TestProvider()], None, None)
        assert text == "test output"

    def test_tier_ordering(self) -> None:
        calls: list[str] = []

        class Tier3(ContextProvider):
            tier = 3
            def render(self, state, skill):
                calls.append("tier3")
                return "tier3 text"

        class Tier1(ContextProvider):
            tier = 1
            def render(self, state, skill):
                calls.append("tier1")
                return "tier1 text"

        text = assemble_system_prompt([Tier3(), Tier1()], None, None)
        assert calls == ["tier1", "tier3"]
        assert "tier1 text" in text
        assert "tier3 text" in text

    def test_same_tier_priority(self) -> None:
        calls: list[str] = []

        class PriorityLow(ContextProvider):
            tier = 1; priority = 10
            def render(self, state, skill):
                calls.append("low")
                return "low"

        class PriorityHigh(ContextProvider):
            tier = 1; priority = 0
            def render(self, state, skill):
                calls.append("high")
                return "high"

        text = assemble_system_prompt([PriorityLow(), PriorityHigh()], None, None)
        assert calls == ["high", "low"]

    def test_disabled_provider_skipped(self) -> None:
        class DisabledProvider(ContextProvider):
            tier = 1; enabled = False
            def render(self, state, skill):
                return "should not appear"

        text = assemble_system_prompt([DisabledProvider()], None, None)
        assert text == ""

    def test_full_pipeline(self) -> None:
        """完整的 Tier 1 + Tier 2 + Tier 3 管道。"""
        state = WorldState(map_path="/Game/Maps/Test", actors={
            "A_0": ActorSnapshot(name="A_0"),
        })

        providers: list[ContextProvider] = [
            SystemContextProvider(),
            TaskContextProvider(),
            ToolReferenceProvider([
                {"name": "t1", "description": "desc1"},
            ]),
        ]

        text = assemble_system_prompt(providers, state, None)
        assert "Unreal Engine" in text
        assert "/Game/Maps/Test" in text
        assert "可用工具" in text
        # Tier 2 应跳过（无 active_skill）
        assert "任务 Skill" not in text
