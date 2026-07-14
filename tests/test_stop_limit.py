"""测试 match_reference 叫停机制 v3 — 倒计时 + 10轮兜底 + SaveLevelAs 一次 + Session 重置.

计数逻辑:
  - hist<0.70 始终: 10 轮后硬终止兜底
  - hist≥0.70 首次达成: 激活倒计时 3 轮，max_rounds = 达成轮 + 3
  - 倒计时归零 (<0): pre_call 硬拦截
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp.server.lowlevel.server import request_ctx
from mcp.types import CallToolRequest, CallToolRequestParams
from mcp.shared.context import RequestContext

from harness.config import Config
from harness.server import build_server
from harness.interceptor import DebugPreCallInterceptor
from harness.stop_limit import StopLimitInterceptor


# ──────────────────────────────────────────────
# 测试辅助
# ──────────────────────────────────────────────

def _make_fake_screenshot_b64(width: int = 10, height: int = 10) -> str:
    buf = io.BytesIO()
    from PIL import Image as PILImage
    PILImage.new("RGB", (width, height), (128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _make_ref_image(tmp_path: Path, color: tuple = (100, 100, 100)) -> Path:
    from PIL import Image as PILImage
    ref = tmp_path / "ref.png"
    PILImage.new("RGB", (10, 10), color).save(ref)
    return ref


def _fake_verdict():
    return MagicMock(
        answer="亮度: similar\n色温: similar\n饱和度: similar\n",
        confidence="high",
        observations=[],
    )


def _fake_metrics(hist: float = 0.50):
    """构造假量化指标，histogram_correlation 可控。"""
    return {
        "luminance": {"ref": 100.0, "cur": 110.0, "delta_pct": 10.0},
        "contrast": {"ref": 40.0, "cur": 42.0, "delta_pct": 5.0},
        "color_temperature": {"ref_r_b_ratio": 1.2, "cur_r_b_ratio": 1.3},
        "saturation": {"ref": 60.0, "cur": 55.0, "delta_pct": -8.3},
        "histogram_correlation": hist,
    }


def _setup_capturer_mock(monkeypatch):
    import harness.verification.capturer as capturer_mod
    fake_b64 = _make_fake_screenshot_b64(10, 10)

    async def _fake_capture(*args, **kwargs):
        return MagicMock(data_b64=fake_b64, width=10, height=10)

    monkeypatch.setattr(capturer_mod, "capture", _fake_capture)


def _build_server_and_call(ue_client, stop_limit, tmp_path):
    """构建 server 并返回 call_match 辅助函数。"""
    ref_path = _make_ref_image(tmp_path)
    server = build_server(
        config=Config(),
        ue_client=ue_client,
        interceptors=[DebugPreCallInterceptor()],
        skills_dir=Path("skills"),
        stop_limit=stop_limit,
    )
    handler_fn = server.request_handlers[CallToolRequest]

    async def call() -> MagicMock:
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="match_reference",
                arguments={"path": str(ref_path)},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )
        token = request_ctx.set(ctx)
        try:
            return await handler_fn(req)
        finally:
            request_ctx.reset(token)

    return call


# ──────────────────────────────────────────────
# 倒计时机制
# ──────────────────────────────────────────────


class TestCountdown:
    """验证 hist≥0.70 时倒计时激活 + 递减 + 硬拦截。"""

    @pytest.mark.asyncio
    async def test_countdown_activates_at_hist_70(self, tmp_path: Path, monkeypatch):
        """hist≥0.70 首次达成 → 输出含倒计时提示。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.72),
        ):
            result = await call()
            text = result.root.content[0].text
            assert "0.70" in text or "直方图" in text
            assert "剩余" in text or "机会" in text, (
                f"倒计时激活后应显示剩余机会，实际: {text[:200]}"
            )

    @pytest.mark.asyncio
    async def test_countdown_hard_stop_after_3_rounds(self, tmp_path: Path, monkeypatch):
        """倒计时激活后 R1-R4 通过，R5 被硬拦截。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.72),
        ):
            # R1: 激活倒计时 → 剩余 2 次
            r1 = await call()
            assert not r1.root.isError, "R1 不应被拦截（激活轮）"

            # R2-R4: countdown 倒计时通过
            for i in range(2, 5):
                r = await call()
                assert not r.root.isError, f"R{i} 不应被拦截"

            # R5: countdown=-1 < 0 → 硬拦截
            r5 = await call()
            assert r5.root.isError, "R5 应被倒计时硬拦截（countdown < 0）"
            assert "倒计时" in r5.root.content[0].text

    @pytest.mark.asyncio
    async def test_r1_achieve_70_max_4_rounds(self, tmp_path: Path, monkeypatch):
        """R1 达成 0.75 → R1-R4 通过，R5 被拦截。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.75),
        ):
            # R1-R4 应全部通过
            for i in range(4):
                r = await call()
                assert not r.root.isError, f"R{i + 1} 应通过"

            # R5 应被拦截
            r5 = await call()
            assert r5.root.isError, "R5 应被硬拦截"
            assert "倒计时" in r5.root.content[0].text


