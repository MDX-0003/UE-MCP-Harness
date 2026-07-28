"""Reference-image subsystem -- ReferenceImageSession + match_reference handler.

Involved Issues: 016 (reference image comparison), 019 (session dataclass-ification), 0714 (stop mechanism).
"""

from __future__ import annotations

import base64
import io as _io
import json
import logging
import time
from pathlib import Path as _Path
from dataclasses import dataclass
from typing import Any

from mcp.types import CallToolResult, TextContent

from harness.tools import ToolContext, tool_fail, log_local_call, VISION_BADGES

_log = logging.getLogger("harness.reference")


# ---------------------------------------------------------------------------
# ReferenceImageSession (Issue 019 -- replaces magic dict)
# ---------------------------------------------------------------------------

@dataclass
class ReferenceImageSession:
    """State object for a reference-image comparison session.

    Replaces the bare ``_session_reference`` dict (14 magic keys).
    0714 stop formula: ``max(10, countdown_start_round + 3)`` --
    countdown decrements every round, resets on reference change.
    """

    ref_path: str = ""
    ref_b64: str = ""
    loaded: bool = False
    match_count: int = 0
    countdown_remaining: int | None = None
    countdown_start_round: int = 0
    max_allowed_rounds: int = 10
    metrics: dict | None = None
    prev_metrics: dict | None = None
    prev_hist: float | None = None
    best_metrics: dict | None = None
    decline_count: int = 0
    snapshot_saved: bool = False
    best_snapshot_path: str | None = None
    snapshot_base_path: str = ""

    # -- stop / lifecycle --------------------------------------------------

    def check_stop(self) -> str | None:
        """Hard-stop decision.  Returns None=continue, str=stop reason."""
        if self.countdown_remaining is not None and self.countdown_remaining < 0:
            return (
                "倒计时已归零"
                "（直方图>=0.70 达成后已用尽 3 次调整机会）"
            )
        if self.countdown_remaining is None and self.match_count >= self.max_allowed_rounds:
            return f"已达到最大轮次限制（{self.max_allowed_rounds} 轮）"
        return None

    def begin_round(self, ref_path: str) -> bool:
        """Start a new round.  Resets all accumulated state on reference change.

        Returns True when this is the first load (new session or new reference).
        """
        is_new_ref = ref_path != self.ref_path
        is_first = not self.loaded
        if is_new_ref and not is_first:
            self.__init__()
            self.loaded = True
            is_first = True
        self.ref_path = ref_path
        self.loaded = True
        self.match_count += 1
        return is_first

    def activate_countdown(self, hist: float) -> None:
        """Activate 3-round countdown when histogram first reaches 0.70+."""
        if self.countdown_remaining is None and hist >= 0.70:
            self.countdown_remaining = 3
            self.countdown_start_round = self.match_count
            self.max_allowed_rounds = self.match_count + 3

    def record_metrics(self, m: dict) -> dict:
        """Record this round's metrics.  Returns an events dict for the caller
        to render:  new_best, below_best, decline_streak.
        """
        events: dict = {}
        hist = m["histogram_correlation"]

        # countdown decrement (runs every round while active)
        if self.countdown_remaining is not None:
            self.countdown_remaining -= 1

        # best-point tracking
        best = self.best_metrics
        if best is None or hist > best.get("histogram_correlation", 0):
            self.best_metrics = {
                "histogram_correlation": hist,
                "round": self.match_count,
                "rb_ratio": m["color_temperature"]["cur_r_b_ratio"],
            }
            events["new_best"] = True
        elif best is not None and hist < best.get("histogram_correlation", 0):
            events["below_best"] = best

        # consecutive decline detection
        if self.prev_hist is not None and hist < self.prev_hist:
            self.decline_count += 1
            if self.decline_count >= 2:
                events["decline_streak"] = True
        else:
            self.decline_count = 0

        self.prev_hist = hist
        self.prev_metrics = self.metrics
        self.metrics = m
        return events


# ---------------------------------------------------------------------------
# Private helpers (moved from server.py)
# ---------------------------------------------------------------------------

