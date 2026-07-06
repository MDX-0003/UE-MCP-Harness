"""可观测性 — 回放引擎：harness replay 命令。

读取 JSONL 日志文件，连接运行中的 UE，按顺序重放每个 tool call。

特性：
  - 回放模式下跳过验证步骤（screenshot 标记为不可重现）
  - 工具调用失败时输出失败的 step 编号和错误，不继续执行
  - 需要运行中的 UE MCP Server
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from harness.config import Config
from harness.client import McpClientSession

logger = logging.getLogger("harness.observability.replay")


def cmd_replay(log_file: Path, ue_port: int = 8000) -> int:
    """harness replay 命令入口。

    Args:
        log_file: JSONL 日志文件路径。
        ue_port: UE MCP Server 端口。

    返回 0 成功，1 失败。
    """
    if not log_file.is_file():
        print(f"日志文件不存在: {log_file}")
        return 1

    entries = _load_jsonl(log_file)
    if not entries:
        print(f"日志文件为空: {log_file}")
        return 0

    # 加载完整配置（环境变量），再覆盖 ue_port
    try:
        config = Config.from_env()
        config.ue_port = ue_port  # type: ignore[assignment]
    except Exception:
        config = Config(ue_port=ue_port)

    # Harness 原生工具——不在 UE 侧，无法回放，跳过
    _HARNESS_TOOLS = frozenset({
        "activate_skill", "deactivate_skill", "get_context", "save_skill",
        "vision_screenshot", "vision_ask", "vision_tell", "vision_reset", "vision_status",
    })

    async def run() -> int:
        client = McpClientSession(config)
        try:
            await client.connect()
            logger.info("已连接 UE MCP Server，开始回放 %d 个步骤...", len(entries))
            skipped = 0

            for i, entry in enumerate(entries, start=1):
                # 兼容新旧两种 JSONL 格式
                tool_name = entry.get("tool_name") or entry.get("tool", "")
                tool_input = entry.get("tool_input") or entry.get("input", {})

                # 跳过 Harness 原生工具
                short = tool_name.split(".")[-1] if "." in tool_name else tool_name
                if short in _HARNESS_TOOLS:
                    logger.debug("[%d/%d] 跳过 Harness 工具: %s", i, len(entries), tool_name)
                    skipped += 1
                    continue

                logger.info("[%d/%d] 回放: %s", i, len(entries), tool_name)

                try:
                    result = await client.call_tool(tool_name, tool_input)
                    logger.debug("[%d/%d] 成功: %s → %s", i, len(entries), tool_name,
                                 result[:120] if result else "(空)")
                except Exception as e:
                    logger.error("[%d/%d] 回放失败: %s → %s", i, len(entries), tool_name, e)
                    print(f"\n回放在步骤 {i}/{len(entries)} 处失败")
                    print(f"  工具: {tool_name}")
                    print(f"  参数: {json.dumps(tool_input, ensure_ascii=False)[:200]}")
                    print(f"  错误: {e}")
                    return 1

            logger.info("回放完成，%d 个步骤全部成功（跳过 %d 个 Harness 工具）。",
                        len(entries) - skipped, skipped)
            print(f"回放完成: {len(entries) - skipped} 个步骤成功"
                  + (f"（跳过 {skipped} 个 Harness 工具）" if skipped else ""))
            return 0

        except Exception as e:
            logger.error("回放引擎致命错误: %s", e)
            return 1
        finally:
            await client.close()

    return asyncio.run(run())


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，返回解析后的条目列表。"""
    entries: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("跳过损坏的日志行: %s", line[:80])
    return entries
