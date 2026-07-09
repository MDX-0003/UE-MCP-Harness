"""测试 compute_match_metrics — 5 项量化指标 + 边界条件."""

from __future__ import annotations

import pytest
from PIL import Image

from harness.verification.metrics import compute_match_metrics


# ---- 辅助：创建测试图像 ----

def _solid_image(r: int, g: int, b: int, w: int = 100, h: int = 80) -> Image.Image:
    return Image.new("RGB", (w, h), (r, g, b))


def _gradient_image(w: int = 100, h: int = 80) -> Image.Image:
    """左黑右白的水平渐变."""
    img = Image.new("RGB", (w, h))
    for x in range(w):
        v = int(255 * x / (w - 1))
        for y in range(h):
            img.putpixel((x, y), (v, v, v))
    return img


# ---- Luminance ----

class TestLuminance:
    """亮度 = 0.299R + 0.587G + 0.114B 的全图均值."""

    def test_black_image(self):
        ref = _solid_image(0, 0, 0)
        cur = _solid_image(0, 0, 0)
        result = compute_match_metrics(ref, cur)
        assert result["luminance"]["ref"] == pytest.approx(0.0, abs=0.1)
        assert result["luminance"]["cur"] == pytest.approx(0.0, abs=0.1)
        assert result["luminance"]["delta_pct"] == pytest.approx(0.0, abs=0.1)

    def test_white_image(self):
        ref = _solid_image(255, 255, 255)
        cur = _solid_image(255, 255, 255)
        result = compute_match_metrics(ref, cur)
        # 白像素加权亮度 = 0.299*255 + 0.587*255 + 0.114*255 = 255
        assert result["luminance"]["ref"] == pytest.approx(255.0, abs=0.5)
        assert result["luminance"]["cur"] == pytest.approx(255.0, abs=0.5)

    def test_brighter_current(self):
        ref = _solid_image(100, 100, 100)
        cur = _solid_image(200, 200, 200)
        result = compute_match_metrics(ref, cur)
        assert result["luminance"]["delta_pct"] > 0  # cur 更亮
        assert result["luminance"]["delta_pct"] == pytest.approx(100.0, abs=0.1)


# ---- Contrast ----

class TestContrast:
    """RMS contrast = 加权亮度的全图标准差."""

    def test_flat_image_zero_contrast(self):
        ref = _solid_image(128, 128, 128)
        cur = _solid_image(128, 128, 128)
        result = compute_match_metrics(ref, cur)
        assert result["contrast"]["ref"] == pytest.approx(0.0, abs=0.2)
        assert result["contrast"]["cur"] == pytest.approx(0.0, abs=0.2)

    def test_gradient_has_contrast(self):
        ref = _gradient_image()
        cur = _solid_image(128, 128, 128)
        result = compute_match_metrics(ref, cur)
        assert result["contrast"]["ref"] > 0
        assert result["contrast"]["cur"] == pytest.approx(0.0, abs=0.2)

    def test_higher_contrast_detected(self):
        ref = _solid_image(100, 100, 100)
        cur = _gradient_image()
        result = compute_match_metrics(ref, cur)
        assert result["contrast"]["cur"] > result["contrast"]["ref"]


# ---- Color Temperature ----

class TestColorTemperature:
    """R/B 比 — R̄ / B̄。>1 偏暖，<1 偏冷."""

    def test_warm_red_dominant(self):
        ref = _solid_image(200, 100, 50)   # R/B = 4.0 → 偏暖
        cur = _solid_image(50, 100, 200)   # R/B = 0.25 → 偏冷
        result = compute_match_metrics(ref, cur)
        assert result["color_temperature"]["ref_r_b_ratio"] > 1.5
        assert result["color_temperature"]["cur_r_b_ratio"] < 0.5

    def test_neutral_gray(self):
        ref = _solid_image(128, 128, 128)
        cur = _solid_image(128, 128, 128)
        result = compute_match_metrics(ref, cur)
        assert result["color_temperature"]["ref_r_b_ratio"] == pytest.approx(1.0, abs=0.02)
        assert result["color_temperature"]["cur_r_b_ratio"] == pytest.approx(1.0, abs=0.02)

    def test_zero_blue_channel(self):
        """B 均值为 0 时返回 1.0（避免除零）."""
        ref = _solid_image(100, 100, 0)  # B=0
        cur = _solid_image(200, 200, 100)
        result = compute_match_metrics(ref, cur)
        assert result["color_temperature"]["ref_r_b_ratio"] == pytest.approx(1.0, abs=0.01)


# ---- Saturation ----

class TestSaturation:
    """HSV S 通道均值。0=灰，255=纯色."""

    def test_gray_zero_saturation(self):
        ref = _solid_image(128, 128, 128)
        cur = _solid_image(100, 100, 100)
        result = compute_match_metrics(ref, cur)
        assert result["saturation"]["ref"] == pytest.approx(0.0, abs=0.5)
        assert result["saturation"]["cur"] == pytest.approx(0.0, abs=0.5)

    def test_pure_red_full_saturation(self):
        ref = _solid_image(255, 0, 0)
        cur = _solid_image(255, 0, 0)
        result = compute_match_metrics(ref, cur)
        assert result["saturation"]["ref"] > 200  # HSV S max=255
        assert result["saturation"]["cur"] > 200

    def test_more_saturated_detected(self):
        ref = _solid_image(200, 180, 180)  # 低饱和（偏灰的红色）
        cur = _solid_image(255, 0, 0)      # 高饱和（纯红）
        result = compute_match_metrics(ref, cur)
        assert result["saturation"]["delta_pct"] > 0


# ---- Histogram Correlation ----

class TestHistogramCorrelation:
    """三通道 256-bin 直方图拼接（768-bin）→ 余弦相似度."""

    def test_identical_images_perfect_correlation(self):
        ref = _gradient_image()
        cur = _gradient_image()
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] == pytest.approx(1.0, abs=0.001)

    def test_black_vs_white_low_correlation(self):
        ref = _solid_image(0, 0, 0)
        cur = _solid_image(255, 255, 255)
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] < 0.1

    def test_similar_distribution(self):
        ref = _solid_image(100, 100, 100)
        cur = _solid_image(110, 110, 110)
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] < 0.5


# ---- Validation ----

class TestValidation:
    """输入验证."""

    def test_non_rgb_raises(self):
        ref = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
        cur = Image.new("RGB", (10, 10), (0, 0, 0))
        with pytest.raises(ValueError, match="RGB"):
            compute_match_metrics(ref, cur)

    def test_size_mismatch_too_large_raises(self):
        ref = Image.new("RGB", (100, 100), (0, 0, 0))
        cur = Image.new("RGB", (300, 300), (0, 0, 0))  # 3x > 2x
        with pytest.raises(ValueError, match="size|宽度|高度"):
            compute_match_metrics(ref, cur)

    def test_size_mismatch_within_limit_ok(self):
        ref = Image.new("RGB", (100, 100), (0, 0, 0))
        cur = Image.new("RGB", (150, 150), (0, 0, 0))  # 1.5x < 2x
        result = compute_match_metrics(ref, cur)
        assert "luminance" in result

    def test_single_color_image(self):
        """单色图像：所有指标应可计算，histogram_correlation=1.0."""
        ref = _solid_image(42, 128, 200)
        cur = _solid_image(42, 128, 200)
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] == pytest.approx(1.0, abs=0.001)
        assert result["contrast"]["ref"] == pytest.approx(0.0, abs=0.1)
