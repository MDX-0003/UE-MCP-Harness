"""Vision Sub-Agent 独立配置 — 加载 .vision.env 文件。

Vision Sub-Agent 是 Harness 中唯一需要 LLM API 的模块。
其配置独立于主 Harness 配置，存储在项目根目录的 .vision.env 文件中。

用法：
    from harness.verification.config import load_vision_env

    load_vision_env()            # 将 .vision.env 加载到 os.environ
    config = Config.from_env()   # 随后 Config.from_env() 可读取 vision_* 环境变量
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("harness.verification.config")

# .vision.env 文件名
VISION_ENV_FILE = ".vision.env"

# 默认值
DEFAULT_VISION_API_BASE_URL = "https://token-plan-cn.xiaomimimo.com"
DEFAULT_VISION_MODEL = "claude-sonnet-4-6"


def load_vision_env(project_root: Path | None = None) -> bool:
    """加载 .vision.env 文件中的环境变量。

    仅在环境变量尚未设置时覆盖（不覆盖已存在的值）。
    这意味着 CLI --vision-key 等参数优先级高于 .vision.env。

    Args:
        project_root: 项目根目录。None 时自动检测（当前目录或 harness 包的父目录）。

    Returns:
        True 表示成功加载了 .vision.env 文件，False 表示未找到。
    """
    candidates: list[Path] = []

    if project_root is not None:
        candidates.append(project_root / VISION_ENV_FILE)
    else:
        # 自动检测
        candidates.extend([
            Path.cwd() / VISION_ENV_FILE,
            Path(__file__).resolve().parent.parent.parent / VISION_ENV_FILE,
        ])

    loaded = False
    for dotenv in candidates:
        try:
            if not dotenv.is_file():
                continue
            _parse_and_set(dotenv)
            logger.info("已加载 Vision 配置: %s", dotenv)
            loaded = True
            break  # 只加载第一个找到的
        except Exception:
            pass

    return loaded


def create_vision_env_template(project_root: Path | None = None) -> Path:
    """在项目根目录创建 .vision.env 模板文件（如果不存在）。

    Returns:
        创建的（或已存在的）.vision.env 文件路径。
    """
    root = project_root or Path(__file__).resolve().parent.parent.parent
    path = root / VISION_ENV_FILE

    if path.exists():
        logger.info(".vision.env 已存在: %s", path)
        return path

    template = f"""# Vision Sub-Agent 配置 — Harness 唯一需要 LLM API 的模块
# 编辑此文件后无需重启 Harness（每次 vision check 重新读取）

# API Key（必填——在此填入你的 Anthropic API key）
HARNESS_VISION_API_KEY=

# API 端点（默认为 Anthropic 兼容代理）
HARNESS_VISION_API_BASE_URL={DEFAULT_VISION_API_BASE_URL}

# Vision 模型（Claude 系列带 vision 能力）
HARNESS_VISION_MODEL={DEFAULT_VISION_MODEL}
"""
    path.write_text(template, encoding="utf-8")
    logger.info("已创建 .vision.env 模板: %s", path)
    return path


def _parse_and_set(path: Path) -> None:
    """解析 .env 格式文件，将未设置的环境变量写入 os.environ。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = val
