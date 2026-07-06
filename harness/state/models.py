"""WorldState — UE 编辑器世界状态缓存模型 (Contract 2)

pydantic 模型，004 的 Tier 1 System Context 用其渲染状态快照，
008 的 StateCacheInterceptor 通过 L1 写穿透填充。

dirty 区分两粒度：
  - dirty_actors:   覆盖 handler 写入但未做 L2 读验证的 Actor
  - dirty_toolsets: 完全未被覆盖的 toolset（连"改了哪个 Actor"都不知道）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Vector3(BaseModel):
    """三维向量。"""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class ActorSnapshot(BaseModel):
    """单个 Actor 的缓存快照。"""
    name: str
    class_name: str | None = None        # 如 "PointLight", "DirectionalLight"
    transform: dict | None = None        # {"location": \{...}, "rotation": \{...}, "scale": \{...}}
    properties: dict = Field(default_factory=dict)  # {"LightColor": "(1,0.5,0.3)", ...}
    label: str | None = None
    tags: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    deleted: bool = False
    last_updated: datetime | None = None


class WorldState(BaseModel):
    """UE 编辑器世界状态的完整缓存快照。

    刷新策略（ADR 0004）：
      L1 写穿透 — 覆盖的 write tool 调用成功后即时更新
      L2 读验证 — 按需（当前阶段暂不强制）
      L3 全量刷新 — 仅在 Hard Boundary 事件触发
    """

    map_path: str = ""
    actors: dict[str, ActorSnapshot] = Field(default_factory=dict)
    selected_actors: list[str] = Field(default_factory=list)

    dirty_actors: set[str] = Field(default_factory=set)
    dirty_toolsets: set[str] = Field(default_factory=set)

    pie_running: bool | None = None       # None = 未知（无直接数据源）
    last_full_refresh: datetime | None = None
    last_vision_verdict: dict | None = None  # 007 验证闭环：最近一次视觉验证结果

    # Hard Boundary 指纹（ADR 0008）：上次 execute_hard_boundary() 获取的关卡指纹
    last_fingerprint: dict | None = None
    drift_detected: bool = False          # 上次 Hard Boundary 检测到漂移

    _needs_refresh: bool = False          # load_level 后标记，由 Hard Boundary 消费后清除
