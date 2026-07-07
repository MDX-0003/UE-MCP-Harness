"""Vision Interceptor — 截图工具调用后自动触发 Vision 分析 (Issue 007)

ToolCallInterceptor 的 post_call 实现：
  当 LLM 调用截图工具（CaptureEditorImage / Screenshot 等）成功后，
  自动从返回结果提取 base64 图像数据，调用 VisionSubAgent 进行视觉验证，
  结果写入 WorldState.last_vision_verdict，供 get_context 消费。

Issue 015 修订：对接 VisionSessionManager，不再直接调用 VisionSubAgent。
  自动截图触发仍有 Vision 分析，但 Session 管理由 VisionSessionManager 负责。

设计约束：
  - 仅覆盖 post_call，不改变工具调用结果
  - Vision 分析失败不阻断主链路（异常在 post_call 内捕获）
  - 拦截器独立：通过 get_active_skill callback 获取活跃 Skill 上下文
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable, TYPE_CHECKING

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor
from harness.state.models import WorldState
from harness.verification.capturer import parse_screenshot, Screenshot
from harness.verification.vision_agent import VisionSubAgent

if TYPE_CHECKING:
    from harness.verification.session import VisionSessionManager

logger = logging.getLogger("harness.verification.interceptor")

# 截图工具名关键词（大小写不敏感，短名匹配）
_SCREENSHOT_KEYWORDS = frozenset({
    "captureeditorimage",
    "captureassetimage",
    "screenshot",
})


class VisionInterceptor(ToolCallInterceptor):
    """截图工具调用后自动触发 Vision 分析。

    post_call 中检测截图工具，提取图片数据，调用 Vision 分析，
    结果写入 WorldState.last_vision_verdict。

    Issue 015: 可选择对接 VisionSessionManager。
    有 SessionManager 时通过 Session API 调用；无 SessionManager 时保留旧路径。

    Args:
        vision_agent: VisionSubAgent 实例（旧路径，保留向后兼容）。
        cache: 全局 WorldState 实例。
        get_active_skill: 可选 callback，返回当前活跃 Skill dict 或 None。
        get_pending_screenshot: 可选 callback，返回待处理的 Screenshot。
        session_manager: 可选 VisionSessionManager（Issue 015 新路径）。
    """

    def __init__(
        self,
        vision_agent: VisionSubAgent,
        cache: WorldState,
        get_active_skill: Callable[[], dict | None] | None = None,
        get_pending_screenshot: Callable[[], Screenshot | None] | None = None,
        session_manager: "VisionSessionManager | None" = None,
    ) -> None:
        self._vision = vision_agent
        self._cache = cache
        self._get_active_skill = get_active_skill or (lambda: None)
        self._get_pending_screenshot = get_pending_screenshot or (lambda: None)
        self._session_mgr = session_manager

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """截图工具成功后触发 Vision 分析。

        条件：工具名匹配截图工具 && 调用成功 && 能从结果中提取到图片数据。
        """
        if event.error is not None:
            return

        if not _is_screenshot_tool(event.name) and event.name != "vision_screenshot":
            return

        # —— 路径 A: Harness vision_screenshot ——
        if event.name == "vision_screenshot":
            screenshot = self._get_pending_screenshot()
            if screenshot is None:
                logger.debug("vision_screenshot 回调返回空，跳过 Vision 分析")
                return
            image_b64 = screenshot.data_b64
        else:
            # —— 路径 B: UE 原生截图工具 ——
            image_b64 = _extract_image_b64(event)

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

        # 从 event.args 中提取针对性提问（Issue 015: question 参数）
        question = event.args.get("question", "") if event.args else ""

        # 调用 Vision 分析
        try:
            if self._session_mgr is not None:
                # Issue 015 新路径：通过 SessionManager
                if event.name == "vision_screenshot":
                    meta = {
                        "width": 0, "height": 0, "mode": event.args.get("mode", "viewport") if event.args else "viewport"
                    }
                    verdict = await self._session_mgr.add_screenshot(
                        image_b64, meta, question=question,
                    )
                else:
                    # UE 原生截图工具 → SessionManager 旧路径
                    meta = {"width": 0, "height": 0, "mode": "unknown"}
                    verdict = await self._session_mgr.add_screenshot(
                        image_b64, meta, question=question,
                    )
            else:
                # 旧路径：直接调 VisionSubAgent
                verdict = await self._vision.check(
                    image_b64,
                    expected=expected,
                    tolerance=tolerance,
                    question=question,
                )

            self._cache.last_vision_verdict = {
                "answer": verdict.answer,
                "confidence": verdict.confidence,
                "caveats": verdict.caveats,
                "observations": verdict.observations,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            badges = {"high": "🟢", "medium": "🟡", "low": "🔴"}
            logger.info("Vision 分析完成: %s confidence=%s — %s",
                        badges.get(verdict.confidence, "🟡"),
                        verdict.confidence,
                        verdict.answer[:120])

        except Exception as e:
            logger.error("Vision 分析异常（不阻断主流程）: %s", e)


# ---- 工具名检测 ----

def _is_screenshot_tool(name: str) -> bool:
    """
    判断工具名是否属于UE的截图工具（短名关键词匹配，大小写不敏感）。
    不论这个tool来自harness自己的tool还是ue的tool，都会return ture
    """
    short = name.split(".")[-1].lower() if "." in name else name.lower()
    return any(kw in short for kw in _SCREENSHOT_KEYWORDS)


# ---- 图片数据提取 ----

def _extract_image_b64(event: ToolCallCompleted) -> str | None:
    """从工具调用事件中提取 base64 图片数据。

    Harness vision_screenshot 工具：通过 VisionInterceptor 持有的回调获取
    Screenshot 对象（已在 capturer.capture() 内完成解析 + resize）。

    UE 原生截图工具（CaptureEditorImage / Screenshot 等）：
    通过 capturer.parse_screenshot() 从 raw_result 中提取——6 种格式、
    isError 检测、padding 修复、PIL resize 全部复用。
    """
    # —— 路径 A: Harness vision_screenshot ——
    if event.name == "vision_screenshot":
        # 回调由 server.py 注入，capturer.capture() 已完成解析+resize
        return None  # 由 post_call 中通过 self._get_pending_screenshot() 获取

    # —— 路径 B: UE 原生截图工具（CaptureEditorImage / Screenshot 等） ——
    raw_dict = event.raw_result
    if raw_dict is None:
        return None

    try:
        raw_str = json.dumps(raw_dict) if not isinstance(raw_dict, str) else raw_dict
        screenshot = parse_screenshot(raw_str)
        # parse_screenshot 对无效图片数据返回 width=0 的 Screenshot（含 padding 修改）
        if screenshot.width == 0:
            logger.debug("截图工具 %s 返回中无有效图片数据", event.name)
            return None
        return screenshot.data_b64
    except ValueError:
        logger.debug("截图工具 %s 返回错误响应，跳过 Vision 分析", event.name)
        return None
    except Exception:
        logger.debug("从 raw_result 提取 base64 失败: %s", event.name)
        return None
