"""Vision Session Manager — 统一管理 Vision Agent 的对话生命周期 (Issue 015)

取代 VisionInterceptor 的直接 VisionSubAgent 调用，提供：
  - 会话创建/追问/上下文注入/关闭/状态查询
  - 多截图绑定（同一 Session 可累积多张截图）
  - 自动上下文组装（dirty actors + recent writes + L2 read-back）
  - 上下文过滤与 token cap
  - Session 过期三级警告升级
  - Recent writes 内存滚动缓冲
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from harness.config import Config
from harness.verification.vision_agent import (
    VisionSubAgent, VisionVerdict,
    VISION_SYSTEM_PROMPT_DESCRIBE, VISION_SYSTEM_PROMPT_VERIFY,
)

if TYPE_CHECKING:
    from harness.state.models import WorldState

logger = logging.getLogger("harness.verification.session")


# ---- Context Block 类型 ----

class ContextSource(str, Enum):
    """上下文来源分类。"""
    DIRTY = "auto:dirty"       # WorldState.dirty_actors
    WRITE = "auto:write"       # 最近 write tool 调用
    L2 = "auto:l2"             # L2 读回结果
    MANUAL = "manual:tell"     # LLM 通过 vision_tell 手动注入


@dataclass
class ContextBlock:
    """一条上下文信息。"""
    source: ContextSource
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScreenshotRef:
    """Session 中的一张截图引用。"""
    b64: str
    meta: dict  # {width, height, mode, mime_type}
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class VisionSession:
    """一次 Vision 对话会话。

    绑定到一个任务/场景。可累积多张截图、多次提问。
    LLM 通过 vision_reset 显式关闭。
    """

    id: str
    created_at: datetime
    config: Config
    screenshots: list[ScreenshotRef] = field(default_factory=list)
    context_blocks: list[ContextBlock] = field(default_factory=list)
    question_log: list[dict] = field(default_factory=list)  # [{question, verdict, at}]
    _agent: VisionSubAgent | None = None
    last_active_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    question_count: int = 0
    is_active: bool = True

    @property
    def screenshot_count(self) -> int:
        return len(self.screenshots)

    @property
    def latest_screenshot(self) -> ScreenshotRef | None:
        return self.screenshots[-1] if self.screenshots else None

    def touch(self) -> None:
        """更新最后活跃时间。"""
        self.last_active_at = datetime.now(timezone.utc)
        self.question_count += 1


# ---- Recent Writes Buffer (模块级) ----

_recent_writes: deque[str] = deque(maxlen=10)


def record_write(short_name: str, args: dict) -> None:
    """记录一次 write tool 调用到滚动 buffer。

    由 StateCacheInterceptor 在每个 handler 成功后调用。
    """
    desc = _format_write_description(short_name, args)
    if desc:
        _recent_writes.append(desc)
        logger.debug("Recent write 已记录: %s", desc)


def get_recent_writes(limit: int = 3) -> list[str]:
    """获取最近 N 次 write 操作记录。"""
    items = list(_recent_writes)
    return items[-limit:] if len(items) > limit else items


def _format_write_description(short_name: str, args: dict) -> str:
    """将 write tool 的 args 格式化为简洁的人可读描述。

    P0-1 修复：使用 normalize_tool_args 统一参数解析，
    处理 actor/instance 别名 + refPath/name 兼容 + values/json 别名。
    """
    from harness.state.normalize import normalize_tool_args

    nc = normalize_tool_args(short_name, args)
    actor_name = nc.actor_name

    if short_name == "set_actor_transform":
        loc = nc.payload.get("xform", {}).get("location", {})
        if loc:
            return (
                f"set_actor_transform({actor_name or '?'}, "
                f"location=({loc.get('x',0):.0f},{loc.get('y',0):.0f},{loc.get('z',0):.0f}))"
            )
        return f"set_actor_transform({actor_name or '?'})"

    elif short_name == "set_properties":
        props = nc.payload
        if props:
            keys = list(props.keys())[:5]
            key_str = ", ".join(str(k) for k in keys)
            if len(props) > 5:
                key_str += f"...({len(props)} total)"
        else:
            key_str = "?"
        return f"set_properties({actor_name or '?'}, [{key_str}])"

    elif short_name in ("add_to_scene_from_class", "add_to_scene_from_asset"):
        actor_type = nc.payload.get("actor_type", "?")
        label = nc.payload.get("label", "")
        result = f"add_to_scene({actor_type}"
        if label:
            result += f', label="{label}"'
        result += ")"
        return result

    elif short_name == "remove_from_scene":
        return f"remove_from_scene({actor_name or '?'})"

    elif short_name == "set_label":
        label = nc.payload.get("label", "?")
        return f"set_label({actor_name or '?'}, \"{label}\")"

    elif short_name == "add_tag":
        tag = nc.payload.get("tag", "?")
        return f"add_tag({actor_name or '?'}, \"{tag}\")"

    elif short_name == "remove_tag":
        tag = nc.payload.get("tag", "?")
        return f"remove_tag({actor_name or '?'}, \"{tag}\")"

    elif short_name == "load_level":
        path = nc.payload.get("path", "?")
        return f"load_level({path})"

    elif short_name == "SelectActors":
        actors = nc.payload.get("actors", [])
        if isinstance(actors, list):
            return f"select_actors([{', '.join(str(a) for a in actors[:5])}])"
        return "select_actors(?)"

    # 未覆盖的 write tool
    return f"{short_name}(...)"


# ---- Session 过期警告 ----

def _check_session_warning(session: VisionSession) -> str | None:
    """检查 Session 是否触发过期警告。

    三级升级：L1 提醒(8min/5次) → L2 警告(15min/8次) → L3 严重(30min/15次)。
    返回警告文本或 None。详见 Issue 015 §6。
    """
    age_min = (datetime.now(timezone.utc) - session.created_at).total_seconds() / 60
    count = session.question_count

    if age_min > 30 or count > 15:
        return (
            f"🚨 [Vision Session 严重超时]\n"
            f"Session 已活跃 {age_min:.0f} 分钟，累计 {count} 次提问，"
            f"{session.screenshot_count} 张截图。\n"
            f"长时间 Session 累积高额 token 成本，且 Vision 上下文膨胀可能影响判断质量。\n"
            f"请立即调 vision_reset() 关闭此 Session。"
        )
    elif age_min > 15 or count > 8:
        return (
            f"⚠ [Vision Session 超时警告]\n"
            f"Session 已活跃 {age_min:.0f} 分钟，累计 {count} 次提问。\n"
            f"长时间 Session 累积 token 成本。建议调 vision_reset() 关闭。"
        )
    elif age_min > 8 or count > 5:
        return (
            f"💡 [Vision Session 提醒]\n"
            f"Session 已活跃 {age_min:.0f} 分钟。\n"
            f"如验证已完成，可调 vision_reset() 关闭以节省 token。"
        )
    return None


# ---- 自动上下文构建 ----

def build_scene_context(
    world_state: "WorldState | None",
    question: str = "",
    max_actors: int = 15,
) -> str:
    """从 WorldState 构建场景上下文文本，供 Vision prompt 注入。

    过滤优先级（Issue 015 §5.2）：
      P0: question 中提及的 Actor 名 → 全文提取
      P1: dirty_actors 全部提取
      P2: 最近 10 个 last_updated Actor

    Args:
        world_state: State Cache（可能为 None 或空）。
        question: LLM 的提问文本，用于 P0 匹配。
        max_actors: P2 最多提取的 Actor 数量。

    Returns:
        格式化的场景上下文字符串。空字符串表示无可用上下文。
    """
    if world_state is None:
        return ""

    # 收集所有活跃 Actor
    active_actors = {
        name: snap for name, snap in world_state.actors.items()
        if not snap.deleted
    }
    if not active_actors:
        return ""

    # P0: question 中提及的 Actor
    mentioned: set[str] = set()
    if question:
        q_lower = question.lower()
        for name in active_actors:
            if name.lower() in q_lower:
                mentioned.add(name)

    # P1: dirty actors
    dirty = set(world_state.dirty_actors) & set(active_actors.keys())

    # P2: recency — 按 last_updated 排序
    with_ts = [(n, s) for n, s in active_actors.items() if n not in mentioned and n not in dirty]
    with_ts.sort(
        key=lambda x: x[1].last_updated or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    recency = {n for n, _ in with_ts[:max_actors]}

    # 合并（按优先级排序）
    all_selected = list(mentioned) + list(dirty) + list(recency)
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for n in all_selected:
        if n not in seen and n in active_actors:
            seen.add(n)
            ordered.append(n)

    # 渲染
    parts: list[str] = []

    # 全局概览
    type_counts: dict[str, int] = {}
    for snap in active_actors.values():
        cls = snap.class_name or "Unknown"
        type_counts[cls] = type_counts.get(cls, 0) + 1
    type_summary = "、".join(
        f"{cls}×{cnt}" if cnt > 1 else cls
        for cls, cnt in sorted(type_counts.items())
    )
    parts.append(f"当前关卡：{world_state.map_path or '未知'}")
    parts.append(f"已知 Actor（共 {len(active_actors)} 个）：{type_summary}")

    # 详细列表
    if mentioned:
        parts.append("\n问题中提到的 Actor：")
        for name in mentioned:
            parts.append(_format_actor_detail(name, active_actors[name]))

    if dirty:
        parts.append("\n最近修改的 Actor（dirty）：")
        for name in dirty:
            if name in active_actors:
                parts.append(_format_actor_detail(name, active_actors[name]))

    # recency（只列名称，不展开详情以节省 token）
    if recency - mentioned - dirty:
        parts.append(
            f"\n其他已知 Actor："
            + ", ".join(sorted(recency - mentioned - dirty))
        )

    total_selected = len(mentioned) + len(dirty) + len(recency - mentioned - dirty)
    if total_selected < len(active_actors):
        parts.append(f"... 等共 {len(active_actors)} 个 Actor")

    # 脏数据警告
    if world_state.dirty_toolsets:
        parts.append(f"\n⚠ 未追踪的工具集：{', '.join(sorted(world_state.dirty_toolsets)[:5])}")

    return "场景上下文（来自 State Cache）：\n" + "\n".join(parts)


def _format_actor_detail(name: str, snap: Any) -> str:
    """格式化单个 Actor 的详情行。"""
    detail = f"  - {name}"
    if snap.class_name:
        detail += f" ({snap.class_name})"
    if snap.label and snap.label != name:
        detail += f' | label="{snap.label}"'
    if snap.transform:
        loc = snap.transform.get("location", {})
        if loc:
            detail += (
                f" | location=({loc.get('x',0):.0f},"
                f"{loc.get('y',0):.0f},{loc.get('z',0):.0f})"
            )
    if snap.tags:
        detail += f" | tags=[{','.join(snap.tags)}]"
    return detail


def build_full_prompt_context(
    world_state: "WorldState | None",
    session: VisionSession,
    question: str = "",
    max_tokens: int = 1000,
) -> str:
    """组装完整的 Vision prompt 上下文。

    包含：自动场景上下文 + recent writes + manual tell context。
    按 P0-P3 优先级过滤，超出 token cap 则截断低优先级内容。

    Args:
        world_state: State Cache。
        session: 当前 Vision Session。
        question: LLM 的提问。
        max_tokens: token 硬上限。

    Returns:
        组装好的上下文字符串，可直接嵌入 Vision user message。
    """
    blocks: list[tuple[int, str]] = []  # (priority, text)

    # P1: 场景上下文（dirty + mentioned actors）
    scene_ctx = build_scene_context(world_state, question)
    if scene_ctx:
        blocks.append((1, scene_ctx))

    # P1: Recent writes
    recent = get_recent_writes(limit=3)
    if recent:
        blocks.append((1, "最近执行的操作：\n" + "\n".join(f"  - {r}" for r in recent)))

    # P2: Manual tell context
    manual_blocks = [
        b for b in session.context_blocks
        if b.source == ContextSource.MANUAL
    ]
    if manual_blocks:
        blocks.append((2, "LLM 提供的上下文：\n" + "\n".join(
            f"  - {b.content}" for b in manual_blocks
        )))

    # P3: 上次验证结果
    if session.question_log:
        last = session.question_log[-1]
        last_v = last.get("verdict", {})
        if last_v:
            passed = "✅" if last_v.get("pass") else "❌"
            reason = last_v.get("reason", "")[:500]
            blocks.append((3, f"上次验证结果：{passed} {reason}"))

    if not blocks:
        return ""

    return _cap_context(blocks, max_tokens)


def _cap_context(blocks: list[tuple[int, str]], max_tokens: int = 1000) -> str:
    """按优先级组装并截断 context blocks。

    P1（critical）必须保留；P2+（optional）超出 token cap 时截断。
    中文约 1.5 char/token，英文约 4 char/token，混合取 3 作为保守估算。
    """
    max_chars = max_tokens * 3
    omit_marker_chars = 50  # 省略标注文本的字符数预留

    # 分离 critical (≤1) 和 optional (>1)
    critical_blocks = [(p, t) for p, t in blocks if p <= 1]
    optional_blocks = [(p, t) for p, t in blocks if p > 1]

    critical_text = "\n\n".join(t for _, t in critical_blocks)

    if not optional_blocks:
        if len(critical_text) <= max_chars:
            return critical_text
        # Critical 自身超了
        return critical_text[:max_chars - omit_marker_chars] + "\n\n... (context 超长，已省略部分内容)"

    optional_full = "\n\n".join(t for _, t in optional_blocks)

    if len(critical_text) + len(optional_full) <= max_chars:
        return critical_text + "\n\n" + optional_full

    # 需要截断。先判断 critical + marker 是否放得下
    marker_overhead = omit_marker_chars
    if len(critical_text) + marker_overhead >= max_chars:
        # critical 本身就接近或超过 cap → 截断 critical
        available = max_chars - marker_overhead
        if available <= 0:
            available = max_chars // 2
        return critical_text[:available] + "\n\n... (context 超长，已省略部分内容)"

    # critical 放得下，截断 optional
    remaining = max_chars - len(critical_text) - marker_overhead
    included: list[str] = []
    omitted_chars = 0
    for _, text in optional_blocks:
        if remaining >= len(text):
            included.append(text)
            remaining -= len(text)
        elif remaining > 40:
            included.append(text[:remaining])
            omitted_chars += len(text) - remaining
            remaining = 0
        else:
            omitted_chars += len(text)

    result = critical_text
    if included:
        result += "\n\n" + "\n\n".join(included)
    if omitted_chars > 0:
        result += f"\n\n... (省略约 {omitted_chars // 3} tokens：补充上下文等低优先级信息)"

    return result


# ---- Vision Session Manager ----

class VisionSessionManager:
    """管理 Vision 对话会话的生命周期。

    职责：会话 CRUD、上下文组装、警告检查、归档。
    VisionSubAgent 仅作为 API client 使用。
    """

    def __init__(
        self,
        config: Config,
        world_state: "WorldState | None" = None,
        log_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._world_state = world_state
        self._log_dir = log_dir
        self._active: VisionSession | None = None
        self._archive: list[VisionSession] = []

    def set_log_dir(self, log_dir: Path) -> None:
        """动态更新日志目录（Harness session 连接后调用）。"""
        self._log_dir = log_dir

    # ---- 会话生命周期 ----

    def start(self, question: str = "") -> VisionSession:
        """开始新的 Vision Session（自动关闭旧 Session）。"""
        if self._active and self._active.is_active:
            self._archive_old()
        session = VisionSession(
            id=str(uuid.uuid4())[:8],
            created_at=datetime.now(timezone.utc),
            config=self._config,
            _agent=VisionSubAgent(self._config),
        )
        session.touch()
        self._active = session
        logger.info("Vision Session 已创建: %s%s", session.id,
                     f' (question="{question[:60]}...")' if question else "")
        return session

    def reset(self) -> VisionSession:
        """关闭并归档当前 Session，返回新 Session。"""
        old = self._active
        if old:
            self._archive_old()
            logger.info("Vision Session %s 已关闭并归档", old.id)
        return self.start()

    def get_active(self) -> VisionSession | None:
        """获取当前活跃 Session。"""
        return self._active if self._active and self._active.is_active else None

    def _archive_old(self) -> None:
        """将当前活跃 Session 标记为非活跃并归档。"""
        if self._active is None:
            return
        self._active.is_active = False
        self._archive.append(self._active)

        # 写入归档文件
        if self._log_dir is not None:
            self._write_session_json(self._active)

        self._active = None

    def _write_session_json(self, session: VisionSession) -> None:
        """将 Session 摘要写入 JSON 文件。"""
        try:
            vs_dir = self._log_dir / "vision_sessions"
            vs_dir.mkdir(parents=True, exist_ok=True)
            file_path = vs_dir / f"{session.id}.json"

            summary = {
                "id": session.id,
                "created_at": session.created_at.isoformat(),
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "screenshot_count": session.screenshot_count,
                "question_count": session.question_count,
                "question_log": session.question_log,
                "context_sources": {
                    src.value: sum(
                        1 for b in session.context_blocks if b.source == src
                    )
                    for src in ContextSource
                },
            }
            file_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.debug("Vision Session 已归档: %s", file_path)
        except Exception as e:
            logger.warning("Vision Session 归档失败: %s", e)

    # ---- Vision 交互 ----

    async def add_screenshot(
        self,
        screenshot_b64: str,
        screenshot_meta: dict,
        question: str = "",
        scene_context: str = "",
    ) -> VisionVerdict:
        """添加截图到当前 Session 并可选进行首次提问。

        如无活跃 Session 则自动创建。
        """
        session = self.get_active()
        if session is None:
            session = self.start(question)
        elif session._agent is None:
            session._agent = VisionSubAgent(self._config)

        # 重置 VisionSubAgent 历史以绑定新截图
        # （同一 Session 内的 ask() 会复用历史）
        if session._agent.call_count > 0:
            session._agent.reset()

        session.touch()
        session.screenshots.append(ScreenshotRef(
            b64=screenshot_b64,
            meta=screenshot_meta,
        ))

        # 组装上下文
        ctx_text = scene_context or build_full_prompt_context(
            self._world_state, session, question,
        )

        # 调用 Vision API
        agent = session._agent
        if question:
            verdict = await agent.check(
                screenshot_b64,
                question=question,
                scene_context=ctx_text,
            )
        else:
            # 自由描述模式
            verdict = await agent.check(
                screenshot_b64,
                scene_context=ctx_text,
            )

        # 记录
        verdict_dict = {
            "pass": verdict.pass_,
            "reason": verdict.reason[:1000],
            "adjustment": verdict.adjustment,
            "question": question[:500] if question else "",
        }
        session.question_log.append({
            "question": question[:500] if question else "(描述模式)",
            "verdict": verdict_dict,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        session.last_verdict = verdict_dict

        return verdict

    async def ask(self, question: str) -> VisionVerdict:
        """在当前 Session 中追问（不截新图）。

        复用 Session 内所有截图的对话历史。
        Raises ValueError 如果无活跃 Session。
        """
        session = self.get_active()
        if session is None:
            raise ValueError(
                "没有活跃的 Vision Session。请先调 vision_screenshot 创建 Session。"
            )
        if session._agent is None or session._agent.call_count == 0:
            raise ValueError(
                "Vision Session 尚未进行任何分析。请先调 vision_screenshot。"
            )

        session.touch()

        # 组装上下文并追问
        ctx_text = build_full_prompt_context(
            self._world_state, session, question,
        )
        verdict = await session._agent.continue_with_question(
            question, scene_context=ctx_text,
        )

        verdict_dict = {
            "pass": verdict.pass_,
            "reason": verdict.reason[:1000],
            "adjustment": verdict.adjustment,
            "question": question[:500],
        }
        session.question_log.append({
            "question": question[:500],
            "verdict": verdict_dict,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        session.last_verdict = verdict_dict

        return verdict

    def tell(self, info: str) -> None:
        """向当前 Session 注入 LLM 提供的上下文（不调 API）。"""
        session = self.get_active()
        if session is None:
            logger.debug("vision_tell 调用时无活跃 Session，跳过")
            return
        session.context_blocks.append(ContextBlock(
            source=ContextSource.MANUAL,
            content=info,
        ))
        logger.debug("Vision Session %s: 注入手动上下文 (%d 字符)", session.id, len(info))

    def check_warning(self) -> str | None:
        """检查当前 Session 是否触发过期警告。"""
        session = self.get_active()
        if session is None:
            return None
        return _check_session_warning(session)

    def status_text(self) -> str:
        """生成当前 Session 的可读摘要。"""
        session = self.get_active()
        if session is None:
            return "没有活跃的 Vision Session。调 vision_screenshot 创建。"

        age_min = (datetime.now(timezone.utc) - session.created_at).total_seconds() / 60
        lines = [
            f"Vision Session: {session.id} (活跃 {age_min:.0f} 分钟)",
        ]
        if session.screenshots:
            last_ss = session.screenshots[-1]
            w = last_ss.meta.get("width", "?")
            h = last_ss.meta.get("height", "?")
            mode = last_ss.meta.get("mode", "?")
            sec_ago = int((datetime.now(timezone.utc) - last_ss.timestamp).total_seconds())
            lines.append(f"  截图: {session.screenshot_count} 张 (最近: {w}×{h} {mode}, {sec_ago}秒前)")
        else:
            lines.append(f"  截图: 0 张")

        manual_count = sum(
            1 for b in session.context_blocks if b.source == ContextSource.MANUAL
        )
        lines.append(
            f"  提问: {session.question_count} 次"
            + (f" | 手动上下文: {manual_count} 条" if manual_count else "")
        )

        if session.question_log:
            last = session.question_log[-1]
            verdict = last.get("verdict", {})
            passed = "✅" if verdict.get("pass") else "❌"
            reason = verdict.get("reason", "")[:120]
            lines.append(f"  上次结论: {passed} {reason}")

        # 自动注入上下文摘要
        dirty_count = sum(
            1 for b in session.context_blocks if b.source == ContextSource.DIRTY
        )
        write_count = sum(
            1 for b in session.context_blocks if b.source == ContextSource.WRITE
        )
        if dirty_count or write_count:
            parts = []
            if dirty_count:
                parts.append(f"dirty={dirty_count}")
            if write_count:
                parts.append(f"write={write_count}")
            lines.append(f"  自动注入上下文: {', '.join(parts)}")

        return "\n".join(lines)
