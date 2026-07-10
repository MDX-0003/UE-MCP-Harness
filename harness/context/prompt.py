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
        "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n"
        "\n"
        "## 工作模式\n"
        "当前有两种主要工作模式，根据用户意图选择：\n"
        "\n"
        "  氛围优先 → activate_skill(\"match-atmosphere\")\n"
        "    参考图驱动的整体场景氛围调整：光照、天空、雾、云、后处理\n"
        "\n"
        "  局部调整 → activate_skill(\"scene-lighting\")\n"
        "    单灯/少灯的精确属性调整：位置、颜色、强度、旋转\n"
        "\n"
        "  自由探索 → deactivate_skill（或保持不激活任何 Skill）\n"
        "    所有工具可用，适合查询、浏览、非标准操作\n"
        "\n"
        "## 通用验证 SOP\n"
        "任何场景修改后：\n"
        "  1. 修改后的写入值由 Harness 自动读回验证（⚠ 徽章提示失配）\n"
        "  2. 相机定位——三步原则：\n"
        "     a. 先用 FocusOnActors（找场景中最大的 StaticMesh/Landscape）对准目标。\n"
        "        FocusOnActors 比重算 camera position 更可靠，优先使用。\n"
        "     b. 只在 Vision 明确说\"视角太近/太远/被遮挡\"时才手动 SetCameraTransform。\n"
        "        预设角度：pitch=-25 yaw=45 / pitch=-20 yaw=90 / pitch=-55 yaw=0\n"
        "     c. ⚠ 如果 vision_screenshot 返回\"场景为空\"或\"看不到几何体\"：\n"
        "        不要继续调属性！回到 2a，用 FocusOnActors 重新对准。\n"
        "  3. vision_screenshot(question=\"具体问题\") 做视觉验证\n"
        "  4. 需要时用 vision_ask 追问，完成后 vision_reset 闭环\n"
        "\n"
        "  灯光验证特别注意：在被照亮的表面上判断效果，不看编辑器图标颜色。\n"
    )

    # 验证 SOP 简版（软引导，始终可见；完整版在 scene-verification Skill 激活后注入）
    VERIFICATION_SOP_HINT = (
        "\n💡 修改场景后，调 activate_skill(\"验证\") 进入标准视觉验证流程"
        "（L2 读回 → vision_screenshot → vision_ask → 闭环）。"
    )

    def render(self, state: WorldState | None, active_skill: dict | None) -> str:
        parts = [self.AGENT_IDENTITY]
        # 仅在无活跃 Skill 且 State Cache 非空时注入（避免初次启动时噪音）
        if active_skill is None and state is not None:
            parts.append(self.VERIFICATION_SOP_HINT)
        parts.append(_render_state_snapshot(state))
        return "\n".join(parts)


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
        dirty_list = ", ".join(sorted(state.dirty_actors)[:10])
        if len(state.dirty_actors) > 10:
            dirty_list += f" ...等 {len(state.dirty_actors)} 个"
        lines.append(f"- ⚠ 以下 Actor 缓存可能过时：{dirty_list}")

    if state.dirty_toolsets:
        dirty_list = ", ".join(sorted(state.dirty_toolsets)[:5])
        if len(state.dirty_toolsets) > 5:
            dirty_list += f" ...等 {len(state.dirty_toolsets)} 个"
        lines.append(
            f"- ⚠ 以下工具集未受 State Cache 追踪，如需最新状态请手动查询：{dirty_list}"
        )

    # 008 Hard Boundary：漂移检测警告（ADR 0008）
    if state.drift_detected:
        lines.append("")
        lines.append("## ⚠ 世界状态漂移")
        lines.append("关卡在当前会话外被修改过（指纹失配或外部脏包检测）。")
        lines.append("- 请调 **get_context** 获取最新手指纹和漂移详情。")
        lines.append("- 在继续修改前，使用 find_actors / get_actor_transform 重新观测场景。")
        lines.append("- 不要依赖过往会话的记忆——当前世界可能已有差异。")
        if state.last_fingerprint:
            fp = state.last_fingerprint
            lines.append(
                f"- 基准指纹：actorCount={fp.get('actorCount')} "
                f"hash={fp.get('actorNameHash')} "
                f"guid={fp.get('packageGuid','?')[:8]}..."
            )

    # 007 验证闭环：视觉验证反馈
    if state.last_vision_verdict:
        verdict = state.last_vision_verdict
        passed = verdict.get("pass", False)
        reason = verdict.get("reason", "")
        adjustment = verdict.get("adjustment", "")
        at_time = verdict.get("at", "")

        lines.append("")
        if passed:
            lines.append(f"上次视觉验证：✅ 通过 — {reason}")
        else:
            lines.append(f"上次视觉验证：❌ 未通过 — {reason}")
            if adjustment and adjustment != "无需调整":
                lines.append(f"  建议调整：{adjustment}")
        if at_time:
            lines.append(f"  验证时间：{at_time}")

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
