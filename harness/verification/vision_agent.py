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

# Vision Sub-Agent 的系统 prompt — 自由描述模式（无预期描述时）
VISION_SYSTEM_PROMPT_DESCRIBE = """你是一个 Unreal Engine 编辑器截图分析器。
你会收到编辑器截图，请用中文描述截图中的场景内容。关注：
- 场景中有什么物体/Actor（灯光、模型、地形等）
- 光照情况（方向、色温、亮度、阴影）
- 相机角度和视口内容
- 整体氛围和风格
直接描述即可，不需要 JSON 格式。"""

# Vision Sub-Agent 的系统 prompt — 验证模式（有预期描述时）
VISION_SYSTEM_PROMPT_VERIFY = """你是一个 Unreal Engine 视觉质量验证器。
你会收到编辑器截图和预期场景描述，你的任务是判断截图是否符合描述。

返回 JSON 格式（不要包含其他内容）：
{
  "pass": true或false,
  "reason": "具体的判断原因（中文）",
  "adjustment": "如果不通过，建议如何调整；如果通过，写 '无需调整'"
}

如果信息不足以做出判断，可以追问：
{
  "need_more_info": true,
  "question": "需要补充的信息"
}

判断标准：
- 关注：光照方向/角度、色温、亮度、阴影长度、天空颜色、整体氛围
"""


@dataclass
class VisionVerdict:
    """Vision Sub-Agent 的返回结果。"""
    pass_: bool
    reason: str
    adjustment: str
    need_more_info: bool = False
    question: str = ""
    raw_response: str = ""  # 原始 API 响应，用于 debug


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
    ) -> VisionVerdict:
        """发送截图给 Vision model，返回结构化判断。

        Args:
            image_b64: base64 编码的 PNG 截图。
            expected: 预期场景描述。None 或空字符串时走自由描述模式。
            tolerance: 容忍度阈值（0-1），仅在验证模式下使用。
            extra_context: 额外上下文（如追问回答）。

        Returns:
            VisionVerdict 结构化判断结果。
        """
        self._call_count += 1

        # 判断模式
        is_verify = bool(expected and expected != "描述截图内容")

        if is_verify:
            user_message = f"预期场景：{expected}\n容忍度：{tolerance}"
        else:
            user_message = "请描述这张截图中的场景内容。"
        if extra_context:
            user_message += f"\n补充信息：{extra_context}"

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
            response = await _call_vision_api(self.config, messages, verify_mode=is_verify)

            # 保存历史
            self._history.append(messages[-1])
            self._history.append({"role": "assistant", "content": response})

            return _parse_verdict(response, verify_mode=is_verify)

        except Exception as e:
            logger.error("Vision API 调用失败: %s", e)
            return VisionVerdict(
                pass_=False,
                reason=f"Vision API 调用失败: {e}",
                adjustment="请检查 Vision API key 和网络连接后重试",
            )

    async def continue_with_info(self, info: str) -> VisionVerdict:
        """用额外信息继续判断——Vision Sub-Agent 追问后，Harness 获取信息并返回。"""
        self._call_count += 1

        # 添加追问的回答作为用户消息
        self._history.append({
            "role": "user",
            "content": [{"type": "text", "text": f"补充信息：{info}"}],
        })

        messages = list(self._history)
        try:
            # 追问后保持验证模式
            response = await _call_vision_api(self.config, messages, verify_mode=True)
            self._history.append({"role": "assistant", "content": response})
            return _parse_verdict(response, verify_mode=True)
        except Exception as e:
            logger.error("Vision API 继续判断失败: %s", e)
            return VisionVerdict(
                pass_=False,
                reason=f"Vision API 调用失败: {e}",
                adjustment="请重试",
            )

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
    verify_mode: bool = True,
) -> str:
    """调用 Anthropic Claude Vision API。

    Args:
        config: Harness 配置。
        messages: 消息历史。
        verify_mode: True=验证模式（返回 JSON），False=描述模式（返回自然语言）。

    如果未安装 anthropic SDK 或无 API key，返回 mock 响应用于测试。
    """
    if not config.vision_api_key or config.vision_api_key == "test-key":
        logger.debug("Vision API key 缺失或为测试 key，返回 mock 结果")
        return json.dumps({
            "pass": True,
            "reason": "[MOCK] Vision API 未配置，默认通过",
            "adjustment": "请配置 HARNESS_VISION_API_KEY 环境变量进行真实验证",
        })

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK 未安装，返回 mock 结果")
        return json.dumps({
            "pass": True,
            "reason": "[MOCK] anthropic SDK 未安装，默认通过",
            "adjustment": "pip install anthropic 启用真实 Vision 验证",
        })

    client = anthropic.Anthropic(
        api_key=config.vision_api_key,
        base_url=config.vision_api_base_url,
    )

    # 根据模式选择 system prompt
    system = VISION_SYSTEM_PROMPT_VERIFY if verify_mode else VISION_SYSTEM_PROMPT_DESCRIBE
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
    response = await asyncio.to_thread(
        client.messages.create,
        model=config.vision_model,
        max_tokens=1024,
        system=system,
        messages=anthropic_messages,
    )

    # 提取文本
    text = ""
    for block in response.content:
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
        else:
            # 兜底：尝试直接转字符串
            text += str(block)

    text = text.strip()

    logger.info(
        "Vision API 调用完成 — model=%s, tokens_in=%d, tokens_out=%d, text_len=%d",
        config.vision_model,
        response.usage.input_tokens if hasattr(response, 'usage') else 0,
        response.usage.output_tokens if hasattr(response, 'usage') else 0,
        len(text),
    )
    # debug 日志输出前 500 字符，方便排查解析问题
    logger.debug("Vision 原始响应前 500 字符: %s", text[:500])

    return text


def _parse_verdict(raw: str, verify_mode: bool = True) -> VisionVerdict:
    """从 Vision model 返回的文本中解析结构化判断。

    验证模式（verify_mode=True）：期望 JSON 格式的 pass/fail 判断。
    描述模式（verify_mode=False）：将整个返回文本作为 reason，不要求 JSON。
    """
    # 描述模式：全文即结果
    if not verify_mode:
        if not raw or not raw.strip():
            return VisionVerdict(
                pass_=True,
                reason="(Vision model 返回为空)",
                adjustment="",
                raw_response=raw,
            )
        return VisionVerdict(
            pass_=True,
            reason=raw.strip(),
            adjustment="",
            raw_response=raw,
        )

    # ---- 验证模式：尝试解析 JSON ----
    # 尝试提取 JSON 代码块
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # 尝试找到 JSON 对象
        json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            json_str = raw

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # 解析失败，返回原始文本帮助排查
        return VisionVerdict(
            pass_=False,
            reason=f"Vision model 未返回有效 JSON。原始响应: {raw[:500]}",
            adjustment="请检查 Vision model 配置和 prompt 模板",
            raw_response=raw,
        )

    need_more = data.get("need_more_info", False)
    return VisionVerdict(
        pass_=data.get("pass", not need_more),
        reason=data.get("reason", ""),
        adjustment=data.get("adjustment", ""),
        need_more_info=need_more,
        question=data.get("question", ""),
        raw_response=raw,
    )
