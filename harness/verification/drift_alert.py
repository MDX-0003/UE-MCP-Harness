"""DriftAlertInterceptor — 世界状态漂移后的强制提醒。

当 Hard Boundary 检测到 drift_detected 后，下一次工具调用的返回值
会被注入醒目的漂移警告，确保 LLM 不会错过。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor

if TYPE_CHECKING:
    from harness.state.models import WorldState

logger = logging.getLogger("harness.verification.drift_alert")

DRIFT_WARNING = (
    "\n\n"
    "========================================\n"
    "⚠  WORLD DIVERGENCE DETECTED\n"
    "The level was modified outside this Harness session.\n"
    "→ Call get_context immediately to see the current fingerprint.\n"
    "→ Re-observe the scene (find_actors / get_properties) before continuing.\n"
    "→ Do NOT trust cached observations from earlier in this session.\n"
    "========================================\n"
)


class DriftAlertInterceptor(ToolCallInterceptor):
    """在漂移检测后的第一次工具调用结果中注入警告。

    只触发一次——警告注入后将 drift_detected 标记为已通知，
    避免每条工具调用都被污染。
    """

    def __init__(self, cache: WorldState) -> None:
        self._cache = cache
        self._alerted = False

    async def post_call(self, event: ToolCallCompleted) -> None:
        """检测到漂移且尚未通知时，在结果文本中注入警告。"""
        if not self._cache.drift_detected or self._alerted:
            return

        if event.parsed_text is not None:
            # 注入到 parsed_text——这个字段是 server.py 中所有 interceptor
            # 共享的，后续的 interceptor (logger, snapshotter) 也会看到
            event.parsed_text = DRIFT_WARNING + event.parsed_text

        self._alerted = True
        logger.info("漂移警告已注入 tool call 结果")
