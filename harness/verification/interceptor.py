"""Vision Interceptor — 截图工具调用后自动触发 Vision 分析 (Issue 007)

ToolCallInterceptor 的 post_call 实现：
  当 LLM 调用截图工具（CaptureEditorImage / Screenshot 等）成功后，
  自动从返回结果提取 base64 图像数据，调用 VisionSubAgent 进行视觉验证，
  结果写入 WorldState.last_vision_verdict，供 get_context 消费。

设计约束：
  - 仅覆盖 post_call，不改变工具调用结果
  - Vision 分析失败不阻断主链路（异常在 post_call 内捕获）
  - 拦截器独立：通过 get_active_skill callback 获取活跃 Skill 上下文
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor
from harness.state.models import WorldState
from harness.verification.vision_agent import VisionSubAgent

logger = logging.getLogger("harness.verification.interceptor")

# 截图工具名关键词（大小写不敏感，短名匹配）
_SCREENSHOT_KEYWORDS = frozenset({
    "captureeditorimage",
    "captureassetimage",
    "screenshot",
})


class VisionInterceptor(ToolCallInterceptor):
    """截图工具调用后自动触发 Vision 分析。

    post_call 中检测截图工具，提取图片数据，调用 VisionSubAgent.check()，
    结果写入 WorldState.last_vision_verdict。

    Args:
        vision_agent: VisionSubAgent 实例（独立 LLM API 客户端）。
        cache: 全局 WorldState 实例。
        get_active_skill: 可选 callback，返回当前活跃 Skill dict 或 None。
    """

    def __init__(
        self,
        vision_agent: VisionSubAgent,
        cache: WorldState,
        get_active_skill: Callable[[], dict | None] | None = None,
    ) -> None:
        self._vision = vision_agent
        self._cache = cache
        self._get_active_skill = get_active_skill or (lambda: None)

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """截图工具成功后触发 Vision 分析。

        条件：工具名匹配截图工具 && 调用成功 && 能从结果中提取到图片数据。
        """
        if event.error is not None:
            return

        if not _is_screenshot_tool(event.name):
            return

        image_b64 = _extract_image_base64(event.raw_result)
        if not image_b64:
            logger.debug("截图工具 %s 返回中无图片数据，跳过 Vision 分析", event.name)
            return

        # 从活跃 Skill 提取验证预期
        expected: str | None = None
        tolerance: float = 0.7
        skill = self._get_active_skill()
        if skill:
            verification = skill.get("verification")
            if isinstance(verification, dict):
                expected = verification.get("expected") or None
                tolerance = verification.get("tolerance", 0.7)

        # 调用 Vision Sub-Agent
        try:
            verdict = await self._vision.check(
                image_b64,
                expected=expected,
                tolerance=tolerance,
            )
            self._cache.last_vision_verdict = {
                "pass": verdict.pass_,
                "reason": verdict.reason,
                "adjustment": verdict.adjustment,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            status = "✅ PASS" if verdict.pass_ else "❌ FAIL"
            logger.info("Vision 分析完成: %s — %s", status, verdict.reason[:120])

        except Exception as e:
            logger.error("Vision 分析异常（不阻断主流程）: %s", e)


# ---- 工具名检测 ----

def _is_screenshot_tool(name: str) -> bool:
    """判断工具名是否属于截图工具（短名关键词匹配，大小写不敏感）。"""
    short = name.split(".")[-1].lower() if "." in name else name.lower()
    return any(kw in short for kw in _SCREENSHOT_KEYWORDS)


# ---- 图片数据提取 ----

def _extract_image_base64(raw: Any) -> str | None:
    """从工具调用的 raw_result 中提取 base64 编码的图片数据。

    支持三种格式：
      1. MCP content image block: {"content": [{"type": "image", "data": "..."}]}
      2. 嵌套 returnValue: {"content": [{"type": "text", "text": "{\"returnValue\":{\"data\":\"...\"}}"}]}
      3. Data URI: "data:image/png;base64,..."
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        content = raw.get("content", [])

        for item in content:
            if not isinstance(item, dict):
                continue

            # 格式 1: 直接 image block
            if item.get("type") == "image":
                data = item.get("data", "")
                if data:
                    return data

            # 格式 2: 文本块中可能嵌套图片数据
            if item.get("type") == "text":
                text = item.get("text", "")

                # 2a: 嵌套 returnValue JSON
                if text.lstrip().startswith("{") and "returnValue" in text:
                    try:
                        inner = json.loads(text)
                        rv = inner.get("returnValue", {})
                        if isinstance(rv, dict):
                            data = rv.get("data", "")
                            if data:
                                return data
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 2b: Data URI
                match = re.search(r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', text)
                if match:
                    return match.group(1)

    # 格式 3: 顶层就是纯 base64 字符串（非 dict 的 raw_result）
    if isinstance(raw, str):
        # Data URI in top-level string
        match = re.search(r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', raw)
        if match:
            return match.group(1)

    return None
