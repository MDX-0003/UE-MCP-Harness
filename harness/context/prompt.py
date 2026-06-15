"""Context Assembly — 三层 Provider 实现 + Assembler (Contract 3)

Provider 列表：
  - SystemContextProvider (tier=1): Agent 身份 + WorldState 快照
  - TaskContextProvider   (tier=2): Skill 步骤 + 进度（005 实现，当前占位）
  - ToolReferenceProvider (tier=3): 可用工具名称 + 简述

Assembler 按 tier 分组、排序、渲染、拼接。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.context.provider import ContextProvider

if TYPE_CHECKING:
    from harness.state.models import WorldState


# ---- Tier 1: System Context ----

class SystemContextProvider(ContextProvider):
    """Agent 身份 + State Cache 快照。

    state 为 None → 输出占位文本（008 未就绪时使用）。
    state 有值但 actors 为空 → 输出含警告的上下文。
    """

    tier = 1
    priority = 0

    AGENT_IDENTITY = (
        "你是一个运行在 Unreal Engine 5.8 中的 UE Editor Agent。\n"
        "你可以使用工具来控制 Unreal Editor。\n"
        "尽量使用截图验证你的修改。\n"
    )

    def render(self, state: WorldState | None, active_skill: dict | None) -> str:
        return self.AGENT_IDENTITY + "\n" + _render_state_snapshot(state)


def _render_state_snapshot(state: WorldState | None) -> str:
    """将 WorldState 渲染为 Tier 1 状态文本。"""
    if state is None:
        return (
            "当前 UE 状态：缓存未初始化。\n"
            "请调 get_context 获取最新状态，或使用 find_actors 手动查询。"
        )

    actor_count = sum(1 for a in state.actors.values() if not a.deleted)
    selected_str = ", ".join(state.selected_actors) or "无"

    # 缓存新鲜度
    freshness = ""
    if state.last_full_refresh:
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - state.last_full_refresh
        if delta.total_seconds() < 60:
            freshness = f"（{int(delta.total_seconds())} 秒前刷新）"
        elif delta.total_seconds() < 3600:
            freshness = f"（{int(delta.total_seconds() / 60)} 分钟前刷新）"
        else:
            freshness = "（超过 1 小时前刷新）"

    lines = [f"当前 UE 状态：{freshness}"]
    lines.append(f"- 地图：{state.map_path or '未知'}")
    lines.append(f"- PIE：{_pie_str(state.pie_running)}")
    lines.append(f"- 选中 Actor：{selected_str}")
    lines.append(f"- 场景 Actor 数：{actor_count}")

    if state.dirty_actors:
        dirty_list = ", ".join(sorted(state.dirty_actors)[:5])
        if len(state.dirty_actors) > 5:
            dirty_list += f" ...等 {len(state.dirty_actors)} 个"
        lines.append(f"- ⚠ 以下 Actor 缓存可能过时：{dirty_list}")

    if state.dirty_toolsets:
        dirty_list = ", ".join(sorted(state.dirty_toolsets)[:5])
        if len(state.dirty_toolsets) > 5:
            dirty_list += f" ...等 {len(state.dirty_toolsets)} 个"
        lines.append(
            f"- ⚠ 以下工具集未受 State Cache 追踪，如需最新状态请手动查询：{dirty_list}"
        )

    return "\n".join(lines)


def _pie_str(pie: bool | None) -> str:
    if pie is None:
        return "未知"
    return "运行中" if pie else "已停止"


# ---- Tier 2: Task Context ----

class TaskContextProvider(ContextProvider):
    """Skill 步骤 + 进度（005 实现，当前占位）。

    active_skill 为 None 时返回空字符串——无 Skill 匹配时 Tier 2 不注入内容。
    """

    tier = 2
    priority = 0

    def render(self, state: WorldState | None, active_skill: dict | None) -> str:
        if active_skill is None:
            return ""
        # 005 未实现时的占位：简要输出 Skill 名称和步骤数
        name = active_skill.get("name", "未命名")
        steps = active_skill.get("steps", "")
        step_count = len([s for s in steps.splitlines() if s.strip()]) if steps else 0
        return (
            f"任务 Skill：{name}\n"
            f"步骤数：{step_count}\n"
            f"工具白名单：{', '.join(active_skill.get('tools_allowlist', []))}\n"
            f"\n步骤：\n{steps}"
        )


# ---- Tier 3: Tool Reference ----

class ToolReferenceProvider(ContextProvider):
    """可用工具名称 + 简述。

    LLM 可调 describe_toolset 获取完整 schema。
    """

    tier = 3
    priority = 0

    def __init__(self, tool_list: list[dict]) -> None:
        self._tools = tool_list

    def render(self, state: WorldState | None, active_skill: dict | None) -> str:
        lines = ["## 可用工具"]
        for t in self._tools:
            name = t.get("name", "")
            desc = t.get("description", "")
            # 截断长描述，节约 context
            short_desc = desc[:120] + "..." if len(desc) > 120 else desc
            lines.append(f"- **{name}**: {short_desc}")
        return "\n".join(lines)


# ---- Assembler ----

def assemble_system_prompt(
    providers: list[ContextProvider],
    state: WorldState | None,
    active_skill: dict | None,
) -> str:
    """按 tier 分组、排序、渲染、拼接。

    同一 tier 内按 priority 排序，tier 间空行分隔。
    空文本的 provider 自动跳过。
    """
    enabled = [p for p in providers if p.enabled]
    enabled.sort(key=lambda p: (p.tier, p.priority))

    sections: list[str] = []
    current_tier = 0
    for p in enabled:
        text = p.render(state, active_skill)
        if not text.strip():
            continue
        if p.tier != current_tier and sections:
            sections.append("")
            current_tier = p.tier
        sections.append(text)

    return "\n".join(sections).strip()