# ──────────────────────────────────────────────
# 10 轮兜底（始终未达 0.70）
# ──────────────────────────────────────────────


class TestFallback10Rounds:
    """验证始终未达 0.70 → 10 轮后兜底硬终止。"""

    @pytest.mark.asyncio
    async def test_no_countdown_10_round_fallback(self, tmp_path: Path, monkeypatch):
        """hist 始终 < 0.70 → 10 轮通过，11 轮被拦截。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.50),  # 始终 < 0.70
        ):
            for i in range(10):
                r = await call()
                assert not r.root.isError, f"R{i + 1} 应通过（<0.70，10轮兜底）"

            r11 = await call()
            assert r11.root.isError, "R11 应被 10 轮兜底拦截"
            assert "最大轮次限制" in r11.root.content[0].text


# ──────────────────────────────────────────────
# SaveLevelAs 仅一次
# ──────────────────────────────────────────────


class TestSaveLevelAsOnce:
    """验证快照仅在 hist 首次跨过 0.70 时保存一次。"""

    @pytest.mark.asyncio
    async def test_save_only_on_first_cross_70(self, tmp_path: Path, monkeypatch):
        """hist R1=0.65(<0.70) → R2=0.72(首次触发保存) → R3=0.80(不再保存)。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        metrics_sequence = [
            _fake_metrics(hist=0.65),  # R1: 未触发
            _fake_metrics(hist=0.72),  # R2: 首次 ≥0.70 → 应触发 SaveLevelAs
            _fake_metrics(hist=0.80),  # R3: 更高但不触发
            _fake_metrics(hist=0.82),  # R4: 继续不触发
        ]
        call_idx = [0]

        def _rotating_metrics(*args, **kwargs):
            m = metrics_sequence[call_idx[0] % len(metrics_sequence)]
            call_idx[0] += 1
            return m

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            side_effect=_rotating_metrics,
        ):
            await call()  # R1: hist=0.65, 不触发
            await call()  # R2: hist=0.72, 应触发
            await call()  # R3: hist=0.80, 不触发
            await call()  # R4: hist=0.82, 不触发

        save_calls = [
            c for c in ue_client.call_tool.mock_calls
            if "SaveLevelAs" in str(c.args)
        ]
        assert len(save_calls) == 1, (
            f"SaveLevelAs 应仅被调用 1 次（首次跨过 0.70），实际: {len(save_calls)}"
        )


# ──────────────────────────────────────────────
# Session 重置
# ──────────────────────────────────────────────


class TestSessionReset:
    """验证换参考图时状态重置。"""

    @pytest.mark.asyncio
    async def test_counter_resets_on_new_reference(self, tmp_path: Path, monkeypatch):
        """换参考图后计数器重置，不会错误触发硬终止。"""
        ref1 = _make_ref_image(tmp_path, color=(100, 100, 100))
        ref2 = _make_ref_image(tmp_path, color=(200, 200, 200))
        ref2_new = tmp_path / "ref2.png"
        ref2.rename(ref2_new)

        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.50),
        ):
            server = build_server(
                config=Config(), ue_client=ue_client,
                interceptors=[DebugPreCallInterceptor()],
                skills_dir=Path("skills"), stop_limit=sl,
            )
            handler_fn = server.request_handlers[CallToolRequest]

            async def call_match(path: Path):
                req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="match_reference", arguments={"path": str(path)},
                    ),
                    jsonrpc="2.0", id=1,
                )
                ctx = RequestContext(
                    request_id="req-1", meta=None,
                    session=MagicMock(), lifespan_context=None, request=req,
                )
                token = request_ctx.set(ctx)
                try:
                    return await handler_fn(req)
                finally:
                    request_ctx.reset(token)

            for _ in range(3):
                await call_match(ref1)

            for _ in range(8):
                result = await call_match(ref2_new)

            assert not result.root.isError, (
                "换参考图后计数器应重置，8 次不应触发硬终止"
            )


