"""State Cache Interceptor — ToolCallInterceptor 实现 (Contract 1 + 2)

L1 写穿透：拦截 write tool call 成功后，从参数和返回值中提取变更语义，
即时更新 WorldState 缓存。未经覆盖的 write tool 标记 dirty_toolsets。
"""

from __future__ import annotations

import logging
from typing import Callable, TYPE_CHECKING

from harness.interceptor import ToolCallCompleted, ToolCallInterceptor
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
    actor_name = event.args.get("actor", {}).get("name", "")
    if actor_name:
        if actor_name not in cache.actors:
            cache.actors[actor_name] = _new_snapshot(actor_name)
        cache.actors[actor_name].transform = event.args.get("xform", {})
        cache.dirty_actors.add(actor_name)
    # Issue 015: 记录到 Recent Writes Buffer
    record_write("set_actor_transform", event.args)


def _handle_set_properties(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    json_str = event.args.get("json", "{}")
    if actor_name:
        if actor_name not in cache.actors:
            cache.actors[actor_name] = _new_snapshot(actor_name)
        cache.dirty_actors.add(actor_name)
        import json
        try:
            props = json.loads(json_str) if isinstance(json_str, str) else json_str
            cache.actors[actor_name].properties.update(props)
        except (json.JSONDecodeError, TypeError):
            pass
    record_write("set_properties", event.args)


def _handle_add_to_scene(cache: WorldState, event: ToolCallCompleted) -> None:
    # 尝试从返回值中提取新 Actor 名
    text = event.parsed_text or ""
    actor_name = _extract_actor_from_result(text)
    if not actor_name:
        actor_name = event.args.get("actor_name", event.args.get("name", ""))
    if actor_name:
        cache.actors[actor_name] = _new_snapshot(actor_name)
        cache.dirty_actors.add(actor_name)
# 区分 class 和 asset 来源
    write_name = "add_to_scene_from_class"
    record_write(write_name, event.args)


def _handle_remove_from_scene(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", event.args.get("name", ""))
    if actor_name and actor_name in cache.actors:
        cache.actors[actor_name].deleted = True
        cache.dirty_actors.add(actor_name)
    record_write("remove_from_scene", event.args)


def _handle_set_label(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    label = event.args.get("label", "")
    if actor_name:
        if actor_name not in cache.actors:
            cache.actors[actor_name] = _new_snapshot(actor_name)
        cache.actors[actor_name].label = label
        cache.dirty_actors.add(actor_name)
    record_write("set_label", event.args)


def _handle_add_tag(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    tag = event.args.get("tag", "")
    if actor_name and tag:
        if actor_name not in cache.actors:
            cache.actors[actor_name] = _new_snapshot(actor_name)
        if tag not in cache.actors[actor_name].tags:
            cache.actors[actor_name].tags.append(tag)
        cache.dirty_actors.add(actor_name)
    record_write("add_tag", event.args)


def _handle_remove_tag(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    tag = event.args.get("tag", "")
    if actor_name and tag and actor_name in cache.actors:
        try:
            cache.actors[actor_name].tags.remove(tag)
        except ValueError:
            pass
        cache.dirty_actors.add(actor_name)
    record_write("remove_tag", event.args)


def _handle_add_component(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    comp_name = event.args.get("component_class", event.args.get("component", ""))
    if actor_name:
        if actor_name not in cache.actors:
            cache.actors[actor_name] = _new_snapshot(actor_name)
        if comp_name:
            cache.actors[actor_name].components.append(comp_name)
        cache.dirty_actors.add(actor_name)


def _handle_remove_component(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    comp_name = event.args.get("component", "")
    if actor_name:
        cache.dirty_actors.add(actor_name)
        if comp_name and actor_name in cache.actors:
            try:
                cache.actors[actor_name].components.remove(comp_name)
            except ValueError:
                pass


def _handle_set_parent_component(cache: WorldState, event: ToolCallCompleted) -> None:
    actor_name = event.args.get("actor", {}).get("name", "")
    if actor_name and actor_name in cache.actors:
        cache.dirty_actors.add(actor_name)


def _handle_select_actors(cache: WorldState, event: ToolCallCompleted) -> None:
    actors = event.args.get("actors", event.args.get("names", []))
    if isinstance(actors, list):
        cache.selected_actors = actors


def _handle_load_level(cache: WorldState, event: ToolCallCompleted) -> None:
    cache.actors.clear()
    cache.dirty_actors.clear()
    cache.dirty_toolsets.clear()
    cache.selected_actors.clear()
    map_path = event.args.get("path", event.args.get("level_path", ""))
    cache.map_path = map_path
    cache._needs_refresh = True  # 标记需 L3 刷新，由 server.py call_tool 的 post 阶段触发


def _handle_set_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    # SceneTools.set_actor_folder — 参数语义不完全可知，保守标记 dirty
    actor_name = event.args.get("actor", {}).get("name", "")
    if actor_name:
        cache.dirty_actors.add(actor_name)


def _handle_rename_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    cache.dirty_toolsets.add("SceneTools")


def _handle_delete_folder(cache: WorldState, event: ToolCallCompleted) -> None:
    cache.dirty_toolsets.add("SceneTools")


# ---- 辅助 ----

def _new_snapshot(name: str) -> "ActorSnapshot":
    from harness.state.models import ActorSnapshot
    return ActorSnapshot(name=name)


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
