"""Verification Interceptors — L2 读回 + Vision 分析 (Issue 007, 016)

ReadbackInterceptor (Issue 016 Part A):
  写工具调用后自动读回实际值，与意图值 diff。
  白名单映射「写工具 → 读回工具」，读回直接走 ue_client 避免递归。
  失配/读回失败时通过修改 event.parsed_text 注入徽章警告。

VisionInterceptor (Issue 007, 015):
  截图工具调用后自动触发 Vision 分析。
  结果写入 WorldState.last_vision_verdict，供 get_context 消费。

设计约束：
  - 仅覆盖 post_call，不改变工具调用结果
  - 分析失败不阻断主链路（异常在 post_call 内捕获）
  - 拦截器独立：通过 callback 获取外部上下文
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable, TYPE_CHECKING

from harness.client import mcp_extract_text, mcp_unwrap_return_value
from harness.interceptor import ToolCallCompleted, ToolCallInterceptor
from harness.state.models import WorldState
from harness.state.normalize import mcp_tool_short_name, normalize_tool_args
from harness.verification.capturer import parse_screenshot, Screenshot
from harness.verification.vision_agent import VisionSubAgent

if TYPE_CHECKING:
    from harness.client import McpClientSession
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

        if not is_screenshot_tool(event.name) and event.name != "vision_screenshot":
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


# ---- L2 读回验证 (Issue 016 Part A) ----

# 白名单: 写工具短名 → 读回工具短名
_READBACK_MAP: dict[str, str] = {
    "set_actor_transform": "get_actor_transform",
    "set_properties": "get_properties",
}

# 读回失配徽章前缀
_READBACK_MISMATCH_PREFIX = "⚠ L2 读回失配"
_READBACK_FAILURE_PREFIX = "⚠ L2 读回失败"


class ReadbackInterceptor(ToolCallInterceptor):
    """写工具调用后自动读回实际值，与意图值 diff。

    白名单映射「写工具短名 → 读回工具短名」。
    读回调用直接走 ue_client（不经过拦截器链），避免递归触发。
    失配/读回失败时通过修改 event.parsed_text 注入徽章警告。

    Args:
        ue_client: McpClientSession 实例，用于直接调用 UE 工具。
        cache: 全局 WorldState 实例。
        epsilon: 浮点比较容差，默认 1e-3。
    """

    def __init__(
        self,
        ue_client: "McpClientSession",
        cache: WorldState,
        epsilon: float = 5e-3,
    ) -> None:
        self._ue = ue_client
        self._cache = cache
        self._epsilon = epsilon

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """写工具成功后触发 L2 读回验证。

        条件：工具名在白名单中 && 调用成功 && 能提取 actor 名。
        """
        if event.error is not None:
            return
    
        # event.name = "toolset_registry.toolsets.core.object.ObjectTools.set_properties"
        # short = "set_properties"
        short = mcp_tool_short_name(event.name)
        readback_short = _READBACK_MAP.get(short)
        if readback_short is None:
            return

        nc = normalize_tool_args(short, event.args)
        if not nc.actor_name:
            logger.debug("L2 读回跳过: %s 缺少 actor 名", short)
            return

        # 构建读回工具的全限定名（替换短名）
        readback_full = event.name.replace(short, readback_short)
        
        #尝试构造RPC请求，直接向ue发送查询call_tool
        try:
            readback_args = _build_readback_args(short, nc, event.args)
            if readback_args is None:
                logger.debug("L2 读回跳过: %s 无法构建读回参数", short)
                return

            result_text = await self._ue.call_tool(readback_full, readback_args)
            actual = _parse_readback_result(short, result_text)

            mismatches = _diff_values(short, nc.payload, actual, self._epsilon)
            if mismatches:
                badge_lines = [_READBACK_MISMATCH_PREFIX + f": {short}({nc.actor_name})"]
                for m in mismatches:
                    badge_lines.append(f"  {m}")
                badge = "\n".join(badge_lines)
                if event.parsed_text is not None:
                    event.parsed_text = badge + "\n" + event.parsed_text
                logger.warning("L2 读回失配: %s → %s", short, mismatches)
            else:
                logger.debug("L2 读回通过: %s(%s)", short, nc.actor_name)
                # 回写 WorldState，观测标记为已确认
                _confirm_cache(self._cache, short, nc, actual)

        except Exception as e:
            logger.warning("L2 读回失败（不阻断主流程）: %s(%s) → %s",
                           short, nc.actor_name, e)
            badge = (
                f"{_READBACK_FAILURE_PREFIX}: "
                f"{readback_short}({nc.actor_name}) — {e}"
            )
            if event.parsed_text is not None:
                event.parsed_text = badge + "\n" + event.parsed_text


# ---- 读回辅助函数 ----


def _build_readback_args(
    short: str,
    nc: "harness.state.normalize.NormalizedCall",
    write_args: dict,
) -> dict | None:
    """从写工具参数构建读回工具参数。"""
    if short == "set_actor_transform":
        actor = write_args.get("actor")
        if actor is None:
            return None
        return {"actor": actor}

    if short == "set_properties":
        instance = write_args.get("instance")
        if instance is None:
            return None
        # 从 values JSON 中提取属性名列表
        property_names = list(nc.payload.keys()) if nc.payload else []
        if not property_names:
            return None
        return {"instance": instance, "properties": property_names}

    return None


def _parse_readback_result(short: str, result_text: str) -> dict | list:
    """解析 ue_client.call_tool 返回的原始结果，提取实际值。

    result_text 是 JSON-RPC result 的 JSON 字符串。
    解包两层：
      1. MCP content wrapper: {"content": [{"type": "text", "text": "..."}]}
      2. ToolsetRegistry returnValue wrapper: {"returnValue": "<json>"}
    """
    try:
        raw = json.loads(result_text) if isinstance(result_text, str) else result_text
    except (json.JSONDecodeError, TypeError):
        logger.debug("L2 读回结果解析失败（非 JSON）: %.200s", str(result_text))
        return {}

    # 提取内层文本（MCP content[0].text 或 raw 本身）
    text = mcp_extract_text(raw)

    # 按工具类型解析内层值
    if short == "set_actor_transform":
        return _parse_transform_readback(raw, text)

    if short == "set_properties":
        return _parse_properties_readback(text)

    return {}


def _parse_transform_readback(raw: dict, text: str) -> dict:
    """解析 get_actor_transform 的返回值。

    优先从 returnValue 解包，其次从 MCP content text 直接解析，
    最后检查 raw 本身是否是 Transform-like dict。
    """
    # 尝试 returnValue 解包
    rv = mcp_unwrap_return_value(text)
    if rv is not None:
        return rv

    # 尝试从 text 直接解析为 Transform JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and ("translation" in parsed or "rotation" in parsed):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: raw 本身是 Transform-like dict
    if isinstance(raw, dict) and ("translation" in raw or "rotation" in raw):
        return raw

    return {}


def _parse_properties_readback(text: str) -> dict:
    """解析 get_properties 的返回值。

    优先从 returnValue 解包，其次直接从 text 解析 JSON。
    """
    rv = mcp_unwrap_return_value(text)
    if rv is not None:
        return rv

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _diff_values(
    short: str,
    intent: dict,
    actual: dict | list,
    epsilon: float,
) -> list[str]:
    """比较意图值和实际值，返回失配描述列表。"""
    mismatches: list[str] = []

    if short == "set_actor_transform":
        mismatches.extend(_diff_transform(intent, actual, epsilon))
    elif short == "set_properties":
        mismatches.extend(_diff_properties(intent, actual, epsilon))

    return mismatches


def _diff_transform(intent: dict, actual: dict, epsilon: float) -> list[str]:
    """比较 Transform 的 translation/rotation/scale3d。"""
    mismatches: list[str] = []
    xform_intent = intent.get("xform", intent)

    for component in ("translation", "rotation", "scale3d"):
        intent_vec = xform_intent.get(component, {})
        actual_vec = actual.get(component, {})
        if not isinstance(intent_vec, dict) or not isinstance(actual_vec, dict):
            continue
        for axis in ("x", "y", "z"):
            iv = intent_vec.get(axis)
            av = actual_vec.get(axis)
            if iv is None or av is None:
                continue
            try:
                if abs(float(iv) - float(av)) > epsilon:
                    mismatches.append(
                        f"{component}.{axis} 意图={iv} 实际={av}"
                    )
            except (ValueError, TypeError):
                if iv != av:
                    mismatches.append(
                        f"{component}.{axis} 意图={iv} 实际={av}"
                    )
    return mismatches


def _diff_properties(intent: dict, actual: dict, epsilon: float = 1e-6) -> list[str]:
    """按属性名递归比较，数值使用 epsilon 容差.

    支持任意深度嵌套 dict。处理 UE FLinearColor 精度往返
    （0.88 → 0.878431，需 epsilon >= 5e-3）。
    """
    mismatches: list[str] = []
    for key, intent_val in intent.items():
        if key not in actual:
            mismatches.append(f"{key} 读回结果中缺失")
            continue
        actual_val = actual[key]
        # 嵌套 dict: 递归比较
        if isinstance(intent_val, dict) and isinstance(actual_val, dict):
            sub_mismatches = _diff_properties(intent_val, actual_val, epsilon)
            for sm in sub_mismatches:
                mismatches.append(f"{key}.{sm}")
            continue
        # 嵌套 list: 逐元素比较
        if isinstance(intent_val, list) and isinstance(actual_val, list):
            if len(intent_val) != len(actual_val):
                mismatches.append(
                    f"{key} 意图长度={len(intent_val)} 实际长度={len(actual_val)}"
                )
                continue
            for i, (iv, av) in enumerate(zip(intent_val, actual_val)):
                if isinstance(iv, dict) and isinstance(av, dict):
                    sub = _diff_properties(iv, av, epsilon)
                    for sm in sub:
                        mismatches.append(f"{key}[{i}].{sm}")
                elif not _values_equal(iv, av, epsilon):
                    mismatches.append(f"{key}[{i}] 意图={iv} 实际={av}")
            continue
        if not _values_equal(intent_val, actual_val, epsilon):
            mismatches.append(f"{key} 意图={intent_val} 实际={actual_val}")
    return mismatches


def _values_equal(intent_val: object, actual_val: object, epsilon: float = 1e-6) -> bool:
    """比较两个值是否等价（数值使用 epsilon 容差，字符串精确）。

    意图值 2.0 (float) 与实际值 2 (int) 通过 float 转换比较。
    意图值 0.88 与实际值 0.878431 (UE FLinearColor 精度损失) 在 epsilon=5e-3 下通过。
    """
    try:
        if abs(float(intent_val) - float(actual_val)) <= epsilon:
            return True
        return False
    except (ValueError, TypeError):
        pass
    return str(intent_val) == str(actual_val)


def _confirm_cache(
    cache: WorldState,
    short: str,
    nc: "harness.state.normalize.NormalizedCall",
    actual: dict | list,
) -> None:
    """读回确认后更新 WorldState 中的实际值。"""
    actor_name = nc.actor_name
    if not actor_name or actor_name not in cache.actors:
        return

    actor = cache.actors[actor_name]
    actor.last_updated = datetime.now(timezone.utc)

    if short == "set_actor_transform":
        if isinstance(actual, dict):
            actor.transform = actual
        # 清除 dirty 标记
        cache.dirty_actors.discard(actor_name)

    elif short == "set_properties":
        if isinstance(actual, dict):
            for key, val in actual.items():
                actor.properties[key] = str(val) if not isinstance(val, str) else val
        cache.dirty_actors.discard(actor_name)


# ---- 工具名检测 ----

def is_screenshot_tool(name: str) -> bool:
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
