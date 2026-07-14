"""match_reference 调用次数硬限制——兜底机制。

v3: 倒计时优先（hist≥0.70 → 3 轮后切断），10 轮兜底。
不依赖 JSONL 文本解析——直接从 _session_reference 结构化数据构建摘要。
"""

from __future__ import annotations

import logging

from harness.interceptor import ToolCallInterceptor

logger = logging.getLogger("harness.stop_limit")


class StopLimitInterceptor(ToolCallInterceptor):
    """match_reference 硬限制拦截器（兜底）。

    计数器 + 倒计时状态由 server.py 在 _session_reference 中维护。
    不依赖 JSONL 文本解析——直接从 _session_reference 结构化数据构建摘要。
    """

    def build_summary(
        self, session_ref: dict,
        best_snapshot_path: str | None = None,
        stop_reason: str = "",
    ) -> str:
        """从 session_ref 结构化数据组装回顾摘要。"""
        match_count = session_ref.get("_match_count", 0)
        best = session_ref.get("best_metrics")
        prev_hist = session_ref.get("_prev_hist")

        lines = [f"⛔ {stop_reason}", ""]

        if best_snapshot_path:
            lines.append(f"🏆 最佳状态快照: {best_snapshot_path}")
            lines.append(
                "   仅当需要回退到此前保存的关卡状态时，才调 "
                f"LoadLevel(\"{best_snapshot_path}\")。"
            )
            lines.append(
                "   ⚠ 当前编辑器在原关卡，请勿无故加载快照——"
                "加载快照会丢弃当前所有未保存改动。"
            )
            lines.append("")

        if best:
            lines.append("指标轨迹：")
            lines.append(
                f"    最佳 (第{best.get('round', '?')}轮): "
                f"直方图={best.get('histogram_correlation', '?'):.2f}, "
                f"R/B={best.get('rb_ratio', '?')} 🏆"
            )
            if prev_hist is not None:
                lines.append(
                    f"    最终 (第{match_count}轮): "
                    f"直方图={prev_hist:.2f}"
                )
            lines.append("")

        lines.append("请向用户确认是否继续调整，或调 deactivate_skill 退出。")
        return "\n".join(lines)
