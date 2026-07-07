"""State Cache Interceptor — ToolCallInterceptor 实现 (Contract 1 + 2)

L1 写穿透：拦截 write tool call 成功后，从参数和返回值中提取变更语义，
即时更新 WorldState 缓存。未经覆盖的 write tool 标记 dirty_toolsets。
"""

from __future__ import annotations

import logging
from typing import Callable, TYPE_CHECKING

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor
from harness.state.normalize import normalize_tool_args, _parse_ref_path  # P0-1: 共享参数归一化
from harness.verification.session import record_write  # Issue 015: 操作记录供 Vision context 注入

if TYPE_CHECKING:
    from harness.state.models import WorldState

logger = logging.getLogger("harness.state.interceptor")

# handler 函数签名: (cache: WorldState, event: ToolCallCompleted) -> None
CacheHandler = Callable[["WorldState", ToolCallCompleted], None]


class StateCacheInterceptor(ToolCallInterceptor):
    """拦截 write tool 调用，成功后即时更新 WorldState。

    仅实现 post_call（确认 UE 返回成功后才更新缓存，避免乐观更新的回滚问题）。
    """

    def __init__(self, cache: WorldState) -> None:
        self._cache = cache
        self._handlers: dict[str, CacheHandler] = _build_handlers()

    # ---- ToolCallInterceptor ----

    async def post_call(self, event: ToolCallCompleted) -> None:
        """仅当调用成功时更新缓存。"""
        if event.error is not None:
            return

        # 1. 精确全路径名匹配
        handler = self._handlers.get(event.name)

        # 2. 短名 fallback：提取工具名的最后一段做子串匹配
        if handler is None:
            short = event.name.split(".")[-1] if "." in event.name else event.name
            for full_name, h in self._handlers.items():
                if full_name.split(".")[-1] == short:
                    handler = h
                    logger.debug("短名 fallback 匹配: %s → %s", event.name, full_name)
                    break

        if handler is not None:
            try:
                handler(self._cache, event)
                logger.debug("缓存更新: %s", event.name)
            except Exception as e:
                logger.warning("缓存更新失败: %s — %s", event.name, e)
        elif _is_write_tool(event.name):
            # 未覆盖的 write tool → 标记 toolset dirty
            toolset = _extract_toolset(event.name)
            if toolset:
                self._cache.dirty_toolsets.add(toolset)
                logger.debug("未覆盖的 write tool: %s → dirty toolset: %s", event.name, toolset)


# ---- Handler 构建 ----

def _build_handlers() -> dict[str, CacheHandler]:
    """构建全限定工具名 → handler 函数的映射表。

    按 Contract 2 的 WRITE_TOOL_HANDLERS 路由表，将短名映射到
    UE 实际使用的全限定路径。
    """
    # 短名 → handler 函数
    handlers_by_short: dict[str, CacheHandler] = {
        "set_actor_transform":     _handle_set_transform,
        "set_properties":          _handle_set_properties,
        "add_to_scene_from_class": _handle_add_to_scene,
        "add_to_scene_from_asset": _handle_add_to_scene,
        "remove_from_scene":       _handle_remove_from_scene,
        "set_label":               _handle_set_label,
        "add_tag":                 _handle_add_tag,
        "remove_tag":              _handle_remove_tag,
        "add_component":           _handle_add_component,
        "remove_component":        _handle_remove_component,
        "set_parent_component":    _handle_set_parent_component,
        "SelectActors":            _handle_select_actors,
        "load_level":              _handle_load_level,
        "set_actor_folder":        _handle_set_folder,
        "rename_folder":           _handle_rename_folder,
        "delete_folder":           _handle_delete_folder,
        "get_class":               _handle_get_class,   # Step 4: 读结果回填
    }

    # 拉长名 → handler：为每个短名生成全限定路径映射
    # C++ 工具: ToolsetRegistry.{ToolsetName}.{ToolName}
    # Python 工具: toolset_registry.toolsets.core.{module}.{ToolsetName}.{tool_name}
    full_handlers: dict[str, CacheHandler] = {}
    for short_name, handler in handlers_by_short.items():
        # C++ 后缀
        full_handlers[f"ToolsetRegistry.EditorAppToolset.{short_name}"] = handler
        # Python 后缀——覆盖多个 toolset
        for module in ("scene", "actor", "object"):
            for toolset_suffix in ("SceneTools", "ActorTools", "ObjectTools"):
                full_handlers[
                    f"toolset_registry.toolsets.core.{module}.{toolset_suffix}.{short_name}"
                ] = handler

    return full_handlers


