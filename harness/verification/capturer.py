"""截图获取 — 通过 MCP 调用 SlateInspector.Screenshot() 或 EditorAppToolset.CaptureEditorImage()。

FToolsetImage 返回 MimeType: "image/png" + Data: base64，Harness 零转码直传 Vision API。
截图在发送前 resize 到最大 config.vision_max_size（默认 1024x768），保持宽高比。
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.client import McpClientSession

logger = logging.getLogger("harness.verification.capturer")


@dataclass
class Screenshot:
    """截图结果。"""
    data_b64: str          # base64 编码的 PNG 数据
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0


async def capture(
    ue_client: McpClientSession,
    max_width: int = 1024,
    max_height: int = 768,
) -> Screenshot:
    """通过 MCP 获取 UE 编辑器截图。

    优先使用 SlateInspector.Screenshot()，失败则尝试 EditorAppToolset.CaptureEditorImage()。
    返回的 base64 数据已经过 resize 处理。
    """
    # 尝试 SlateInspector.Screenshot
    try:
        result = await ue_client.call_tool(
            "ToolsetRegistry.Plugin.SlateInspectorToolset.SlateInspector.Screenshot",
            {},
        )
        if result:
            return _parse_and_resize(result, max_width, max_height)
    except Exception:
        logger.debug("SlateInspector.Screenshot 失败，尝试 EditorAppToolset.CaptureEditorImage")

    # 回退方案
    try:
        result = await ue_client.call_tool(
            "ToolsetRegistry.EditorAppToolset.CaptureEditorImage",
            {},
        )
        logger.debug("CaptureEditorImage 原始返回前 200 字符: %s", str(result)[:200])
        return _parse_and_resize(result, max_width, max_height)
    except Exception as e:
        logger.error("截图工具调用失败: %s", e)
        raise


def capture_from_file(path: Path, max_width: int = 1024, max_height: int = 768) -> Screenshot:
    """从本地文件读取截图（用于测试和离线 replay）。"""
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")

    # 获取尺寸（不依赖 PIL）
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w > max_width or h > max_height:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            w, h = img.size
        return Screenshot(data_b64=b64, width=w, height=h)
    except ImportError:
        logger.debug("PIL 未安装，跳过 resize")
        return Screenshot(data_b64=b64)


def _parse_and_resize(raw: str, max_width: int, max_height: int) -> Screenshot:
    """解析 MCP 返回的截图数据并 resize。

    MCP 可能返回两种格式：
      1. image content block: {"content": [{"type": "image", "data": "...", "mimeType": "image/png"}]}
      2. text content block 中嵌 base64: {"content": [{"type": "text", "text": "<base64>"}]}
      3. 直接是 base64 字符串（无 JSON 包装）
    """
    import json
    import re

    b64_data = raw

    # 尝试解析 JSON
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            # 检查是否为错误响应
            if parsed.get("isError"):
                content = parsed.get("content", [])
                err_text = ""
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        err_text = item.get("text", "")
                        break
                raise ValueError(f"UE 截图失败: {err_text}")

            content = parsed.get("content", [])
            for item in content:
                if not isinstance(item, dict):
                    continue
                # 格式 1: image block
                if item.get("type") == "image":
                    b64_data = item.get("data", "")
                    if b64_data:
                        break
                # 格式 2: text block 中可能嵌了 base64
                if item.get("type") == "text":
                    text = item.get("text", "")

                    # 格式 2a: UE 工具返回嵌套 JSON — {"returnValue":{"mimeType":"...","data":"<base64>"}}
                    if text.lstrip().startswith("{") and "returnValue" in text:
                        try:
                            inner = json.loads(text)
                            rv = inner.get("returnValue", {})
                            if isinstance(rv, dict) and rv.get("data"):
                                b64_data = rv["data"]
                                logger.debug("从嵌套 returnValue JSON 提取 base64，长度=%d", len(b64_data))
                                break
                        except json.JSONDecodeError:
                            pass  # 继续尝试其他格式

                    # 格式 2b: data URI — data:image/png;base64,...
                    data_uri_match = re.search(
                        r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', text
                    )
                    if data_uri_match:
                        b64_data = data_uri_match.group(1)
                        logger.debug("从 data URI 提取 base64，长度=%d", len(b64_data))
                        break

                    # 格式 2c: 纯 base64 字符串
                    cleaned = re.sub(r'\s+', '', text)
                    if _looks_like_base64(cleaned):
                        b64_data = cleaned
                        logger.debug("从 text 提取纯 base64，长度=%d", len(b64_data))
                        break
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # 如果 b64_data 还是原始 raw，尝试直接从 raw 中提取
    if b64_data == raw:
        # 尝试 data URI
        data_uri_match = re.search(
            r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', raw
        )
        if data_uri_match:
            b64_data = data_uri_match.group(1)
            logger.debug("从原始文本提取 data URI base64，长度=%d", len(b64_data))

    # 验证 base64 有效性
    cleaned = re.sub(r'\s+', '', b64_data)
    if len(cleaned) % 4 != 0:
        # 补齐 padding
        cleaned += '=' * (4 - len(cleaned) % 4)
    b64_data = cleaned

    # Resize
    try:
        data = base64.b64decode(b64_data)
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w > max_width or h > max_height:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_data = base64.b64encode(buf.getvalue()).decode("ascii")
            w, h = img.size
        return Screenshot(data_b64=b64_data, width=w, height=h)
    except ImportError:
        logger.debug("PIL 未安装，返回原始截图尺寸未知")
        return Screenshot(data_b64=b64_data)
    except Exception as e:
        logger.warning("Resize 失败: %s，base64 前 80 字符: %s", e, b64_data[:80])
        return Screenshot(data_b64=b64_data)


def _looks_like_base64(s: str) -> bool:
    """判断字符串是否看起来像 base64 编码的二进制数据。"""
    import re
    if not s or len(s) < 20:
        return False
    # base64 字符集 + 可选 padding
    return bool(re.match(r'^[A-Za-z0-9+/]+=*$', s))
