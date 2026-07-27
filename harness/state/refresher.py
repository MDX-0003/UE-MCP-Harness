"""State Cache L3 全量刷新 — Hard Boundary 事件驱动的全量状态快照。

触发条件（ADR 0004）：
  1. Harness 首次连接 UE
  2. load_level() 调用
  3. Harness 与 UE 重连
  4. LLM 显式 cache_refresh
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from harness.state.normalize import ref_parse_actor_names

if TYPE_CHECKING:
    from harness.client import McpClientSession
    from harness.state.models import WorldState

logger = logging.getLogger("harness.state.refresher")


async def state_full_refresh(
    ue_client: McpClientSession,
    cache: WorldState,
) -> None:
    """执行 L3 全量刷新——重建 State Cache。

    调用 find_actors(glob='*') 获取所有 Actor 列表，
    不逐个查询 Actor 属性（性能考虑，由 L1/L2 按需填充）。
    """
    logger.info("L3 全量刷新开始...")

    # 1. 获取当前地图路径
    try:
        result = await ue_client.call_tool(
            "toolset_registry.toolsets.core.scene.SceneTools.get_current_level",
            {},
        )
        cache.map_path = _extract_level_path(result)
    except Exception as e:
        logger.warning("获取地图路径失败: %s", e)

    # 2. 全量 Actor 扫描
    try:
        result = await ue_client.call_tool(
            "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
            {"glob": "*"},
        )
        actor_names = ref_parse_actor_names(result)
        for name in actor_names:
            if name not in cache.actors:
                from harness.state.models import ActorSnapshot
                from harness.state.normalize import infer_class_name
                snap = ActorSnapshot(name=name)
                snap.class_name = infer_class_name(name)  # Step 2: 名称推断填入
                cache.actors[name] = snap
        logger.info("L3 刷新: 发现 %d 个 Actor", len(actor_names))
    except Exception as e:
        logger.warning("全量 Actor 扫描失败: %s", e)

    # 3. 获取选中 Actor
    try:
        result = await ue_client.call_tool(
            "ToolsetRegistry.EditorAppToolset.GetSelectedActors",
            {},
        )
        cache.selected_actors = ref_parse_actor_names(result)
    except Exception as e:
        logger.warning("获取选中 Actor 失败: %s", e)

    # 4. 清除 dirty 标记
    cache.dirty_actors.clear()
    cache.dirty_toolsets.clear()

    from datetime import datetime, timezone
    cache.last_state_full_refresh = datetime.now(timezone.utc)

    actor_count = sum(1 for a in cache.actors.values() if not a.deleted)
    logger.info("L3 刷新完成: 地图=%s, Actor=%d, 选中=%d",
                 cache.map_path, actor_count, len(cache.selected_actors))


def _extract_level_path(result: str) -> str:
    """从 get_current_level 结果中提取地图路径。

    Python toolset 返回格式: content[0].text = '{"returnValue":"/Game/MyMap"}' （有 returnValue 包装）
    或直接返回纯文本路径。
    """
    import json
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        text = ""
        if isinstance(parsed, dict):
            content = parsed.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "").strip()
                    break

        # 尝试解析 returnValue 包装
        if text:
            try:
                wrapper = json.loads(text)
                if isinstance(wrapper, dict) and "returnValue" in wrapper:
                    return wrapper["returnValue"].strip().strip('"')
            except json.JSONDecodeError:
                pass
            return text.strip().strip('"')
    except json.JSONDecodeError:
        pass
    return ""
