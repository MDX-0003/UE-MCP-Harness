"""可观测性 — ToolCallLogger：ToolCallInterceptor 的日志实现。

每次 tool call 完成（post_call）时，异步将一条 JSONL 追加到
{log_dir}/{session_id}/tool_calls.jsonl 文件。不阻塞主链路。

日志格式（每行一条 JSON）：
{
  "ts": "2026-07-03T06:49:07Z",       # 简短时间戳
  "tool": "vision_screenshot",         # 工具名（短名优先）
  "input": {"mode": "viewport"},       # 工具参数
  "output": "...",                     # 工具输出（智能截断，见 _format_output）
  "error": null,                       # 异常信息
  "ms": 656,                           # 耗时（毫秒）
  "screenshot": "screenshots/...",     # 截图文件路径（截图工具专用）
  "verdict": {"pass": true, ...}       # Vision 判断结果（截图工具专用）
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor

logger = logging.getLogger("harness.observability.logger")

# 输出过于冗长的工具名——对这些工具提取关键摘要而非存储全文
_VERBOSE_TOOLS = frozenset({
    "list_properties",
    "describe_toolset",
})


class ToolCallLogger(ToolCallInterceptor):
    """以装饰器模式包裹 call_tool，记录所有工具调用到 JSONL 日志文件。

    日志写入使用 asyncio.create_task，不增加 tool call 的感知延迟。
    写入失败记 error 日志但不抛异常——日志不应阻断主业务。
    """

    def __init__(
        self,
        log_dir: Path,
        session_id: str = "",
        get_verdict: Callable[[], dict | None] | None = None,
        get_screenshot_path: Callable[[], str | None] | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._session_id = session_id
        self._session_dir: Path | None = None
        self._log_path: Path | None = None
        self._write_queue: asyncio.Queue[str] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._get_verdict = get_verdict or (lambda: None)
        self._get_screenshot_path = get_screenshot_path or (lambda: None)

    # ---- 生命周期 ----

    async def start(self) -> None:
        """初始化日志目录和后台写入协程。

        日志写入 {log_dir}/{session_id}/tool_calls.jsonl（session 同名子目录内），
        与 SnapshotRecorder（截图）共享同一目录。
        """
        session_id = self._session_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._session_dir = self._log_dir / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._session_dir / "tool_calls.jsonl"
        self._writer_task = asyncio.create_task(self._background_writer())
        logger.info("日志文件: %s", self._log_path)

    async def stop(self) -> None:
        """等待队列排空，关闭后台写入。"""
        if self._writer_task is None:
            return
        await self._write_queue.put(None)
        try:
            await asyncio.wait_for(self._writer_task, timeout=5.0)
        except asyncio.TimeoutError:
            self._writer_task.cancel()
        self._writer_task = None

    async def _background_writer(self) -> None:
        """后台协程：从队列取 JSON 行，批量写入文件。"""
        if self._log_path is None:
            return
        with open(self._log_path, "a", encoding="utf-8") as f:
            while True:
                line = await self._write_queue.get()
                if line is None:
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

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """将一次完成的工具调用序列化为 JSON 行并加入写入队列。

        对冗长工具（list_properties 等）做智能摘要，对截图工具注入
        screenshot 路径和 Vision 判断结果。
        """
        short = _short_name(event.name)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": short,
            "input": _serialize_args(event.args),
            "output": _format_output(short, event.parsed_text),
            "error": str(event.error) if event.error else None,
            "ms": round(event.duration_ms, 1),
        }

        # 截图工具附加字段
        if _is_screenshot_tool(short):
            entry["screenshot"] = self._get_screenshot_path()
            verdict = self._get_verdict()
            if verdict:
                entry["verdict"] = {
                    "pass": verdict.get("pass"),
                    "reason": (verdict.get("reason", "") or "")[:1000],
                }

        try:
            line = json.dumps(entry, ensure_ascii=False)
            await self._write_queue.put(line)
        except Exception:
            logger.exception("序列化日志行失败: %s", event.name)


# ---- 辅助 ----

_SCREENSHOT_NAMES = frozenset({
    "vision_screenshot",
    "CaptureEditorImage", "CaptureAssetImage",
})


def _short_name(full: str) -> str:
    """ToolsetRegistry.EditorAppToolset.CaptureAssetImage → CaptureAssetImage"""
    return full.split(".")[-1] if "." in full else full


def _is_screenshot_tool(short_name: str) -> bool:
    return short_name in _SCREENSHOT_NAMES


def _format_output(short_name: str, text: str | None) -> str | None:
    """根据工具类型智能格式化输出。

    - list_properties / describe_toolset：提取结构摘要（属性名/计数），跳过完整 schema
    - 普通工具：保留全文，截断到 3000 字符
    """
    if not text:
        return None

    if short_name in _VERBOSE_TOOLS:
        return _summarize_verbose_output(text)

    # 普通工具：增加截断上限到 3000（之前 2000 不够）
    if len(text) <= 3000:
        return text
    return text[:3000] + f"...[truncated, {len(text)} total]"


def _summarize_verbose_output(text: str) -> str:
    """对 list_properties 等冗长工具输出提取可读摘要。

    list_properties 返回的 JSON schema 格式：
      {"returnValue": "{\"propName\": {\"type\": \"number\", ...}, ...}"}
    提取为：
      [list_properties: 42 fields] propName, propName2, ...

    get_properties 返回的值格式（不在此处理，保留全文）：
      {"returnValue": "{\"propName\": 123, ...}"}
    """
    # 尝试解析嵌套 JSON
    try:
        outer = json.loads(text)
        rv = outer.get("returnValue", text)
        if isinstance(rv, str):
            inner = json.loads(rv)
        else:
            inner = rv
    except (json.JSONDecodeError, TypeError):
        return _truncate(text, 500)

    if isinstance(inner, dict):
        keys = list(inner.keys())
        if len(keys) > 20:
            preview = ", ".join(keys[:20])
            return f"[{len(keys)} fields] {preview}..."
        return f"[{len(keys)} fields] {', '.join(keys)}"

    return _truncate(text, 500)


def _serialize_args(args: dict) -> dict:
    """截断参数中的大值，避免日志膨胀。字符串 > 500 字符则截断。"""
    if not args:
        return {}
    result = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 500:
            result[k] = v[:500] + "...[truncated]"
        else:
            result[k] = v
    return result


def _truncate(text: str, max_len: int = 2000) -> str:
    """截断超长文本到 max_len，末尾标记截断。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...[truncated, {len(text)} total]"
