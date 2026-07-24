"""Skill 工具组 handlers — activate_skill / save_skill / deactivate_skill / get_context。

涉及的 Issue：005（Skill 系统）、018（注册表化）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.types import CallToolResult

from harness.tools import ToolContext, log_local_call, tool_ok, tool_fail
from harness.context.skill_registry import _normalize_list

logger = logging.getLogger("harness.skill_tools")


def _parse_skill_yaml_to_dict(yaml_text: str) -> dict:
    """将原始 YAML 文本解析为 skill 兼容格式。"""
    import yaml
    parsed = yaml.safe_load(yaml_text) or {}
    if not isinstance(parsed, dict):
        return {"name": "", "description": "", "triggers": [], "tools_allowlist": [], "steps": ""}
    return {
        "name": str(parsed.get("name", "")),
        "description": str(parsed.get("description", "")),
        "triggers": _normalize_list(parsed.get("triggers", [])),
        "tools_allowlist": _normalize_list(parsed.get("tools_allowlist", [])),
        "steps": str(parsed.get("steps", "")),
    }


def _list_skill_names(ctx: ToolContext) -> str:
    skills = ctx.skill_registry.list_skills()
    if not skills:
        return "(无可用 Skill)"
    return ", ".join(
        f"{s.name}({s.description[:30]}...)"
        if len(s.description) > 30 else f"{s.name}({s.description})"
        for s in skills
    )


async def handle_activate_skill(ctx: ToolContext, arguments: dict) -> CallToolResult:
    query = arguments.get("name_or_desc", "")

    # 空查询 → 重新扫描目录（感知外部 YAML 变更）
    if not query.strip():
        count = ctx.skill_registry.reload()
        return tool_ok(
            f"已重新扫描 Skill 目录，发现 {count} 个 Skill。"
            f"可用: {_list_skill_names(ctx)}",
        )

    matches = ctx.skill_registry.match_skill(query)

    if not matches:
        return tool_ok(
            f"未找到匹配 '{query}' 的 Skill。可用 Skill: {_list_skill_names(ctx)}",
        )

    if len(matches) == 1:
        skill = matches[0]
        yaml_text = ctx.skill_registry.load_skill_yaml(skill.name)
        if yaml_text:
            if ctx.skill_ref is not None:
                ctx.skill_ref[0] = _parse_skill_yaml_to_dict(yaml_text)
            if ctx.snapshot_recorder is not None:
                ctx.snapshot_recorder.on_skill_activated(skill.name, yaml_text)
            logger.info("Skill 已激活: %s", skill.name)
            return tool_ok(
                f"Skill '{skill.name}' 已激活。{skill.description}\n"
                f"步骤 ({skill.steps_count} 步)、"
                f"工具白名单 ({len(skill.tools_allowlist)} 个): "
                f"{', '.join(skill.tools_allowlist[:5])}"
                f"{'...' if len(skill.tools_allowlist) > 5 else ''}",
            )

    # 多匹配：列出备选
    lines = [f"找到 {len(matches)} 个匹配 '{query}' 的 Skill，请选择一个："]
    for m in matches:
        lines.append(f"  - {m.name}: {m.description or '(无描述)'}")
    return tool_ok("\n".join(lines))


async def handle_save_skill(ctx: ToolContext, arguments: dict) -> CallToolResult:
    skill_name = arguments.get("name", "")
    yaml_content = arguments.get("yaml_content", "")

    if not skill_name or not yaml_content:
        return tool_fail("错误: name 和 yaml_content 均为必填")

    # 重复名检查
    existing = ctx.skill_registry.get_skill(skill_name)
    if existing and not arguments.get("overwrite", False):
        return tool_ok(
            f"Skill '{skill_name}' 已存在。传 overwrite=true 覆盖，或先调 delete 再 save。",
        )

    try:
        info = ctx.skill_registry.save_skill(skill_name, yaml_content)
        return tool_ok(
            f"Skill '{info.name}' 已保存。{info.description}\n"
            f"步骤: {info.steps_count} 步, "
            f"工具: {', '.join(info.tools_allowlist)}",
        )
    except ValueError as e:
        return tool_fail(f"保存失败: {e}")


async def handle_get_context(ctx: ToolContext, arguments: dict) -> CallToolResult:
    from harness.context.prompt import assemble_system_prompt
    prompt = assemble_system_prompt(
        ctx.context_providers,
        ctx.world_state,
        ctx.skill_ref[0] if ctx.skill_ref else None,
    )
    return tool_ok(prompt)


async def handle_deactivate_skill(ctx: ToolContext, arguments: dict) -> CallToolResult:
    was_active = ctx.skill_ref is not None and ctx.skill_ref[0] is not None
    if ctx.skill_ref is not None:
        ctx.skill_ref[0] = None
    if ctx.snapshot_recorder is not None:
        ctx.snapshot_recorder.on_skill_deactivated()
    if was_active:
        return tool_ok("已退出 Skill 模式，回到自由探索模式。")
    return tool_ok("当前未激活任何 Skill，已在自由探索模式。")
