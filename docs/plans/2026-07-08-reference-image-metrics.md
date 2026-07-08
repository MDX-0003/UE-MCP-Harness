# 参考图量化指标 Implementation Plan

> **依赖:** 无（独立模块，可率先开发、测试、合并）
> **被依赖:** Plan 2（`match_reference` handler 调用 `compute_match_metrics`）

**Goal:** 新增纯计算模块 `harness/verification/metrics.py`，提供 5 项全图统计指标用于交叉校验 MiMo 的 8 维度主观分析。

**Architecture:** 纯 Pillow 计算，零外部依赖（Pillow 已在 `pyproject.toml`）。不涉及 UE、MCP、Vision API。入口函数 `compute_match_metrics(ref, cur) -> dict`，接受两个 PIL `Image` 对象，返回结构化 dict。

**Tech Stack:** Python 3.12+, Pillow, pytest + pytest-asyncio

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `harness/verification/metrics.py` | **新建** | 5 项指标计算 + 入口函数 |
| `tests/test_metrics.py` | **新建** | 覆盖 5 项指标 + 边界条件 |

为什么必须新建而非扩展现有文件：
- metrics 是纯计算模块，不与任何现有模块耦合（不 import harness 其他模块除了类型标注）
- 没有现有的"图像处理"文件可以挂靠——capturer.py 是截图获取，vision_agent.py 是 API 调用，interceptor.py 是拦截器链
- 新建一个 ~80 行的单文件比塞进已有的 500+ 行文件更清晰

---

### Task 1: 项目结构验证 + 测试文件骨架

**Files:**
- Create: `tests/test_metrics.py`

- [ ] **Step 1: 验证 Pillow 可用**

```bash
uv run python -c "from PIL import Image; print('Pillow OK')"
```
Expected: `Pillow OK`

- [ ] **Step 2: 写测试文件骨架**

```python
# tests/test_metrics.py
"""测试 compute_match_metrics — 5 项量化指标 + 边界条件."""

from __future__ import annotations

import pytest
from PIL import Image

from harness.verification.metrics import compute_match_metrics


# ---- 辅助：创建单色测试图像 ----

def _solid_image(r: int, g: int, b: int, w: int = 100, h: int = 80) -> Image.Image:
    return Image.new("RGB", (w, h), (r, g, b))


def _gradient_image(w: int = 100, h: int = 80) -> Image.Image:
    """左黑右白的水平渐变."""
    from PIL import ImageDraw
    img = Image.new("RGB", (w, h))
    for x in range(w):
        v = int(255 * x / (w - 1))
        for y in range(h):
            img.putpixel((x, y), (v, v, v))
    return img
```

- [ ] **Step 3: 验证 import 会报 ModuleNotFoundError（TDD 红色）**

```bash
uv run python -c "from harness.verification.metrics import compute_match_metrics"
```
Expected: `ModuleNotFoundError: No module named 'harness.verification.metrics'`

- [ ] **Step 4: Commit**

```bash
git add tests/test_metrics.py
git commit -m "test: add metrics test skeleton with helper factories"
```

---

### Task 2: Luminance（加权亮度均值）

**Files:**
- Create: `harness/verification/metrics.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: 写 luminance 测试（TDD 红色）**

在 `tests/test_metrics.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_metrics.py::TestLuminance -v
```
Expected: all FAIL (module not found or function not defined)

- [ ] **Step 3: 创建 metrics.py 并实现入口 + luminance**

```python
# harness/verification/metrics.py
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
    ref_contrast = _contrast(ref, ref_lum)
    cur_contrast = _contrast(cur, cur_lum)
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


# ---- 内部 ----

def _delta_pct(ref_val: float, cur_val: float) -> float:
    """cur 相对于 ref 的变化百分比。ref 为 0 时返回 0.0。"""
    if ref_val == 0.0:
        return 0.0
    return (cur_val - ref_val) / ref_val * 100.0


def _luminance(img: Image.Image) -> float:
    """逐像素加权亮度 0.299R + 0.587G + 0.114B → 全图均值."""
    pixels = list(img.getdata())
    total = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels)
    return total / len(pixels)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_metrics.py::TestLuminance -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: add compute_match_metrics entry + luminance indicator"
```

---

### Task 3: Contrast（RMS contrast）

**Files:**
- Modify: `tests/test_metrics.py`
- Modify: `harness/verification/metrics.py`

- [ ] **Step 1: 写 contrast 测试**

在 `tests/test_metrics.py` 追加：

```python
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
        # 渐变图有 RMS contrast > 0
        assert result["contrast"]["ref"] > 0
        assert result["contrast"]["cur"] == pytest.approx(0.0, abs=0.2)

    def test_higher_contrast_detected(self):
        ref = _solid_image(100, 100, 100)
        cur = _gradient_image()  # 有变化
        result = compute_match_metrics(ref, cur)
        # cur 的 RMS contrast > ref 的
        assert result["contrast"]["cur"] > result["contrast"]["ref"]
```

- [ ] **Step 2: 确认测试失败**

```bash
uv run pytest tests/test_metrics.py::TestContrast -v
```
Expected: FAIL (KeyError or NameError)

- [ ] **Step 3: 实现 _contrast**

在 `harness/verification/metrics.py` 的 `# ---- 内部 ----` 区域追加：

