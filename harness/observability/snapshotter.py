"""Session Snapshot Recorder — 会话级状态快照与归档 (Issue 007+009)

ToolCallInterceptor 的 post_call 实现，将关键会话状态持久化到磁盘：
  - 截图截图 + Vision verdict → {log_dir}/{session_id}/screenshots/
  - get_context 文本 + WorldState JSON → {log_dir}/{session_id}/contexts/
  - Skill 激活/停用记录 → {log_dir}/{session_id}/skills/
  - Session 元数据 → {log_dir}/{session_id}/session.json

设计约束：
  - 仅覆盖 post_call，不改变工具调用结果
  - 文件 I/O 异常不阻断主链路
  - 复用 verification/interceptor.py 的 _is_screenshot_tool / _extract_image_base64
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness import __version__
from harness.interceptor import ToolCallCompleted, ToolCallInterceptor

# Issue 015: 最近一次保存的截图文件路径，供 ToolCallLogger 读取
_last_saved_screenshot_path: str | None = None
from harness.state.models import WorldState
from harness.verification.capturer import parse_screenshot, Screenshot
from harness.verification.interceptor import _is_screenshot_tool

logger = logging.getLogger("harness.observability.snapshotter")


def _short_name(full_name: str) -> str:
    """从全限定工具名中提取短名，用于文件命名。"""
    return full_name.split(".")[-1] if "." in full_name else full_name


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class SnapshotRecorder(ToolCallInterceptor):
    """会话级快照记录器。

    Args:
        snapshot_dir: 快照根目录（通常为 {log_dir}/{session_id}/）。
        cache: 全局 WorldState 实例。
    """

    def __init__(
        self,
        snapshot_dir: Path,
        cache: WorldState,
        get_pending_screenshot: Callable[[], Screenshot | None] | None = None,
    ) -> None:
        self._dir = snapshot_dir
        self._cache = cache
        self._get_pending_screenshot = get_pending_screenshot or (lambda: None)
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._vision_call_count = 0
        self._skills_activated: list[str] = []
        self._tool_call_count = 0

    # ---- 外部通知（server.py 调用） ----

    def on_skill_activated(self, skill_name: str, yaml_text: str) -> None:
        """Skill 激活时保存 YAML 副本到 skills/ 目录。"""
        try:
            skill_dir = self._ensure_dir("skills")
            ts = _timestamp()
            path = skill_dir / f"{ts}_activate_{_safe_name(skill_name)}.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            self._skills_activated.append(skill_name)
            logger.info("Skill snapshot 已保存: %s", path)
        except Exception as e:
            logger.warning("Skill 快照写入失败: %s", e)

    def on_skill_deactivated(self) -> None:
        """Skill 停用时记录时间戳。"""
        try:
            skill_dir = self._ensure_dir("skills")
            ts = _timestamp()
            (skill_dir / f"{ts}_deactivate.txt").write_text(
                f"Skill deactivated at {datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Skill 停用快照写入失败: %s", e)

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        self._tool_call_count += 1
        if event.error is not None:
            return

        if _is_screenshot_tool(event.name):
            self._handle_screenshot(event)

        if event.name == "get_context":
            self._handle_context(event)

    # ---- Session 生命周期 ----

    def write_session_json(self) -> None:
        """写入 session.json 元数据（shutdown 时调用）。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {
                "started": self._started_at,
                "ended": datetime.now(timezone.utc).isoformat(),
                "tool_call_count": self._tool_call_count,
                "vision_call_count": self._vision_call_count,
                "skills_activated": self._skills_activated,
                "map_path": self._cache.map_path,
                "harness_version": __version__,
            }
            path = self._dir / "session.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Session 元数据已保存: %s", path)
        except Exception as e:
            logger.warning("Session 元数据写入失败: %s", e)

    # ---- 内部 ----

    def _ensure_dir(self, sub: str) -> Path:
        d = self._dir / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _handle_screenshot(self, event: ToolCallCompleted) -> None:
        # —— 路径 A: Harness vision_screenshot ——
        if event.name == "vision_screenshot":
            screenshot = self._get_pending_screenshot()
            if screenshot is None:
                return
            b64 = screenshot.data_b64
        else:
            # —— 路径 B: UE 原生截图工具 ——
            raw_dict = event.raw_result
            if raw_dict is None:
                return
            import json as _json
            raw_str = _json.dumps(raw_dict) if not isinstance(raw_dict, str) else raw_dict
            try:
                parsed = parse_screenshot(raw_str)
                if parsed.width == 0:
                    return  # 无有效图片数据
                b64 = parsed.data_b64
            except (ValueError, Exception):
                return
        if not b64:
            return
        ts = _timestamp()
        short = _short_name(event.name)

        try:
            png_dir = self._ensure_dir("screenshots")
            png_path = png_dir / f"{ts}_{short}.png"
            _write_base64_png(b64, png_path)

            verdict = self._cache.last_vision_verdict
            if verdict:
                v_path = png_dir / f"{ts}_{short}.verdict.json"
                v_path.write_text(
                    json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
            self._vision_call_count += 1
            # Issue 015: 暴露给 ToolCallLogger 的截图路径回调
            global _last_saved_screenshot_path
            _last_saved_screenshot_path = str(png_path)
            logger.info("Screenshot snapshot 已保存: %s", png_path)
        except Exception as e:
            logger.warning("Screenshot 快照写入失败: %s", e)

    def _handle_context(self, event: ToolCallCompleted) -> None:
        ts = _timestamp()
        ctx_dir = self._ensure_dir("contexts")
        try:
            if event.parsed_text:
                ctx_path = ctx_dir / f"{ts}_context.txt"
                ctx_path.write_text(event.parsed_text, encoding="utf-8")
                logger.info("Context snapshot 已保存: %s", ctx_path)

            state_json = self._cache.model_dump_json(indent=2)
            state_path = ctx_dir / f"{ts}_state.json"
            state_path.write_text(state_json, encoding="utf-8")
        except Exception as e:
            logger.warning("Context 快照写入失败: %s", e)


def _write_base64_png(b64: str, path: Path) -> None:
    data = base64.b64decode(b64)
    path.write_bytes(data)


def _safe_name(name: str) -> str:
    """将 Skill 名转换为安全的文件名。"""
    import re
    return re.sub(r'[<>:"/\\|?*\s]', '-', name).strip('-')