# ──────────────────────────────────────────────
# 输出内容验证
# ──────────────────────────────────────────────


class TestOutputContent:
    """验证 match_reference 输出中关键信息的存在性。"""

    @pytest.mark.asyncio
    async def test_round_display_shows_countdown(self, tmp_path: Path, monkeypatch):
        """倒计时激活后输出含"最多 N 轮"和倒计时提示。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.72),
        ):
            result = await call()
            text = result.root.content[0].text
            assert "第 1 轮" in text, "应显示轮次"
            assert "最多" in text, "应显示最大轮次"
            assert "⏳" in text, "倒计时激活应显示 ⏳ 符号"

    @pytest.mark.asyncio
    async def test_snapshot_message_mentions_editor_state(self, tmp_path: Path, monkeypatch):
        """快照消息应告知编辑器仍在原关卡。"""
        ue_client = AsyncMock()
        sl = StopLimitInterceptor()
        _setup_capturer_mock(monkeypatch)
        call = _build_server_and_call(ue_client, sl, tmp_path)

        with patch(
            "harness.verification.vision_agent.VisionSubAgent.compare_with_reference",
            new_callable=AsyncMock, return_value=_fake_verdict(),
        ), patch(
            "harness.verification.metrics.compute_match_metrics",
            return_value=_fake_metrics(hist=0.72),
        ):
            result = await call()
            text = result.root.content[0].text
            assert "编辑器仍在原关卡" in text, (
                f"快照消息应注明编辑器状态，实际: {text[:300]}"
            )
            assert "回退" in text, "应提及回退"


# ──────────────────────────────────────────────
# StopLimitInterceptor 单元测试 (v3 措辞)
# ──────────────────────────────────────────────


class TestStopLimitInterceptorUnit:
    """StopLimitInterceptor.build_summary 单元测试。"""

    def test_build_summary_with_stop_reason(self):
        """stop_reason 出现在摘要首行。"""
        sl = StopLimitInterceptor()
        session_ref = {"_match_count": 4, "best_metrics": {
            "histogram_correlation": 0.82, "round": 1, "rb_ratio": 1.3,
        }}

        summary = sl.build_summary(
            session_ref,
            best_snapshot_path="/Game/Maps/0714-Test",
            stop_reason="倒计时已归零",
        )

        assert "倒计时已归零" in summary
        assert "0.82" in summary

    def test_build_summary_loadlevel_wording(self):
        """LoadLevel 措辞含"仅当需要回退"和"请勿无故加载"。"""
        sl = StopLimitInterceptor()
        session_ref = {"_match_count": 4, "best_metrics": {
            "histogram_correlation": 0.82, "round": 1, "rb_ratio": 1.3,
        }}

        summary = sl.build_summary(
            session_ref,
            best_snapshot_path="/Game/Maps/0714-Test",
            stop_reason="倒计时已归零",
        )

        assert "仅当需要回退" in summary, (
            f"应强调仅回退时用 LoadLevel，实际: {summary}"
        )
        assert "请勿无故加载快照" in summary
        assert "回退" in summary

    def test_build_summary_no_snapshot(self):
        """无快照路径时不输出恢复提示。"""
        sl = StopLimitInterceptor()
        session_ref = {
            "_match_count": 10,
            "best_metrics": {"histogram_correlation": 0.85, "round": 3, "rb_ratio": 1.3},
        }

        summary = sl.build_summary(
            session_ref, best_snapshot_path=None, stop_reason="最大轮次",
        )

        assert "LoadLevel" not in summary
        assert "deactivate_skill" in summary

    def test_build_summary_no_best_metrics(self):
        """无 best_metrics 时不输出指标轨迹。"""
        sl = StopLimitInterceptor()
        session_ref = {"_match_count": 10}

        summary = sl.build_summary(
            session_ref, stop_reason="已达到最大轮次限制（10 轮）",
        )

        assert "最大轮次" in summary
        assert "指标轨迹" not in summary
        assert "deactivate_skill" in summary