# ---- 各 Handler 实现 ----

def _handle_set_transform(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("set_actor_transform", event.args)
    if nc.actor_name:
        if nc.actor_name not in cache.actors:
            cache.actors[nc.actor_name] = _new_snapshot(nc.actor_name)
        cache.actors[nc.actor_name].transform = nc.payload.get("xform", {})
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
    # Issue 015: 记录到 Recent Writes Buffer
    record_write("set_actor_transform", event.args)


def _handle_set_properties(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("set_properties", event.args)
    if nc.actor_name:
        if nc.actor_name not in cache.actors:
            cache.actors[nc.actor_name] = _new_snapshot(nc.actor_name)
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
        if nc.payload:
            cache.actors[nc.actor_name].properties.update(nc.payload)
    record_write("set_properties", event.args)


def _handle_add_to_scene(cache: WorldState, event: ToolCallCompleted) -> None:
    # P0-1: 归一化参数——从 refPath / instance 提取 actor 名
    nc = normalize_tool_args("add_to_scene_from_class", event.args)
    # 尝试从返回值中提取新 Actor 名（新建 Actor 无 refPath，归一化也拿不到名字）
    text = event.parsed_text or ""
    actor_name = _extract_actor_from_result(text) or nc.actor_name
    if actor_name:
        snap = _new_snapshot(actor_name)
        # Step 3: 从 add_to_scene 参数中取 class/asset 名填入
        actor_type = nc.payload.get("actor_type", "")
        if actor_type:
            # add_to_scene_from_asset 时 actor_type 是 asset path（如 /Game/Assets/SM_Chair），
            # 取尾段作为近似类名；add_to_scene_from_class 时就是直接类名（如 "PointLight"）
            snap.class_name = actor_type.rsplit("/", 1)[-1] if "/" in actor_type else actor_type
        cache.actors[actor_name] = snap
        cache.dirty_actors.add(actor_name)
        _touch_actor(cache, actor_name)
    # 区分 class 和 asset 来源
    write_name = "add_to_scene_from_class"
    record_write(write_name, event.args)


def _handle_remove_from_scene(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("remove_from_scene", event.args)
    actor_name = nc.actor_name or event.args.get("name", "")
    if actor_name and actor_name in cache.actors:
        cache.actors[actor_name].deleted = True
        cache.dirty_actors.add(actor_name)
        _touch_actor(cache, actor_name)
    record_write("remove_from_scene", event.args)


def _handle_set_label(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("set_label", event.args)
    if nc.actor_name:
        if nc.actor_name not in cache.actors:
            cache.actors[nc.actor_name] = _new_snapshot(nc.actor_name)
        cache.actors[nc.actor_name].label = nc.payload.get("label", "")
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
    record_write("set_label", event.args)


def _handle_add_tag(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("add_tag", event.args)
    tag = nc.payload.get("tag", "")
    if nc.actor_name and tag:
        if nc.actor_name not in cache.actors:
            cache.actors[nc.actor_name] = _new_snapshot(nc.actor_name)
        if tag not in cache.actors[nc.actor_name].tags:
            cache.actors[nc.actor_name].tags.append(tag)
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
    record_write("add_tag", event.args)


def _handle_remove_tag(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("remove_tag", event.args)
    tag = nc.payload.get("tag", "")
    if nc.actor_name and tag and nc.actor_name in cache.actors:
        try:
            cache.actors[nc.actor_name].tags.remove(tag)
        except ValueError:
            pass
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
    record_write("remove_tag", event.args)


def _handle_add_component(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("add_component", event.args)
    comp_name = nc.payload.get("component_class", "")
    if nc.actor_name:
        if nc.actor_name not in cache.actors:
            cache.actors[nc.actor_name] = _new_snapshot(nc.actor_name)
        if comp_name:
            cache.actors[nc.actor_name].components.append(comp_name)
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)


def _handle_remove_component(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("remove_component", event.args)
    comp_name = nc.payload.get("component", "")
    if nc.actor_name:
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)
        if comp_name and nc.actor_name in cache.actors:
            try:
                cache.actors[nc.actor_name].components.remove(comp_name)
            except ValueError:
                pass


def _handle_set_parent_component(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("set_parent_component", event.args)
    if nc.actor_name and nc.actor_name in cache.actors:
        cache.dirty_actors.add(nc.actor_name)
        _touch_actor(cache, nc.actor_name)


def _handle_select_actors(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("SelectActors", event.args)
    actors = nc.payload.get("actors", [])
    if isinstance(actors, list):
        cache.selected_actors = actors


def _handle_load_level(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("load_level", event.args)
    cache.actors.clear()
    cache.dirty_actors.clear()
    cache.dirty_toolsets.clear()
    cache.selected_actors.clear()
    cache.map_path = nc.payload.get("path", "")
    cache._needs_refresh = True  # 标记需 L3 刷新，由 server.py call_tool 的 post 阶段触发


def _handle_set_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    nc = normalize_tool_args("set_actor_folder", event.args)
    if nc.actor_name:
        cache.dirty_actors.add(nc.actor_name)
        if nc.actor_name in cache.actors:
            _touch_actor(cache, nc.actor_name)


def _handle_rename_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    cache.dirty_toolsets.add("SceneTools")


def _handle_delete_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    cache.dirty_toolsets.add("SceneTools")


def _handle_get_class(cache: WorldState, event: ToolCallCompleted) -> None:
    """Step 4: LLM 调 get_class 后，从返回值回填 class_name。

    输入:  {"instance": {"refPath": "/Game/.../SpotLight_0"}}
    输出:  {"returnValue": {"refPath": "/Script/Engine.SpotLight"}}
    → 提取尾段 "SpotLight" → 写入对应快照。
    """
    nc = normalize_tool_args("get_class", event.args)
    if not nc.actor_name:
        return

    text = event.parsed_text or ""
    import json
    try:
        data = json.loads(text) if isinstance(text, str) else text
        if isinstance(data, dict):
            rv = data.get("returnValue", {})
            if isinstance(rv, dict):
                class_ref = rv.get("refPath", "")
                if class_ref and nc.actor_name in cache.actors:
                    class_name, _ = _parse_ref_path(class_ref)
                    if class_name:
                        cache.actors[nc.actor_name].class_name = class_name
                        logger.debug("get_class 回填: %s → %s", nc.actor_name, class_name)
    except (json.JSONDecodeError, KeyError, TypeError):
        pass


# ---- 辅助 ----

def _new_snapshot(name: str) -> "ActorSnapshot":
    from harness.state.models import ActorSnapshot
    return ActorSnapshot(name=name)


def _touch_actor(cache: WorldState, actor_name: str) -> None:
    """更新 Actor 的 last_updated 时间戳，使 recency 排序生效 (P0-2 前置)。"""
    from datetime import datetime, timezone
    if actor_name in cache.actors:
        cache.actors[actor_name].last_updated = datetime.now(timezone.utc)


def _extract_actor_from_result(text: str) -> str:
    """从工具返回的文本中尝试提取 Actor 名。"""
    import re
    # 常见格式: "Created actor: Light_0", "Added Actor: Light_0"
    m = re.search(r'(?:Created|Added)\s+(?:actor|Actor)[:\s]+(\S+)', text)
    if m:
        return m.group(1)
    return ""


def _extract_toolset(full_name: str) -> str | None:
    """从全限定工具名中提取 toolset 标识。"""
    # C++: ToolsetRegistry.EditorAppToolset.GetSelectedActors → EditorAppToolset
    # Python: toolset_registry.toolsets.core.scene.SceneTools.find_actors → SceneTools
    parts = full_name.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return None


def _is_write_tool(tool_name: str) -> bool:
    """判断工具名是否可能是写操作（启发式）。"""
    write_keywords = ("set_", "add_", "remove_", "delete_", "create_",
                      "rename_", "load_", "save_", "import_", "export_",
                      "Select", "SelectActors")
    short = tool_name.split(".")[-1] if "." in tool_name else tool_name
    return any(short.startswith(kw) for kw in write_keywords)
