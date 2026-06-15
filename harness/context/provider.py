"""ContextProvider — 三层上下文提供者基类 (Contract 3)

每个 provider 产出自己的文本块，assembler 按 tier 分组后拼接。
tier 决定注入顺序（1 → 2 → 3），同一 tier 内按 priority 排序。

当前实现的 provider:
  - SystemContextProvider (tier=1): Agent 身份 + WorldState 快照
  - TaskContextProvider   (tier=2): Skill 步骤 + 进度（005 实现，当前空壳）
  - ToolReferenceProvider (tier=3): 可用工具名称 + 简述
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.state.models import WorldState


class ContextProvider(ABC):
    """三层上下文管线的一个片段。

    子类只需覆写 render()，设置 tier/priority/enabled 即可。
    """

    tier: int             # 1 = System, 2 = Task, 3 = Tool Reference
    priority: int = 0     # 同 tier 内的排序（值越小越前）
    enabled: bool = True

    @abstractmethod
    def render(
        self,
        state: "WorldState | None",
        active_skill: dict | None,
    ) -> str: ...
