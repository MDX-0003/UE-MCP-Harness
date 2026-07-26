"""可观测性 — 统计面板：harness stats 命令。

读取 JSONL 日志文件，输出工具调用统计：
  - 总调用数
  - 按工具分组：调用次数、平均耗时、最大耗时、错误率
  - 总体错误率
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.observability.stats")


def cmd_stats(log_dir: Path, session_id: str | None = None) -> int:
    """harness stats 命令入口。

    如果没有指定 session_id，选取 log_dir 下最新的 JSONL 文件。

    返回 0 表示成功，1 表示失败。
    """
    log_path = _find_log_file(log_dir, session_id)
    if log_path is None:
        print(f"未找到日志文件。日志目录: {log_dir}")
        return 1

    entries = _load_jsonl(log_path)
    if not entries:
        print(f"日志文件为空: {log_path}")
        return 0

    _print_stats(entries, log_path)
    return 0


# ---- 内部函数 ----

def _find_log_file(log_dir: Path, session_id: str | None = None) -> Path | None:
    """在 log_dir 中查找目标 JSONL 文件。

    如果给了 session_id，精确匹配文件名（允许前缀匹配 session_id）。
    否则返回最近的 .jsonl 文件。
    """
    if not log_dir.exists():
        return None

    # 优先搜索子目录中的 tool_calls.jsonl（当前日志格式），
    # 回退到顶层 *.jsonl（兼容旧格式）
    jsonl_files = sorted(log_dir.rglob("tool_calls.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonl_files:
        jsonl_files = sorted(log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return None

    if session_id:
        for f in jsonl_files:
            if f.stem.startswith(session_id):
                return f
        return None

    return jsonl_files[0]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，返回解析后的条目列表。跳过损坏的行。"""
    entries: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("跳过损坏的日志行: %s", line[:80])
    except OSError as e:
        logger.error("读取日志文件失败: %s — %s", path, e)
    return entries


def _print_stats(entries: list[dict[str, Any]], log_path: Path) -> None:
    """打印统计信息到 stdout。"""
    total = len(entries)
    errors = sum(1 for e in entries if e.get("error"))
    error_rate = (errors / total * 100) if total > 0 else 0.0

    # 按 tool_name 分组
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        name = e.get("tool", "unknown")
        groups[name].append(e)

    print(f"日志文件: {log_path}")
    print(f"总调用数: {total}")
    print(f"错误数:   {errors} ({error_rate:.1f}%)")
    print()
    print(f"{'工具名称':<45} {'调用数':>6} {'平均(ms)':>9} {'最大(ms)':>9} {'错误率':>8}")
    print("-" * 80)

    for name in sorted(groups.keys()):
        calls = groups[name]
        count = len(calls)
        errs = sum(1 for c in calls if c.get("error"))
        durations = [c.get("ms", 0.0) for c in calls]
        avg_ms = sum(durations) / len(durations) if durations else 0.0
        max_ms = max(durations) if durations else 0.0
        err_pct = (errs / count * 100) if count > 0 else 0.0
        print(f"{name:<45} {count:>6} {avg_ms:>8.1f} {max_ms:>8.1f} {err_pct:>7.1f}%")

    print("-" * 80)

    # 全量统计
    all_durations = [e.get("ms", 0.0) for e in entries]
    if all_durations:
        avg_all = sum(all_durations) / len(all_durations)
        max_all = max(all_durations)
        print(f"{'[全量]':<45} {total:>6} {avg_all:>8.1f} {max_all:>8.1f} {error_rate:>7.1f}%")
