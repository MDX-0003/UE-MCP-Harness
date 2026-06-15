"""可观测性 — ToolCallLogger：ToolCallInterceptor 的日志实现。

每次 tool call 完成（post_call）时，异步将一条 JSONL 追加到
{log_dir}/{session_id}.jsonl 文件。不阻塞主链路。

日志格式（Contract 1 / architecture.md §2.4）：
{
  "timestamp": "2026-06-13T10:00:00.000Z",
  "session_id": "abc123",
  "task_id": "coffee-shop-001",
  "tool_name": "SceneTools.find_actors",
  "tool_input": {"glob": "DirectionalLight*"},
  "tool_output": "[\"DirectionalLight_0\", \"DirectionalLight_1\"]",
  "error": null,
  "duration_ms": 45,
  "screenshot_path": null,
  "verification": null
}
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor

logger = logging.getLogger("harness.observability.logger")


class ToolCallLogger(ToolCallInterceptor):
    """以装饰器模式包裹 call_tool，记录所有工具调用到 JSONL 日志文件。

    日志写入使用 asyncio.create_task，不增加 tool call 的感知延迟。
    写入失败记 error 日志但不抛异常——日志不应阻断主业务。
    """

    def __init__(self, log_dir: Path, session_id: str = "") -> None:
        self._log_dir = log_dir
        self._session_id = session_id
        self._log_path: Path | None = None
        self._write_queue: asyncio.Queue[str] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        """初始化日志目录和后台写入协程。"""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._log_path = self._log_dir / f"{self._session_id or ts}.jsonl"
        self._writer_task = asyncio.create_task(self._background_writer())
        logger.info("日志文件: %s", self._log_path)

    async def stop(self) -> None:
        """等待队列排空，关闭后台写入。"""
        if self._writer_task is None:
            return
        await self._write_queue.put(None)  # 哨兵
        try:
            await asyncio.wait_for(self._writer_task, timeout=5.0)
        except asyncio.TimeoutError:
            self._writer_task.cancel()
        self._writer_task = None

    async def _background_writer(self) -> None:
        """后台协程：从队列取 JSON 行，批量写入文件。"""
        if self._log_path is None:
            return
        # 打开文件（追加模式，行缓冲）
        with open(self._log_path, "a", encoding="utf-8") as f:
            while True:
                line = await self._write_queue.get()
                if line is None:  # 哨兵
                    self._write_queue.task_done()
                    break
                try:
                    f.write(line + "\n")
                    f.flush()
                except Exception:
                    logger.exception("写入日志行失败")
                finally:
                    self._write_queue.task_done()

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """将一次完成的工具调用序列化为 JSON 行并加入写入队列。

        时间戳在 post_call 中生成——这是工具调用完成的确切时间。
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self._session_id,
            "tool_name": event.name,
            "tool_input": _serialize_args(event.args),
            "tool_output": _truncate(event.parsed_text, 2000) if event.parsed_text else None,
            "error": str(event.error) if event.error else None,
            "duration_ms": round(event.duration_ms, 1),
            "screenshot_path": None,
            "verification": None,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            await self._write_queue.put(line)
        except Exception:
            logger.exception("序列化日志行失败: %s", event.name)


# ---- 辅助 ----

def _serialize_args(args: dict) -> dict:
    """截断参数中的大值，避免日志膨胀。

    规则：
      - 字符串 > 200 字符 → 截断并标记
      - dict/list 保持原样（由 JSON 序列化控制总长度）
    """
    if not args:
        return {}
    result = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            result[k] = v[:200] + "...[truncated]"
        else:
            result[k] = v
    return result


def _truncate(text: str, max_len: int = 2000) -> str:
    """截断超长文本到 max_len，末尾标记截断。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "...[truncated]"
