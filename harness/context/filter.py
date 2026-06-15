"""工具过滤 — 自由探索模式 / Skill 模式工具白名单。

过滤逻辑（纯函数，可脱离 Harness 测试）：
  - 自由探索模式：仅暴露 allowlist 匹配的工具 + 逃生通道（list_toolsets / describe_toolset）
  - Skill 模式：仅暴露 Skill YAML 的 tools_allowlist（005 调用）
"""

from __future__ import annotations

# 始终对 LLM 可见的逃生通道——即使不匹配 allowlist 也可见
ESCAPE_HATCH_TOOLS = frozenset({
    "list_toolsets",
    "describe_toolset",
})


def apply_filter(
    raw_tools: list[dict],
    allowlist: tuple[str, ...],
    *,
    extra_allowed: frozenset[str] | None = None,
) -> list[dict]:
    """过滤工具列表，仅保留 allowlist 匹配和逃生通道工具。

    Args:
        raw_tools: UE 端原始工具列表（含全限定名）。
        allowlist: 工具名子串匹配模式列表。例如 ("SceneTools.", "EditorAppToolset.")。
        extra_allowed: 额外放行的工具全名集合（Skill 模式下的 tools_allowlist）。

    Returns:
        过滤后的工具列表。
    """
    # 合并逃生通道
    escape = ESCAPE_HATCH_TOOLS
    if extra_allowed:
        escape = ESCAPE_HATCH_TOOLS | extra_allowed

    result: list[dict] = []
    for tool in raw_tools:
        name = tool.get("name", "")

        # 逃生通道始终可见
        if any(name.endswith(e) or name == e for e in escape):
            result.append(tool)
            continue

        # allowlist 子串匹配
        if any(pattern in name for pattern in allowlist):
            result.append(tool)
            continue

    return result


def is_escape_hatch(tool_name: str) -> bool:
    """判断工具名是否为逃生通道。"""
    return any(tool_name.endswith(e) or tool_name == e for e in ESCAPE_HATCH_TOOLS)