```python
def _contrast(img: Image.Image, mean_luminance: float | None = None) -> float:
    """RMS contrast — 加权亮度的全图标准差."""
    if mean_luminance is None:
        mean_luminance = _luminance(img)
    pixels = list(img.getdata())
    lum_values = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
    variance = sum((v - mean_luminance) ** 2 for v in lum_values) / len(lum_values)
    return math.sqrt(variance)
```

- [ ] **Step 4: 确认通过**

```bash
uv run pytest tests/test_metrics.py::TestContrast -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: add contrast (RMS) indicator"
```

---

### Task 4: Color Temperature（R/B 比）+ Saturation（HSV S 均值）

**Files:**
- Modify: `tests/test_metrics.py`
- Modify: `harness/verification/metrics.py`

- [ ] **Step 1: 写 color_temperature + saturation 测试**

```python
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
```

- [ ] **Step 2: 确认测试失败**

```bash
uv run pytest tests/test_metrics.py::TestColorTemperature tests/test_metrics.py::TestSaturation -v
```
Expected: FAIL

- [ ] **Step 3: 实现 _r_b_ratio + _saturation**

在 `harness/verification/metrics.py` 追加：

```python
def _r_b_ratio(img: Image.Image) -> float:
    """R̄ / B̄ 通道均值比。>1 偏暖，<1 偏冷。B 均值为 0 时返回 1.0."""
    pixels = list(img.getdata())
    r_sum = sum(r for r, g, b in pixels)
    b_sum = sum(b for r, g, b in pixels)
    if b_sum == 0:
        return 1.0
    return r_sum / b_sum


def _saturation(img: Image.Image) -> float:
    """HSV S 通道均值。0（灰）到 255（纯色）."""
    hsv = img.convert("HSV")
    pixels = list(hsv.getdata())
    # HSV 模式: (H, S, V)，S 在第二个位置
    s_sum = sum(s for h, s, v in pixels)
    return s_sum / len(pixels)
```

- [ ] **Step 4: 确认通过**

```bash
uv run pytest tests/test_metrics.py::TestColorTemperature tests/test_metrics.py::TestSaturation -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: add color_temperature (R/B ratio) + saturation (HSV S) indicators"
```

---

### Task 5: Histogram Correlation（余弦相似度）

**Files:**
- Modify: `tests/test_metrics.py`
- Modify: `harness/verification/metrics.py`

- [ ] **Step 1: 写 histogram_correlation 测试**

```python
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
        assert result["histogram_correlation"] < 0.1  # 完全不重叠

    def test_similar_distribution(self):
        ref = _solid_image(100, 100, 100)
        cur = _solid_image(110, 110, 110)
        result = compute_match_metrics(ref, cur)
        # 相近亮度但不同 bin，相关度仍较低
        assert result["histogram_correlation"] < 0.5
```

- [ ] **Step 2: 确认测试失败**

```bash
uv run pytest tests/test_metrics.py::TestHistogramCorrelation -v
```
Expected: FAIL

- [ ] **Step 3: 实现 _histogram_correlation**

在 `harness/verification/metrics.py` 追加：

```python
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
```

- [ ] **Step 4: 确认通过**

```bash
uv run pytest tests/test_metrics.py::TestHistogramCorrelation -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: add histogram_correlation (cosine similarity over 768-bin)"
```

---

### Task 6: 输入验证 + 边界条件

**Files:**
- Modify: `tests/test_metrics.py`
- Modify: `harness/verification/metrics.py`

- [ ] **Step 1: 写 _validate 边界测试**

```python
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
        with pytest.raises(ValueError, match="size"):
            compute_match_metrics(ref, cur)

    def test_size_mismatch_within_limit_ok(self):
        ref = Image.new("RGB", (100, 100), (0, 0, 0))
        cur = Image.new("RGB", (150, 150), (0, 0, 0))  # 1.5x < 2x
        result = compute_match_metrics(ref, cur)
        assert "luminance" in result  # 不抛异常

    def test_single_color_image(self):
        """单色图像：所有指标应可计算，histogram_correlation=1.0."""
        ref = _solid_image(42, 128, 200)
        cur = _solid_image(42, 128, 200)
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] == pytest.approx(1.0, abs=0.001)
        assert result["contrast"]["ref"] == pytest.approx(0.0, abs=0.1)
```

- [ ] **Step 2: 确认测试失败**

```bash
uv run pytest tests/test_metrics.py::TestValidation -v
```
Expected: FAIL (_validate not defined)

- [ ] **Step 3: 实现 _validate**

在 `harness/verification/metrics.py` 追加：

```python
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
            f"宽度差异过大: ref={ref.width}, cur={cur.width} (ratio={max_w/min_w:.1f}x > 2x)"
        )
    if min_h > 0 and max_h / min_h > 2.0:
        raise ValueError(
            f"高度差异过大: ref={ref.height}, cur={cur.height} (ratio={max_h/min_h:.1f}x > 2x)"
        )
```

- [ ] **Step 4: 运行全部 metrics 测试确认通过**

```bash
uv run pytest tests/test_metrics.py -v
```
Expected: all PASS (~14 tests)

- [ ] **Step 5: Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: add input validation + edge case tests for metrics"
```

---

### Task 7: 最终验证

- [ ] **Step 1: 运行完整测试套件确认无回归**

```bash
uv run pytest tests/ -v
```
Expected: 323+ passed (原 323 + 14 新 = ~337 passed)

- [ ] **Step 2: 最终 Commit**

```bash
git add harness/verification/metrics.py tests/test_metrics.py
git commit -m "feat: complete reference image metrics module (5 indicators + validation)"
```
