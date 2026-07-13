"""Vision Sub-Agent — 独立于主 Agent 的视觉验证 (Contract / ADR 0006)

设计要点：
  - 独立上下文窗口：不占主 Agent context
  - 接收：截图（base64 PNG）+ 预期描述
  - 返回：{"pass": bool, "reason": str, "adjustment": str}
  - 可追问：{"need_more_info": true, "question": "..."}
  - 独立对话历史：可引用"上次太暗了"做渐变判断
  - Vision API 调用延迟和 token 消耗记录到日志

当前支持 Anthropic Claude Vision 模型。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from harness.config import Config

logger = logging.getLogger("harness.verification.vision_agent")

# Vision Sub-Agent 的系统 prompt — 截图分析
VISION_SYSTEM_PROMPT = """你是一个 Unreal Engine 编辑器截图分析器。
分析截图内容，基于可见事实回答。判断灯光看被照亮表面，不看 gizmo 线框。
如实报告，不迎合提问者。"""

# MiMo 纯文本分类的 system prompt — 属性→维度归类
VISION_CLASSIFY_PROMPT = """你是一个 UE 场景属性分类器。
你的任务是将给定的属性索引按视觉维度归类。
只输出整数索引数组，不编造属性名，不输出属性名字符串。
严格遵守请求中的输出格式，不添加额外文字。"""

# 追加到每条 user message 末尾的 JSON schema 定义
# response_format 参数已强制 JSON 模式，此处只需定义字段语义
_VISION_FORMAT_REMINDER = """
返回格式：
{"answer":"...","confidence":"high|medium|low","caveats":["..."],"observations":[{"what":"...","finding":"...","confidence":"high|medium|low"}],"need_more_info":false,"question":""}
- answer: 你的核心回答。收到验证性提问时请明确给出"符合预期"或"不符合预期"的结论
- confidence: high/medium/low
- caveats: 你识别的限制条件（如"2D截图无法判断深度"），无则 []
- observations: 分维度观察，至少 1 项
- need_more_info: 信息不足需追问时设 true，填写 question"""


@dataclass
class VisionVerdict:
    """Vision Sub-Agent 的统一结构化返回。

    answer:       自然语言回答。
    confidence:   "high" / "medium" / "low" — Vision 对自己回答的置信度。
    caveats:      Vision 主动标注的限制条件。
    observations: 分维度观察列表，每项带独立置信度。
    need_more_info: 是否需要补充信息。
    question:     追问文本（仅在 need_more_info 为 true 时有值）。
    raw_response: 原始 API 响应，用于 debug。
    """
    answer: str
    confidence: str = "medium"
    caveats: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    need_more_info: bool = False
    question: str = ""
    raw_response: str = ""

    # ---- 向后兼容属性（旧代码可能引用） ----

    @property
    def pass_(self) -> bool | None:
        """已废弃。始终返回 None——Vision 不再做二元判定。"""
        return None

    @property
    def reason(self) -> str:
        """已废弃。请用 answer。"""
        return self.answer

    @property
    def adjustment(self) -> str:
        """已废弃。请用 caveats。"""
        return "; ".join(self.caveats) if self.caveats else ""


@dataclass
class VisionSubAgent:
    """独立 Vision Sub-Agent。

    拥有独立的对话历史和状态，在整个任务期间保持同一 session。
    """

    config: Config
    _history: list[dict[str, Any]] = field(default_factory=list)
    _call_count: int = 0

    async def check(
        self,
        image_b64: str,
        expected: str | None = None,
        tolerance: float = 0.7,
        extra_context: str = "",
        question: str = "",
        scene_context: str = "",
    ) -> VisionVerdict:
        """发送截图给 Vision model，返回统一结构化判断。

        Args:
            image_b64: base64 编码的 PNG 截图。
            expected: 预期场景描述。None 或空字符串时走自由描述模式。
            tolerance: 容忍度阈值（0-1）。
            extra_context: 额外上下文（如追问回答）。
            question: 针对性提问。有值时优先级高于 expected。
            scene_context: 场景上下文（从 WorldState 构建），注入 user message 供 Vision 参照。

        Returns:
            VisionVerdict 统一结构化判断结果。
        """
        self._call_count += 1

        # 统一组装 user message（不再区分验证/提问/描述模式）
        parts: list[str] = []
        if question:
            parts.append(f"问题：{question}")
        elif expected and expected != "描述截图内容":
            parts.append(f"预期场景：{expected}")
            parts.append(f"容忍度：{tolerance}")
        else:
            parts.append("请描述这张截图中的场景内容。")
        if scene_context:
            parts.append(f"场景上下文：\n{scene_context}")
        user_message = "\n\n".join(parts)

        if extra_context:
            user_message += f"\n补充信息：{extra_context}"
        # 在 user message 末尾追加格式硬约束（带图片时比 system prompt 更有效）
        user_message += _VISION_FORMAT_REMINDER

        # 构建消息（含历史）
        messages = list(self._history)
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": user_message},
            ],
        })

        try:
            response = await _call_vision_api(
                self.config, messages,
                system=VISION_SYSTEM_PROMPT, temperature=0.7,
            )

            # 保存历史
            self._history.append(messages[-1])
            self._history.append({"role": "assistant", "content": response})

            return _parse_verdict(response)

        except Exception as e:
            logger.error("Vision API 调用失败: %s", e)
            return VisionVerdict(
                answer=f"Vision API 调用失败: {e}",
                confidence="low",
                caveats=["请检查 Vision API key 和网络连接后重试"],
            )

    async def continue_with_info(self, info: str) -> VisionVerdict:
        """用额外信息继续判断——Vision Sub-Agent 追问后，Harness 获取信息并返回。

        Vision → LLM 方向：VisionVerdict.need_more_info → Harness 获取信息 → 此方法。
        """
        self._call_count += 1

        self._history.append({
            "role": "user",
            "content": [{"type": "text", "text": f"补充信息：{info}"}],
        })

        messages = list(self._history)
        try:
            response = await _call_vision_api(
                self.config, messages,
                system=VISION_SYSTEM_PROMPT, temperature=0.7,
            )
            self._history.append({"role": "assistant", "content": response})
            return _parse_verdict(response)
        except Exception as e:
            logger.error("Vision API 继续判断失败: %s", e)
            return VisionVerdict(
                answer=f"Vision API 调用失败: {e}",
                confidence="low",
                caveats=["请重试"],
            )

    async def continue_with_question(
        self,
        question: str,
        scene_context: str = "",
    ) -> VisionVerdict:
        """LLM 主动追问同一 Session 内的截图——无新截图，复用对话历史。

        LLM → Vision 方向（Issue 015 新增）：VisionSessionManager.ask() → 此方法。
        Vision 可以引用历史截图和之前的分析结果（如"上次说的蓝色光"）。
        """
        self._call_count += 1

        user_message = f"追问：{question}"
        if scene_context:
            user_message += f"\n\n{scene_context}"
        user_message += _VISION_FORMAT_REMINDER

        self._history.append({
            "role": "user",
            "content": [{"type": "text", "text": user_message}],
        })

        messages = list(self._history)
        try:
            response = await _call_vision_api(
                self.config, messages,
                system=VISION_SYSTEM_PROMPT, temperature=0.7,
            )
            self._history.append({"role": "assistant", "content": response})
            return _parse_verdict(response)
        except Exception as e:
            logger.error("Vision API 追问失败: %s", e)
            return VisionVerdict(
                answer=f"Vision API 调用失败: {e}",
                confidence="low",
                caveats=["请重试"],
            )

    async def compare_with_reference(
        self,
        ref_image_b64: str,
        cur_image_b64: str,
        question: str,
        scene_context: str = "",
    ) -> VisionVerdict:
        """双图对比——参考图 vs 当前截图，不记入 Session 对话历史。

        与 check() 的区别：
          - 同时发送两张图（参考图 + 当前图），而非单张
          - 不追加到 self._history（单次对比，不影响 Session 内的多轮对话）
          - 复用 _call_vision_api + _parse_verdict
        """
        self._call_count += 1

        user_message = question
        if scene_context:
            user_message += f"\n\n场景上下文：\n{scene_context}"
        user_message += _VISION_FORMAT_REMINDER

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": ref_image_b64,
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": cur_image_b64,
                        },
                    },
                    {"type": "text", "text": user_message},
                ],
            }
        ]

        try:
            response = await _call_vision_api(
                self.config, messages,
                system=VISION_SYSTEM_PROMPT, temperature=0.0,
            )
            return _parse_verdict(response)
        except Exception as e:
            logger.error("Vision 双图对比失败: %s", e)
            return VisionVerdict(
                answer=f"Vision 双图对比失败: {e}",
                confidence="low",
                caveats=["请检查 Vision API key 和网络连接后重试"],
            )

    async def classify(self, prompt: str) -> "dict[str, Any]":
        """纯文本分类——发 prompt 给 MiMo，返回 parsed JSON dict。

        与 check() 的区别：
          - 无图片输入（纯文本消息）
          - 不追加到 self._history（单次分类，不影响 Session 多轮对话）
          - 返回 raw dict 而非 VisionVerdict

        Raises:
            ValueError: MiMo 返回无法解析为 JSON
        """
        self._call_count += 1

        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ]

        try:
            response = await _call_vision_api(
                self.config, messages,
                system=VISION_CLASSIFY_PROMPT, temperature=0,
            )
        except Exception as e:
            raise ValueError(f"MiMo 纯文本调用失败: {e}") from e

        json_str = _extract_json_object(response)
        if not json_str or not json_str.strip().startswith("{"):
            raise ValueError(
                f"MiMo 返回中未找到 JSON 对象。"
                f"原始返回前 300 字符: {response[:300]}"
            )

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"MiMo JSON 解析失败: {e}\n"
                f"提取的 JSON 文本前 200 字符: {json_str[:200]}"
            ) from e

    def reset(self) -> None:
        """重置对话历史（新任务开始时调用）。"""
        self._history.clear()
        self._call_count = 0

    @property
    def history_length(self) -> int:
        return len(self._history)

    @property
    def call_count(self) -> int:
        return self._call_count


async def _call_vision_api(
    config: Config,
    messages: list[dict],
    system: str = "",
    temperature: float | None = None,
) -> str:
    """调用 Anthropic Claude API。

    Args:
        config: Harness 配置。
        messages: 消息历史。
        system: system prompt 文本。空字符串时使用 VISION_SYSTEM_PROMPT 作为默认。
        temperature: 模型 temperature。None 时不传参（使用模型默认值）。
            classify() 应传 0（确定性分类），截图分析应传 0.7（保留适度灵活性）。

    如果未安装 anthropic SDK 或无 API key，返回 mock 响应用于测试。
    """
    if not config.vision_api_key or config.vision_api_key == "test-key":
        logger.debug("Vision API key 缺失或为测试 key，返回 mock 结果")
        return json.dumps({
            "answer": "[MOCK] Vision API 未配置——截图内容应正常显示",
            "confidence": "low",
            "caveats": ["Vision API key 未配置，使用 mock 响应"],
            "observations": [{"what": "mock", "finding": "mock response", "confidence": "low"}],
        })

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK 未安装，返回 mock 结果")
        return json.dumps({
            "answer": "[MOCK] anthropic SDK 未安装——截图内容应正常显示",
            "confidence": "low",
            "caveats": ["anthropic SDK 未安装，使用 mock 响应"],
            "observations": [{"what": "mock", "finding": "mock response", "confidence": "low"}],
        })

    client = anthropic.Anthropic(
        api_key=config.vision_api_key,
        base_url=config.vision_api_base_url,
    )

    if not system:
        system = VISION_SYSTEM_PROMPT
    anthropic_messages = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        # Anthropic 要求 content 是字符串或 content block 列表
        if isinstance(content, str):
            anthropic_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            anthropic_content = []
            for block in content:
                if block.get("type") == "text":
                    anthropic_content.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    anthropic_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.get("source", {}).get("media_type", "image/png"),
                            "data": block.get("source", {}).get("data", ""),
                        },
                    })
            anthropic_messages.append({"role": role, "content": anthropic_content})

    import asyncio
    create_kwargs: dict[str, Any] = {
        "model": config.vision_model,
        "max_tokens": 4096,
        "system": system,
        "messages": anthropic_messages,
        "extra_body": {"response_format": {"type": "json_object"}},
    }
    if temperature is not None:
        create_kwargs["temperature"] = temperature

    response = await asyncio.to_thread(
        client.messages.create,
        **create_kwargs,
    )

    # 提取文本——跳过 thinking block（extended thinking 产物，非正文）
    text = ""
    for block in response.content:
        # 跳过 ThinkingBlock / RedactedThinkingBlock
        if hasattr(block, "thinking"):
            continue
        if hasattr(block, "text"):
            text += block.text
        elif hasattr(block, "content"):
            # 有些代理返回嵌套 content 结构
            inner = block.content
            if isinstance(inner, str):
                text += inner
            elif isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict) and "text" in item:
                        text += item["text"]
                    elif hasattr(item, "text"):
                        text += item.text
        elif isinstance(block, dict):
            text += block.get("text", "")
        # 兜底：未知 block 类型，跳过不拼接（避免 ThinkingBlock repr 污染正文）

    text = text.strip()

    # 检测截断：max_tokens 耗尽时 JSON 不完整，后续 _parse_verdict 会失败
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        logger.warning(
            "Vision 响应可能被截断 (stop_reason=max_tokens, text_len=%d)。"
            "如后续解析失败，考虑进一步增加 max_tokens。",
            len(text),
        )

    logger.info(
        "Vision API 调用完成 — model=%s, tokens_in=%d, tokens_out=%d, text_len=%d, stop_reason=%s",
        config.vision_model,
        response.usage.input_tokens if hasattr(response, 'usage') else 0,
        response.usage.output_tokens if hasattr(response, 'usage') else 0,
        len(text),
        stop_reason,
    )
    # debug 日志输出前 500 字符，方便排查解析问题
    logger.debug("Vision 原始响应前 500 字符: %s", text[:500])

    return text


def _parse_verdict(raw: str) -> VisionVerdict:
    """从 Vision model 返回的文本中解析统一结构化 JSON。

    所有模式（验证/提问/描述）统一走 JSON 解析路径。
    解析失败时以全文作为 answer、confidence=low。
    """
    if not raw or not raw.strip():
        return VisionVerdict(
            answer="(Vision model 返回为空)",
            confidence="low",
            caveats=["Vision API 返回了空响应"],
            raw_response=raw,
        )

    # 尝试提取 JSON 代码块
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # 尝试找到 JSON 对象（支持嵌套花括号的平衡匹配）
        json_str = _extract_json_object(raw)

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        # 解析失败：全文作为 answer，confidence=low
        return VisionVerdict(
            answer=raw[:1000],
            confidence="low",
            caveats=["Vision model 未返回有效 JSON——以下为原始回答"],
            raw_response=raw,
        )

    # 标准化 confidence 值
    confidence = data.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    return VisionVerdict(
        answer=data.get("answer", ""),
        confidence=confidence,
        caveats=data.get("caveats", []),
        observations=data.get("observations", []),
        need_more_info=data.get("need_more_info", False),
        question=data.get("question", ""),
        raw_response=raw,
    )


def _extract_json_object(text: str) -> str:
    """从文本中提取最外层的 JSON 对象（支持嵌套花括号）。"""
    # 找到第一个 {
    start = text.find("{")
    if start == -1:
        return text
    # 平衡匹配花括号
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # 未闭合，返回从 start 到末尾
    return text[start:]
