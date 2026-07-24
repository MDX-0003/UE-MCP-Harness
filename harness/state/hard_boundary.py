"""Hard Boundary — 世界状态信任边界管理。

触发条件（ADR 0004 + ADR 0008）：
  1. Harness 首次连接 UE
  2. load_level() 调用
  3. Harness 与 UE 重连
  4. LLM 显式请求 cache_refresh
  5. 关卡指纹失配（LevelPersistenceToolset 提供）

调用 execute_hard_boundary() 执行统一流程：
  L3 全量刷新 → 指纹比对 → dirty-diff 漂移检测 → 记录时间
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from harness.client import mcp_extract_text

if TYPE_CHECKING:
    from harness.client import McpClientSession
    from harness.state.models import WorldState

logger = logging.getLogger("harness.state.hard_boundary")

# LevelPersistenceToolset 工具名常数
_LEVEL_SAVE = "LevelPersistenceToolset.LevelPersistenceToolset"
_GET_FINGERPRINT = f"{_LEVEL_SAVE}.GetLevelFingerprint"
_LIST_DIRTY = f"{_LEVEL_SAVE}.ListDirtyPackages"

# Harness 自身引发的 dirty 包记录（session 级别）
_harness_dirty_packages: set[str] = set()


@dataclass
class HardBoundaryResult:
    """execute_hard_boundary() 的返回值。"""

    reason: str                                          # "startup" | "reconnect" | "load_level" | "fingerprint_mismatch"
    refreshed: bool = False                              # L3 刷新是否成功执行
    actor_count: int = 0
    map_path: str = ""

    # 指纹相关
    fingerprint_match: bool | None = None                # None = 指纹不可用(未安装/调用失败)
    fingerprint: dict | None = None                      # 当前指纹（JSON dict）
    expected_fingerprint: dict | None = None              # 传入的预期指纹

    # 漂移相关
    drift_detected: bool = False
    external_dirty: list[str] = field(default_factory=list)  # Harness 外产生的脏包路径


def record_harness_dirty(packages: list[str]) -> None:
    """记录 Harness 自身引发的脏包（在 write tool post_call 后调用）。

    用于 dirty-diff：Hard Boundary 时排除这些已知脏包，
    剩余的即为 Harness 外改动。
    """
    _harness_dirty_packages.update(packages)


async def execute_hard_boundary(
    ue_client: McpClientSession,
    cache: WorldState,
    *,
    reason: str,
    expected_fingerprint: dict | None = None,
) -> HardBoundaryResult:
    """执行 Hard Boundary 完整流程。

    1. L3 全量刷新 → 重建 State Cache
    2. 指纹比对（LevelPersistenceToolset 已安装时）
    3. dirty-diff 漂移检测（过滤 /Script/ 噪声包）
    4. 填充 WorldState 的 last_full_refresh 时间戳

    Args:
        ue_client: 已连接的 UE MCP 客户端。
        cache: 共享的 WorldState 实例。
        reason: 触发原因标签（用于日志和上下文注入）。
        expected_fingerprint: 预期的关卡指纹（来自上次 Hard Boundary）。
    """

    result = HardBoundaryResult(reason=reason)
    error_occurred = False

    # ── 1. L3 全量刷新 ──
    from harness.state.refresher import full_refresh

    try:
        await full_refresh(ue_client, cache)
        result.refreshed = True
    except Exception as e:
        logger.warning("Hard Boundary L3 刷新失败: %s", e)
        error_occurred = True

    result.actor_count = sum(1 for a in cache.actors.values() if not a.deleted)
    result.map_path = cache.map_path

    # ── 2. 指纹比对 ──
    current = await _get_current_fingerprint(ue_client)
    if current is not None:
        result.fingerprint = current
        result.expected_fingerprint = expected_fingerprint

        if expected_fingerprint is not None:
            match = (
                current.get("packageGuid") == expected_fingerprint.get("packageGuid")
                and current.get("actorCount") == expected_fingerprint.get("actorCount")
                and current.get("actorNameHash") == expected_fingerprint.get("actorNameHash")
            )
            result.fingerprint_match = match
            if not match:
                result.drift_detected = True
                logger.warning(
                    "Hard Boundary 指纹失配: packageGuid %s→%s, actorCount %s→%s, actorNameHash %s→%s",
                    expected_fingerprint.get("packageGuid"), current.get("packageGuid"),
                    expected_fingerprint.get("actorCount"), current.get("actorCount"),
                    expected_fingerprint.get("actorNameHash"), current.get("actorNameHash"),
                )
        else:
            result.fingerprint_match = None  # 首次 Hard Boundary，无预期指纹
            logger.info("Hard Boundary 首次指纹基线: %s", json.dumps(current))
    else:
        logger.debug("LevelPersistenceToolset 指纹不可用，跳过指纹比对。")

    # ── 3. dirty-diff 漂移检测 ──
    try:
        dirty = await _get_dirty_packages(ue_client)
        filtered = {p for p in dirty if not p.startswith("/Script/")}
        external = filtered - _harness_dirty_packages

        if external:
            result.drift_detected = True
            result.external_dirty = sorted(external)
            logger.warning(
                "Hard Boundary dirty-diff 检测到 Harness 外改动 %d 个包: %s",
                len(external), result.external_dirty[:5],
            )
    except Exception as e:
        logger.debug("dirty-diff 不可用: %s", e)

    # ── 4. 记录时间 ──
    cache.last_full_refresh = datetime.now(timezone.utc)
    cache._needs_refresh = False

    if result.fingerprint_match is True:
        fp_label = "match"
    elif result.fingerprint_match is False:
        fp_label = "mismatch"
    elif result.fingerprint is not None:
        fp_label = "first"    # 已获取指纹，但无预期指纹可对比
    else:
        fp_label = "unavailable"

    log_msg = (
        f"Hard Boundary [{reason}]: refresh={'OK' if result.refreshed else 'FAIL'}, "
        f"map={result.map_path}, actors={result.actor_count}, "
        f"fingerprint={fp_label}, "
        f"drift={result.drift_detected}"
    )
    if error_occurred:
        logger.warning(log_msg)
    else:
        logger.info(log_msg)

    return result


async def _get_current_fingerprint(ue_client: McpClientSession) -> dict | None:
    """调用 GetLevelFingerprint 获取当前关卡指纹。

    失败或工具不可用时返回 None。
    """
    try:
        raw = await ue_client.call_tool(_GET_FINGERPRINT, {"LevelPath": ""})
        text = mcp_extract_text(raw)
        # text = '{"returnValue":"{\\"packagePath\\":\\"...\\"}"}'
        wrapper = json.loads(text)
        inner_str = wrapper.get("returnValue", text)
        return json.loads(inner_str) if isinstance(inner_str, str) else inner_str
    except Exception as e:
        logger.debug("GetLevelFingerprint 调用失败: %s", e)
        return None


async def _get_dirty_packages(ue_client: McpClientSession) -> list[str]:
    """调用 ListDirtyPackages 获取脏包列表。"""
    try:
        raw = await ue_client.call_tool(_LIST_DIRTY, {})
        text = mcp_extract_text(raw)
        # text = '{"returnValue":"[\\"/Script/...\\"]"}'
        wrapper = json.loads(text)
        inner = wrapper.get("returnValue", text)
        data = json.loads(inner) if isinstance(inner, str) else inner
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("ListDirtyPackages 调用失败: %s", e)
        return []
