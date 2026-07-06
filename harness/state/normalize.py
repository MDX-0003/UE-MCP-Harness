"""共享参数归一化 — 单一真相来源，将 UE MCP 工具的异构参数映射为统一表达。

涉及的 Issue / Bug：
  - P0-1: StateCache handler、session _format_write_description、branch_mark
          三处各自手写参数解析，对着不存在的参数模式写——修此一处，三处生效。

真实参数形状（来自 JSONL 验证）：
  - write 工具: {"actor": {"refPath": "/Game/.../ActorName"}} 或
               {"instance": {"refPath": "/Game/.../ActorName[.Component]"}}
  - set_properties: {"instance": {refPath}, "values": "JSON string or dict"}
  - 老测试仍用 {"actor": {"name": "ActorName"}} 格式 → 向下兼容
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("harness.state.normalize")


@dataclass
class NormalizedCall:
    """归一化后的工具调用参数。

    actor_name:   目标 Actor 名（如 "SpotLight_0"）——从 refPath 尾段提取。
    component_name: 如果有组件子路径，提取的组件名（如 "LightComponent0"）。
    payload:       操作内容——具体取决于工具类型（详见 _extract_payload）。
    raw_args:      保留原始 args，用于无法归一化的 fallback 场景。
    """

    actor_name: str = ""
    component_name: str = ""
    payload: dict = field(default_factory=dict)
    raw_args: dict = field(default_factory=dict)


# ---- 主入口 ----

def normalize_tool_args(short_name: str, args: dict) -> NormalizedCall:
    """从 UE 工具的原始参数中提取 actor 名和操作内容。

    处理三种目标标识格式（向下兼容）：
      1. refPath 尾段: {"actor": {"refPath": "/Game/.../SpotLight_0"}} → "SpotLight_0"
      2. name 字段:    {"actor": {"name": "SpotLight_0"}}             → "SpotLight_0"
      3. 字符串值:     {"actor": "SpotLight_0"}                       → "SpotLight_0"

    处理两种目标键名:
      - "actor"     (ActorTools / SceneTools)
      - "instance"  (ObjectTools)

    处理两种属性键名:
      - "values"    (ObjectTools.set_properties 实际使用)
      - "json"      (旧测试代码使用)
    """
    # 1. 提取目标描述符
    target = args.get("instance") or args.get("actor") or {}

    # 2. 从目标提取 refPath 或 name
    ref_path = ""
    if isinstance(target, dict):
        ref_path = target.get("refPath", "")
        # 向下兼容旧测试格式（有 name 无 refPath）
        if not ref_path:
            actor_name = target.get("name", "")
            return NormalizedCall(
                actor_name=actor_name,
                payload=_extract_payload(short_name, args),
                raw_args=args,
            )
    elif isinstance(target, str):
        actor_name = target
        return NormalizedCall(
            actor_name=actor_name,
            payload=_extract_payload(short_name, args),
            raw_args=args,
        )
    else:
        # target 为空或未知类型——可能是不需要 actor 目标的工具（load_level 等）
        return NormalizedCall(
            actor_name="",
            payload=_extract_payload(short_name, args),
            raw_args=args,
        )

    # 3. 从 refPath 提取 actor_name 和 component_name
    actor_name, component_name = _parse_ref_path(ref_path)

    return NormalizedCall(
        actor_name=actor_name,
        component_name=component_name,
        payload=_extract_payload(short_name, args),
        raw_args=args,
    )


# ---- 辅助函数 ----

def _parse_ref_path(ref_path: str) -> tuple[str, str]:
    """从 UE refPath 中提取 actor 名和组件名。

    refPath 格式示例：
      /Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0
        → ("SpotLight_0", "")
      /Game/NewWorld.NewWorld:PersistentLevel.SpotLight_0.LightComponent0
        → ("SpotLight_0", "LightComponent0")
      /Script/Engine.SpotLight
        → ("SpotLight", "")
      /Game/NewWorld.NewWorld:PersistentLevel.StaticMeshActor_7
        → ("StaticMeshActor_7", "")
    """
    if not ref_path:
        return "", ""

    # 取最后一个 ':' 之后的部分，或整个 refPath
    if ":" in ref_path:
        sub_path = ref_path.rsplit(":", 1)[-1]
    else:
        sub_path = ref_path

    # 按 '.' 分割，取尾段
    parts = sub_path.split(".")
    if not parts:
        return "", ""

    last = parts[-1]
    second_last = parts[-2] if len(parts) >= 2 else ""

    # 判断最后一个段是否是组件名（约定：包含 "Component" 或首字母大写且后缀不像是 Actor）
    # 安全策略：如果最后一段包含 "Component" 且有第二段，则第二段是 actor
    if "Component" in last and second_last:
        return second_last, last

    # 否则最后一个段就是 actor 名
    return last, ""


def _extract_payload(short_name: str, args: dict) -> dict:
    """按工具类型提取操作内容 payload。"""
    if short_name == "set_actor_transform":
        return {"xform": args.get("xform", {})}

    elif short_name == "set_properties":
        raw = args.get("values") or args.get("json") or {}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return dict(raw) if isinstance(raw, dict) else {}

    elif short_name == "set_label":
        return {"label": args.get("label", "")}

    elif short_name == "add_tag":
        return {"tag": args.get("tag", "")}

    elif short_name == "remove_tag":
        return {"tag": args.get("tag", "")}

    elif short_name == "add_component":
        return {"component_class": args.get("component_class", args.get("component", ""))}

    elif short_name == "remove_component":
        return {"component": args.get("component", "")}

    elif short_name in ("add_to_scene_from_class", "add_to_scene_from_asset"):
        return {
            "actor_type": args.get("actor_type", args.get("asset_path", "")),
            "label": args.get("label", ""),
        }

    elif short_name == "remove_from_scene":
        return {}

    elif short_name == "set_parent_component":
        return {"parent": args.get("parent_component", args.get("parent", ""))}

    elif short_name == "set_actor_folder":
        return {"folder": args.get("folder", args.get("folder_path", ""))}

    elif short_name in ("SelectActors",):
        actors = args.get("actors", args.get("names", []))
        return {"actors": actors if isinstance(actors, list) else []}

    elif short_name == "load_level":
        return {"path": args.get("path", args.get("level_path", ""))}

    elif short_name == "rename_folder":
        return {"old": args.get("old_name", ""), "new": args.get("new_name", "")}

    elif short_name == "delete_folder":
        return {"folder": args.get("folder", args.get("folder_path", ""))}

    # 未覆盖的工具——返回空 payload
    return {}


def extract_short_name(full_name: str) -> str:
    """从全限定工具名中提取短名（如 set_actor_transform）。"""
    return full_name.split(".")[-1] if "." in full_name else full_name