def _b64_to_pil(b64: str) -> "Image.Image":
    """base64 PNG -> PIL Image (RGB)."""
    from PIL import Image as PILImage
    return PILImage.open(_io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _build_trend_summary(
    prev: dict[str, Any], cur: dict[str, Any],
) -> list[str]:
    """Generate metric-trend comparison lines (vs previous match_reference call)."""
    REF_RB = prev.get("color_temperature", {}).get("ref_r_b_ratio")

    def _arrow(prev_val: float, cur_val: float, target: float | None) -> str:
        if target is None:
            return ""
        prev_dist = abs(prev_val - target)
        cur_dist = abs(cur_val - target)
        if cur_dist < prev_dist * 0.9:
            return " ✓ 向参考值收敛"
        if cur_dist > prev_dist * 1.1:
            return " ✗ 远离参考值"
        return " ≈ 无显著变化"

    lines = [
        "",
        "\U0001f4ca 指标趋势（vs 上一次 match_reference）：",
        f"{'':>14} {'上次':>8} {'本次':>8} {'变化':>10}  {'收敛方向':>12}",
    ]

    # R/B ratio
    prev_rb = prev.get("color_temperature", {}).get("cur_r_b_ratio")
    cur_rb = cur.get("color_temperature", {}).get("cur_r_b_ratio")
    if prev_rb is not None and cur_rb is not None:
        rb_delta = cur_rb - prev_rb
        arrow = _arrow(prev_rb, cur_rb, REF_RB)
        lines.append(
            f"{'R/B 比值':>14} {prev_rb:>8.4f} {cur_rb:>8.4f}"
            f" {rb_delta:>+10.4f} {arrow}"
        )

    # Luminance
    prev_lum = prev.get("luminance", {}).get("cur")
    cur_lum = cur.get("luminance", {}).get("cur")
    ref_lum = prev.get("luminance", {}).get("ref")
    if prev_lum is not None and cur_lum is not None:
        lum_delta = cur_lum - prev_lum
        arrow = _arrow(prev_lum, cur_lum, ref_lum)
        lines.append(
            f"{'亮度':>14} {prev_lum:>8.1f} {cur_lum:>8.1f}"
            f" {lum_delta:>+10.1f} {arrow}"
        )

    # Saturation
    prev_sat = prev.get("saturation", {}).get("cur")
    cur_sat = cur.get("saturation", {}).get("cur")
    ref_sat = prev.get("saturation", {}).get("ref")
    if prev_sat is not None and cur_sat is not None:
        sat_delta = cur_sat - prev_sat
        arrow = _arrow(prev_sat, cur_sat, ref_sat)
        lines.append(
            f"{'饱和度':>14} {prev_sat:>8.1f} {cur_sat:>8.1f}"
            f" {sat_delta:>+10.1f} {arrow}"
        )

    return lines


# ---------------------------------------------------------------------------
# ref_resolve_snapshot_path (moved from server.py _ensure_best_snapshot_path)
# ---------------------------------------------------------------------------

async def ref_resolve_snapshot_path(ue_client: Any, session: ReferenceImageSession) -> str:
    """Build snapshot path: {current-level-dir}/{MMDD}-{LevelName}.umap.

    Queries UE for the current level path on first call; caches in session
    for subsequent calls.  Cache is cleared when the session resets (reference change).
    """
    cached = session.snapshot_base_path
    if cached:
        return cached

    try:
        from harness.client import mcp_parse_result, mcp_extract_text
        result = await ue_client.call_tool(
            "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
            {"LevelPath": ""},
        )
        parsed = mcp_parse_result(result)
        text = mcp_extract_text(parsed, result) or ""
        rv = json.loads(text) if isinstance(text, str) else {}  # type: ignore[arg-type]
        pkg_path: str = rv.get("packagePath", "") if isinstance(rv, dict) else ""
    except Exception:
        pkg_path = ""

    from datetime import datetime
    date_prefix = datetime.now().strftime("%m%d")

    if pkg_path:
        parts = pkg_path.rsplit("/", 1)
        if len(parts) == 2:
            snapshot_path = f"{parts[0]}/{date_prefix}-{parts[1]}"
        else:
            snapshot_path = f"/Game/{date_prefix}-Snapshot"
    else:
        snapshot_path = f"/Game/{date_prefix}-Snapshot"

    session.snapshot_base_path = snapshot_path
    return snapshot_path


# ---------------------------------------------------------------------------
# ref_build_stop_summary (moved from StopLimitInterceptor.build_summary)
# ---------------------------------------------------------------------------

def ref_build_stop_summary(
    session: ReferenceImageSession,
    best_snapshot_path: str | None = None,
    stop_reason: str = "",
) -> str:
    """Assemble a retrospective summary from structured session data."""
    lines: list[str] = [f"⛔ {stop_reason}", ""]

    if best_snapshot_path:
        lines.append(f"\U0001f3c6 最佳状态快照: {best_snapshot_path}")
        lines.append(
            "   仅当需要回退到此前保存的关卡状态时，才调 "
            f"LoadLevel(\"{best_snapshot_path}\")。"
        )
        lines.append(
            "   ⚠ 当前编辑器在原关卡，请勿无故加载快照--"
            "加载快照会丢弃当前所有未保存改动。"
        )
        lines.append("")

    best = session.best_metrics
    if best:
        lines.append("指标轨迹：")
        lines.append(
            f"    最佳 (第{best.get('round', '?')}轮): "
            f"直方图={best.get('histogram_correlation', '?'):.2f}, "
            f"R/B={best.get('rb_ratio', '?')} \U0001f3c6"
        )
        if session.prev_hist is not None:
            lines.append(
                f"    最终 (第{session.match_count}轮): "
                f"直方图={session.prev_hist:.2f}"
            )
        lines.append("")

    lines.append("请向用户确认是否继续调整，或调 deactivate_skill 退出。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# handle_match_reference (extracted from server.py _handle_match_reference)
# ---------------------------------------------------------------------------

async def handle_match_reference(ctx: ToolContext, arguments: dict) -> CallToolResult:
    """match_reference tool handler -- reference vs current-viewport comparison."""
    from harness.verification.vision_agent import VisionSubAgent

    # ctx.ref_session is a ReferenceImageSession; ToolContext still types it as dict
    # pending Issue 020 (ToolContext.ref_session typed update).
    session = ctx.ref_session
    t0 = time.monotonic()
    ref_path_str: str = arguments.get("path", "")

    # Phase 3: hard-stop check (countdown zero or max-rounds fallback)
    stop_reason = session.check_stop()
    if stop_reason is not None:
        best_path = session.best_snapshot_path
        summary = ref_build_stop_summary(session, best_path, stop_reason)
        log_local_call(ctx, "match_reference", arguments, summary, t0)
        return tool_fail(summary)

    # 1. Load reference image
    try:
        from PIL import Image as PILImage
        ref_path = _Path(ref_path_str).expanduser().resolve()
        if not ref_path.exists():
            return tool_fail(f"参考图不存在: {ref_path}")
        ref_img = PILImage.open(ref_path).convert("RGB")
    except Exception as e:
        return tool_fail(f"加载参考图失败: {e}")

    # 2. Capture current viewport
    try:
        from harness.verification.capturer import capture_screenshot as capturer_capture
        max_w, max_h = ctx.config.vision_max_size
        screenshot = await capturer_capture(
            ctx.ue_client, max_w, max_h, mode="viewport",
        )
        cur_b64 = screenshot.data_b64
    except Exception as e:
        return tool_fail(f"截图失败: {e}")

    # 3. Reference image -> base64
    buf = _io.BytesIO()
    ref_img.save(buf, format="PNG")
    ref_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    is_first = session.begin_round(str(ref_path))
    session.ref_b64 = ref_b64

    # 4. Quantitative metrics
    from harness.verification.metrics import compute_match_metrics
    try:
        cur_img = _b64_to_pil(cur_b64)
    except Exception:
        cur_img = None

    metrics_error: str | None = None
    metrics_result = None
    if cur_img is not None:
        try:
            metrics_result = compute_match_metrics(ref_img, cur_img)
            session.metrics = metrics_result
        except Exception as e:
            _log.warning("量化指标计算失败（非致命）: %s", e)
            metrics_error = str(e)
    else:
        metrics_error = "当前截图无法解码为 PIL Image"

    # 4b. Trend vs previous match_reference call
    prev_metrics = session.prev_metrics
    trend_lines: list[str] = []
    if prev_metrics is not None and metrics_result is not None:
        trend_lines = _build_trend_summary(prev_metrics, metrics_result)

    # 5. MiMo 9-dimension dual-image comparison
    question = (
        "请从以下 9 个维度比较当前截图与参考图的差异。"
        "每个维度只输出方向性判定，不需要描述绝对值：\n\n"
        "亮度 (Brightness):       darker / similar / brighter\n"
        "对比度 (Contrast):       lower / similar / higher\n"
        "色温 (Color Temperature): cooler / similar / warmer\n"
        "色调偏移 (Color Cast):    none / 偏X色\n"
        "饱和度 (Saturation):      less_saturated / similar / more_saturated\n"
        "大气密度 (Haze):          clearer / similar / hazier\n"
        "阴影方向 (Shadow Direction): 方向描述 + 是否一致\n"
        "天空表现 (Sky):           颜色/云量/渐变的差异方向\n"
        "视角方向 (Viewpoint Direction): looking_more_up / similar / looking_more_down\n\n"
        "注意：视角方向只比较相机俯仰角（向上看 vs 向下看），"
        "不考虑相机距离和目标对象。"
        "如果两张图拍摄的是完全不同的场景/对象，填 'different_scene'。\n\n"
        "每个判定配一句话佐证（你看到什么让你这样判断）。"
    )

    agent = VisionSubAgent(ctx.config)
    try:
        verdict = await agent.compare_with_reference(ref_b64, cur_b64, question)
    except Exception as e:
        err_text = f"match_reference Vision 调用失败: {e}"
        log_local_call(ctx, "match_reference", arguments, err_text, t0, error=e)
        return tool_fail(err_text)

    # 6. Assemble return text (body first; header prepended after countdown activation)
    ref_w, ref_h = ref_img.size

    body_lines: list[str] = []
    if trend_lines:
        body_lines.extend(trend_lines)
    body_lines.append("")

    body_lines.append("MiMo 9 维度差异：")
    body_lines.append(verdict.answer)
    _badges = {"high": "\U0001f7e2", "medium": "\U0001f7e1", "low": "\U0001f534"}
    body_lines.append(
        f"置信度: {_badges.get(verdict.confidence, '\U0001f7e1')} "
        f"{verdict.confidence}"
    )
    if verdict.observations:
        body_lines.append("")
        body_lines.append("分项观察：")
        for obs in verdict.observations:
            what = obs.get("what", "")
            finding = obs.get("finding", "")
            conf = obs.get("confidence", "medium")
            ob = _badges.get(conf, "~")
            body_lines.append(f"  {ob} {what}: {finding}")

    if metrics_result:
        m = metrics_result
        hist = m["histogram_correlation"]
        rb_cur = m["color_temperature"]["cur_r_b_ratio"]

        # Phase 1: countdown activation (first time hist >= 0.70)
        session.activate_countdown(hist)

        # Phase 1c: record metrics (best-point tracking, decline detection, countdown decrement)
        events = session.record_metrics(m)

        body_lines.append("")
        body_lines.append("量化指标（全图统计，不受视点移动影响）：")
        body_lines.append(f"{'':>12} {'参考图':>8} {'当前':>8} {'差异':>10}")
        body_lines.append(
            f"{'亮度':>12} {m['luminance']['ref']:>8.1f} "
            f"{m['luminance']['cur']:>8.1f} {m['luminance']['delta_pct']:>+9.1f}%"
        )
        body_lines.append(
            f"{'对比度':>12} {m['contrast']['ref']:>8.1f} "
            f"{m['contrast']['cur']:>8.1f} {m['contrast']['delta_pct']:>+9.1f}%"
        )
        ct = m["color_temperature"]
        body_lines.append(
            f"{'色温':>12} {'R/B=' + str(ct['ref_r_b_ratio']):>8} "
            f"{'R/B=' + str(ct['cur_r_b_ratio']):>8}"
        )
        body_lines.append(
            f"{'饱和度':>12} {m['saturation']['ref']:>8.1f} "
            f"{m['saturation']['cur']:>8.1f} {m['saturation']['delta_pct']:>+9.1f}%"
        )
        body_lines.append(
            f"{'直方图相似度':>12} {'':>8} {'':>8} "
            f"{m['histogram_correlation']:>10.2f} (0->完全不同, 1->完全一致)"
        )

        # Best-point rendering
        if events.get("new_best"):
            body_lines.append("")
            body_lines.append(
                f"\U0001f3c6 新最佳记录：直方图相似度 {hist:.2f}"
                f"（第 {session.match_count} 轮）"
            )

            # Phase 2: best-state snapshot (save once when first crossing 0.70)
            if hist >= 0.70 and not session.snapshot_saved:
                session.snapshot_saved = True
                snapshot_path = await ref_resolve_snapshot_path(
                    ctx.ue_client, session,
                )
                try:
                    await ctx.ue_client.call_tool(
                        "LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs",
                        {"TargetPath": snapshot_path},
                    )
                    session.best_snapshot_path = snapshot_path
                    body_lines.append("")
                    body_lines.append(
                        f"\U0001f4be 快照已保存至 {snapshot_path}，编辑器仍在原关卡。"
                        f"仅当需要回退错误操作时才调 LoadLevel，请勿无故加载快照。"
                    )
                except Exception as e:
                    _log.warning("SaveLevelAs 失败: %s", e)

        elif events.get("below_best"):
            best = events["below_best"]
            best_hist = best.get("histogram_correlation", 0)
            best_round = best.get("round", "?")
            body_lines.append("")
            body_lines.append(
                f"⚠ 当前直方图相似度 {hist:.2f} 低于最佳记录 "
                f"{best_hist:.2f}（第 {best_round} 轮）。"
                f"可能已越过最佳点，考虑回退到上一轮参数。"
            )

        # Consecutive decline
        if events.get("decline_streak"):
            body_lines.append(
                "\U0001f534 直方图相似度连续 2 轮下降，"
                "建议回退到上一轮参数并停止该方向调整。"
            )

    elif metrics_error:
        body_lines.append(f"\n⚠ 量化指标计算失败: {metrics_error}")
        body_lines.append("MiMo 分析仍然有效。")

    body_lines.append("")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(
        "⚠ **必须先调 activate_skill('match-atmosphere')** "
        "获取完整的 5 步匹配工作流（含强制关闭后处理、正确的调整顺序、颜色诊断决策树）。"
        "在激活 Skill 之前不要手动调整场景参数——你会跳过关键的 Step 1.5（关闭后处理 Volume），"
        "导致后处理掩盖光源的真实效果。"
    )
    body_lines.append("")
    if is_first:
        body_lines.append(
            "⚠ match_reference 是氛围对比的**唯一工具**：\n"
            "- 每轮调整后必须先调 match_reference 看量化指标\n"
            "- **不要用 vision_screenshot 或 vision_ask 判断氛围变化**\n"
            "- vision_screenshot 仅用于: (1) 非参考图任务的视觉验证，\n"
            "  或 (2) match_reference 确认收敛方向后的最终视觉确认\n"
            "- 氛围对比走 match_reference，视觉确认走 vision_screenshot——\n"
            "  两者不可互换。"
        )
        body_lines.append("")
        body_lines.append(
            "在存在参考图的任务里，每轮迭代请使用 "
            f"match_reference(\"{ref_path_str}\") 获取对比反馈。"
        )
        body_lines.append("")
    else:
        body_lines.append(
            "每轮调整后必须先调 match_reference 看量化指标。"
            "不要用 vision_screenshot 或 vision_ask 替代。"
        )
        body_lines.append("")
    body_lines.append(
        "match_reference 每次返回量化指标（R/B、亮度、饱和度）--"
        "这是确定性像素计算，不受 VLM 主观判断影响，是最可靠的调整指南针。"
    )
    body_lines.append(
        "⚠ MiMo 分析与量化指标方向一致 -> 高置信；"
        "不一致 -> 以量化指标为准。"
    )

    # Header (countdown status)
    if session.countdown_remaining is not None and session.countdown_remaining > 0:
        header_lines = [
            f"参考图：{ref_path.name} ({ref_w}x{ref_h})",
            f"第 {session.match_count} 轮（最多 {session.max_allowed_rounds} 轮，"
            f"⏳ 直方图已达 0.70+，剩余 {session.countdown_remaining} 次调整机会）",
        ]
    elif session.countdown_remaining is not None and session.countdown_remaining == 0:
        header_lines = [
            f"参考图：{ref_path.name} ({ref_w}x{ref_h})",
            f"第 {session.match_count} 轮（最多 {session.max_allowed_rounds} 轮，"
            f"⏳ 本轮为最后一次调整机会）",
        ]
    else:
        header_lines = [
            f"参考图：{ref_path.name} ({ref_w}x{ref_h})",
            f"第 {session.match_count} 轮（最多 {session.max_allowed_rounds} 轮）",
        ]

    lines = header_lines + body_lines

    result_text = "\n".join(lines)
    log_local_call(ctx, "match_reference", arguments, result_text, t0)
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
