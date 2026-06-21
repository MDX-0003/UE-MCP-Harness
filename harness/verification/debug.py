"""截图/视觉验证调试工具。

两种启用方式（满足任一即生效）：
  1. 环境变量: HARNESS_VISION_DEBUG=1
  2. config.py 字段: vision_debug = True   ← 推荐，持久化，编辑后重启 Harness

开启后，capturer / server 层的异常会打印完整 traceback 到 Harness 终端。
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import Config

logger = logging.getLogger("harness.verification.debug")

_env_key = "HARNESS_VISION_DEBUG"
_config: Config | None = None


def init(config: Config | None = None) -> None:
    """注入 Config 实例（由 cli.py 在启动时调用）。"""
    global _config
    if config is not None:
        _config = config


def enabled() -> bool:
    """检查是否启用了视觉调试模式。"""
    if os.environ.get(_env_key, "").strip() in ("1", "true", "yes", "on"):
        return True
    if _config is not None and _config.vision_debug:
        return True
    return False


def log_exception(exc: Exception, context: str = "") -> None:
    """启用时打印完整 traceback；否则仅一行 warning。"""
    if enabled():
        logger.error(
            "[vision-debug] %s: %s: %s\n%s",
            context, type(exc).__name__, exc,
            traceback.format_exc(),
        )
    else:
        logger.warning("%s: %s: %s", context, type(exc).__name__, exc)
