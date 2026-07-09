"""参考图量化指标 — 5 项全图统计值，用于交叉校验 Vision 的主观分析.

纯 Pillow 计算，零外部依赖。不涉及 UE、MCP、Vision API。
入口 compute_match_metrics(ref, cur) 接受两个 PIL Image 对象。
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image


def compute_match_metrics(ref: Image.Image, cur: Image.Image) -> dict[str, Any]:
    """计算参考图与当前图的 5 项量化指标。

    Args:
        ref: Pillow Image，参考图（RGB）
        cur: Pillow Image，当前截图（RGB）

    Returns:
        {
            "luminance":       {"ref": float, "cur": float, "delta_pct": float},
            "contrast":        {"ref": float, "cur": float, "delta_pct": float},
            "color_temperature": {"ref_r_b_ratio": float, "cur_r_b_ratio": float},
            "saturation":      {"ref": float, "cur": float, "delta_pct": float},
            "histogram_correlation": float  # 0..1
        }

    Raises:
        ValueError: 输入非 RGB 或尺寸差异过大（>2x）
    """
    _validate(ref, cur)

    ref_lum = _luminance(ref)
    cur_lum = _luminance(cur)
    ref_contrast = _rms_contrast(ref, ref_lum)
    cur_contrast = _rms_contrast(cur, cur_lum)
    ref_rb = _r_b_ratio(ref)
    cur_rb = _r_b_ratio(cur)
    ref_sat = _saturation(ref)
    cur_sat = _saturation(cur)

    return {
        "luminance": {
            "ref": round(ref_lum, 2),
            "cur": round(cur_lum, 2),
            "delta_pct": round(_delta_pct(ref_lum, cur_lum), 2),
        },
        "contrast": {
            "ref": round(ref_contrast, 2),
            "cur": round(cur_contrast, 2),
            "delta_pct": round(_delta_pct(ref_contrast, cur_contrast), 2),
        },
        "color_temperature": {
            "ref_r_b_ratio": round(ref_rb, 4),
            "cur_r_b_ratio": round(cur_rb, 4),
        },
        "saturation": {
            "ref": round(ref_sat, 2),
            "cur": round(cur_sat, 2),
            "delta_pct": round(_delta_pct(ref_sat, cur_sat), 2),
        },
        "histogram_correlation": round(_histogram_correlation(ref, cur), 4),
    }


# ---- 内部：像素数据迭代 ----

def _iter_rgb(img: Image.Image):
    """从 RGB 图像逐像素 yield (r, g, b) 元组。

    Pillow 12.x: get_flattened_data() 仍返回 tuple 序列（等价于 getdata）。
    Pillow 14+: 将返回 flat list，届时需适配。
    """
    return iter(img.get_flattened_data())


def _iter_hsv(img: Image.Image):
    """从 HSV 图像逐像素 yield (h, s, v) 元组。"""
    hsv = img.convert("HSV")
    return iter(hsv.get_flattened_data())


# ---- 内部：指标计算 ----

def _delta_pct(ref_val: float, cur_val: float) -> float:
    """cur 相对于 ref 的变化百分比。ref 为 0 时返回 0.0。"""
    if ref_val == 0.0:
        return 0.0
    return (cur_val - ref_val) / ref_val * 100.0


def _luminance(img: Image.Image) -> float:
    """逐像素加权亮度 0.299R + 0.587G + 0.114B → 全图均值."""
    total = 0.0
    count = 0
    for r, g, b in _iter_rgb(img):
        total += 0.299 * r + 0.587 * g + 0.114 * b
        count += 1
    return total / count if count else 0.0


def _rms_contrast(img: Image.Image, mean_luminance: float | None = None) -> float:
    """RMS contrast — 加权亮度的全图标准差."""
    if mean_luminance is None:
        mean_luminance = _luminance(img)
    total = 0.0
    count = 0
    for r, g, b in _iter_rgb(img):
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        total += (lum - mean_luminance) ** 2
        count += 1
    variance = total / count if count else 0.0
    return math.sqrt(variance)


def _r_b_ratio(img: Image.Image) -> float:
    """R̄ / B̄ 通道均值比。>1 偏暖，<1 偏冷。B 均值为 0 时返回 1.0."""
    r_sum = 0
    b_sum = 0
    for r, g, b in _iter_rgb(img):
        r_sum += r
        b_sum += b
    if b_sum == 0:
        return 1.0
    return r_sum / b_sum


def _saturation(img: Image.Image) -> float:
    """HSV S 通道均值。0（灰）到 255（纯色）."""
    s_sum = 0
    count = 0
    for h, s, v in _iter_hsv(img):
        s_sum += s
        count += 1
    return s_sum / count if count else 0.0


def _histogram_correlation(ref: Image.Image, cur: Image.Image) -> float:
    """三通道 256-bin 直方图拼接（768-bin）→ 余弦相似度。0..1."""
    ref_hist = _flatten_histogram(ref)
    cur_hist = _flatten_histogram(cur)

    dot = sum(a * b for a, b in zip(ref_hist, cur_hist))
    mag_ref = math.sqrt(sum(a * a for a in ref_hist))
    mag_cur = math.sqrt(sum(b * b for b in cur_hist))

    if mag_ref == 0 or mag_cur == 0:
        return 0.0
    return dot / (mag_ref * mag_cur)


def _flatten_histogram(img: Image.Image) -> list[int]:
    """三通道 256-bin 直方图拼接为 768-bin 一维列表."""
    hist = img.histogram()  # PIL: [R0..R255, G0..G255, B0..B255]
    return list(hist)


# ---- 内部：输入验证 ----

def _validate(ref: Image.Image, cur: Image.Image) -> None:
    """验证输入：必须 RGB，尺寸差异不能超过 2x."""
    if ref.mode != "RGB":
        raise ValueError(f"参考图必须是 RGB 模式，当前: {ref.mode}")
    if cur.mode != "RGB":
        raise ValueError(f"当前图必须是 RGB 模式，当前: {cur.mode}")

    max_w = max(ref.width, cur.width)
    min_w = min(ref.width, cur.width)
    max_h = max(ref.height, cur.height)
    min_h = min(ref.height, cur.height)

    if min_w > 0 and max_w / min_w > 2.0:
        raise ValueError(
            f"宽度差异过大: ref={ref.width}, cur={cur.width}"
            f" (ratio={max_w / min_w:.1f}x > 2x)"
        )
    if min_h > 0 and max_h / min_h > 2.0:
        raise ValueError(
            f"高度差异过大: ref={ref.height}, cur={cur.height}"
            f" (ratio={max_h / min_h:.1f}x > 2x)"
        )
