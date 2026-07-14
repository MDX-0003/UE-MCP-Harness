# match_reference 叫停机制 — 实施计划 v2

> 讨论记录: 2026-07-14 | 分析文档: `docs/tmp_issues/0713/analysis.md` — 问题 B
> v1 审查结论: PLAN_0713 存在优先级倒置、_session_reference Bug、SaveLevelAs 无阈值、LoadLevel 规格错误等问题，详见本文件末尾"v1→v2 变更记录"。

**目标：** 四层防线——(1) 达标检测 → (2) 连续下降检测 → (3) 最佳点追踪+自动快照 → (4) 硬终止兜底。另含 Skill 软收敛提示。

**技术栈：** Python 3.12+, interceptor 模式, UE LevelPersistenceToolset C++ 插件（七工具已完成）

---

## 前置条件：UE 端工具 ✅ 已完成

LevelPersistenceToolset 七工具（含 SaveLevelAs + LoadLevel）已实现并通过测试。
测试脚本: `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/test_saveas_load.py`

| 工具 | 参数 | 行为 |
|------|------|------|
| `SaveLevelAs` | `TargetPath: string` | 将当前关卡完整拷贝保存到新路径。编辑器继续编辑原文件不变。 |
| `LoadLevel` | `LevelPath: string`, `bSaveDirty: bool=false` | 加载指定关卡。bSaveDirty=true 时先静默保存脏包，bSaveDirty=false 时不弹对话框直接丢弃脏数据（UE LoadMap 承诺 "Does not prompt"）。 |

> ⚠ v1 计划写"弹 UE 原生确认对话框"——错误。实际实现基于 `FEditorFileUtils::LoadMap`，不弹对话框。

### 快照路径约定

Harness 端构造的快照路径格式：`{当前关卡目录}/{MMDD}-{关卡名}.umap`

如当前编辑 `/Game/Maps/Test.umap`，首次最佳时存入 `/Game/Maps/0714-Test.umap`。后续最佳时覆盖同一文件。LLM 永远只需知道这一个路径。

> 注意：跨天运行时日期前缀变化会创建新快照文件——旧快照不会自动清理，属已知限制。

### 全限定工具名

`LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs`
`LevelPersistenceToolset.LevelPersistenceToolset.LoadLevel`

---

## Harness 端：涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `harness/server.py` | Phase 0 Bug修复 + Phase 1 智能判断 + Phase 2 快照触发 + Phase 3 硬终止检查 + Session 重置 |
| 新增 | `harness/stop_limit.py` | `StopLimitInterceptor` + `build_summary`（硬终止兜底） |
| 修改 | `harness/cli.py` | 注册 StopLimitInterceptor |
| 新增 | `tests/test_stop_limit.py` | 硬终止 + SaveLevelAs 阈值 + Session 重置 + _session_reference 持久性 |
| 修改 | `skills/match-atmosphere.yaml` | 软收敛提示 |
| 修改 | `.claude/memory/level-persistence-toolset.md` | 标记 UE 工具完成 + 修正 LoadLevel 描述 |

---

## Phase 0（前置 PR）：修复 `_session_reference` 全量替换 Bug

**文件：** `harness/server.py`

**Bug 位置：** [server.py:557-558](harness/server.py#L557-L558)

```python
# 当前代码（有 Bug）：
_is_first_load = _session_reference.get("_loaded") is None
_session_reference = {"b64": ref_b64, "path": str(ref_path), "_loaded": True}
# ↑ 新 dict 替换——prev_metrics、metrics 等所有累积状态被丢弃
```

**修复：**

```python
# 修复后：
_is_first_load = _session_reference.get("_loaded") is None
_session_reference.update({"b64": ref_b64, "path": str(ref_path), "_loaded": True})
# ↑ mutate 而非 replace——保留所有累积状态
```

> 此 Bug 导致 `_build_trend_summary`（line 573-578）从未真正工作——每轮 line 558 新建 dict，`prev_metrics` 在下一轮被擦除。

**验证：** 运行现有全量测试确认无回归。

```bash
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

---

## Phase 1：智能判断注入 match_reference 输出（主防线）

**文件：** `harness/server.py`

**原则：** 纯在 `match_reference` 返回值中追加判断性文字——不新增工具、不改变 Skill 流程、最低侵入。

### 1a. Session 重置检测

在 `_is_first_load` 之后（line 557 附近），检测是否换了参考图：

```python
_is_first_load = _session_reference.get("_loaded") is None
_prev_path = _session_reference.get("path", "")
_is_new_reference = (str(ref_path) != _prev_path)

if _is_new_reference and not _is_first_load:
    # 换了参考图 → 重置所有累积状态（保留 _loaded 标记）
    _session_reference = {"_loaded": True}
    _is_first_load = True  # 对新参考图而言是首次
```

### 1b. 达标检测（第一层防线）

量化指标计算完成后（`metrics_result` 可用时），在 MiMo 输出之后、量化指标表格之前插入：

```python
if metrics_result:
    hist = metrics_result["histogram_correlation"]
    rb_cur = metrics_result["color_temperature"]["cur_r_b_ratio"]
    rb_ref = metrics_result["color_temperature"]["ref_r_b_ratio"]
    rb_delta = abs(rb_cur - rb_ref) / rb_ref if rb_ref else float("inf")

    if hist > 0.85 and rb_delta < 0.15:
        lines.append("")
        lines.append("✅ 量化指标已达到良好匹配范围（直方图>0.85, R/B偏差<15%），"
                     "建议停止调整，调 deactivate_skill 退出。")
```

**阈值依据（analysis.md 数据）：**
- `hist>0.85`: R7=0.85 是全程最佳，肉眼判断"已经够了"
- `R/B偏差<15%`: R7 时 R/B=1.41 vs ref=1.24，偏差约 14%，处于可接受范围

### 1c. 最佳点追踪 + 连续下降检测（第二/三层防线）

在量化指标表格输出之后插入：

```python
if metrics_result:
    hist = metrics_result["histogram_correlation"]
    best = _session_reference.get("best_metrics")

    # 最佳点追踪
    if best is None or hist > best.get("histogram_correlation", 0):
        _session_reference["best_metrics"] = {
            "histogram_correlation": hist,
            "round": _session_reference.get("_match_count", 0),
            "rb_ratio": metrics_result["color_temperature"]["cur_r_b_ratio"],
        }
        lines.append("")
        lines.append(f"🏆 新最佳记录：直方图相似度 {hist:.2f}（第 "
                     f"{_session_reference['best_metrics']['round']} 轮）")
    elif best is not None:
        best_hist = best.get("histogram_correlation", 0)
        best_round = best.get("round", "?")
        lines.append("")
        lines.append(f"⚠ 当前直方图相似度 {hist:.2f} 低于最佳记录 "
                     f"{best_hist:.2f}（第 {best_round} 轮）。"
                     f"可能已越过最佳点，考虑回退到上一轮参数。")

    # 连续下降检测
    prev_hist = _session_reference.get("_prev_hist")
    if prev_hist is not None and hist < prev_hist:
        _decline = _session_reference.get("_decline_count", 0) + 1
        _session_reference["_decline_count"] = _decline
        if _decline >= 2:
            lines.append("🔴 直方图相似度连续 2 轮下降，"
                         "建议回退到上一轮参数并停止该方向调整。")
    else:
        _session_reference["_decline_count"] = 0
    _session_reference["_prev_hist"] = hist
```

**阈值依据：**
- 连续下降=2: analysis.md 中 R8→R9→R10 滑坡模式，给 1 轮容错（R7→R8 的 -0.01 可能是噪声）

---

## Phase 2：最佳状态快照（SaveLevelAs 自动触发）

**文件：** `harness/server.py`

### 阈值设计（基于 analysis.md 10 轮数据）

| 条件 | 值 | 数据依据 |
|------|:--:|------|
| 首次调用 | 始终保存 | 建立基线，可回退 |
| 历史首次达标 | hist ≥ 0.75 | 0.64→0.82 的大跳跃被捕获；0.75 以下的质量不值得快照 |
| 后续改善 | Δhist ≥ 0.03 | 正常振荡步长 ±0.02-0.04（R3→R5 恢复阶段）；0.03 过滤噪声同时捕获 R6→R7 (+0.03) 的真实改善 |

**应用到本次 session 的效果：**
R1(0.66,首次)→保存基线 / R2-R5(<0.75)→跳过 / R6(0.82,首次达标)→保存 / R7(0.85,+0.03)→保存。共 3 次（vs 无阈值全保存 7 次）。

### 实现

在 Phase 1c 的"新最佳记录"检测块中追加快照逻辑：

```python
    # 最佳点追踪（Phase 1c 代码块内）
    if best is None or hist > best.get("histogram_correlation", 0):
        _session_reference["best_metrics"] = {...}  # Phase 1c

        # Phase 2: 快照阈值判断
        should_save = False
        if best is None:
            should_save = True  # 首次调用始终保存基线
        elif hist >= 0.75 and (hist - best.get("histogram_correlation", 0)) >= 0.03:
            should_save = True
        elif hist >= 0.75 and best.get("histogram_correlation", 0) < 0.75:
            should_save = True  # 首次跨过 0.75 门槛

        if should_save:
            snapshot_path = await _ensure_best_snapshot_path(ue_client, _session_reference)
            try:
                await ue_client.call_tool(
                    "LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs",
                    {"TargetPath": snapshot_path},
                )
                _session_reference["best_snapshot_path"] = snapshot_path
                lines.append(f"💾 最佳状态已保存至关卡快照: {snapshot_path}")
            except Exception as e:
                logger.warning("SaveLevelAs 失败: %s", e)
```

`_ensure_best_snapshot_path` 辅助函数（在 `_get_jsonl_path` 附近定义）：

```python
async def _ensure_best_snapshot_path(
    ue_client, session_ref: dict,
) -> str:
    """构造快照路径: {当前关卡目录}/{MMDD}-{关卡名}.umap。

    首次调用时查询 UE 获取当前关卡路径，后续复用缓存。
    Session 重置（换参考图）时缓存被清空，重新查询。
    """
    cached = session_ref.get("_snapshot_base_path")
    if cached:
        return cached

    try:
        result = await ue_client.call_tool(
            "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
            {"LevelPath": ""},
        )
        import json as _json
        from harness.server import _parse_raw_result, _extract_parsed_text
        parsed = _parse_raw_result(result)
        text = _extract_parsed_text(parsed, result) or ""
        rv = _json.loads(text) if isinstance(text, str) else {}
        pkg_path = rv.get("packagePath", "") if isinstance(rv, dict) else ""
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

    session_ref["_snapshot_base_path"] = snapshot_path
    return snapshot_path
```

---

## Phase 3：硬终止兜底（StopLimitInterceptor）

**文件：** 新增 `harness/stop_limit.py`，修改 `harness/server.py`，修改 `harness/cli.py`

**定位：** 最后一层保险——仅在 Phase 1 的达标/下降检测全部失效时触发。

### 3a. StopLimitInterceptor

```python
"""match_reference 调用次数硬限制——兜底机制。

当 match_reference 在同一参考图上调用超过 10 次时，
在 pre_call 阶段拦截，从结构化 session 状态组装回顾摘要。
"""

from __future__ import annotations

import logging

from harness.interceptor import ToolCallInterceptor

logger = logging.getLogger("harness.stop_limit")

_MAX_MATCH_REFERENCE_CALLS = 10


class StopLimitInterceptor(ToolCallInterceptor):
    """match_reference 调用次数硬限制（兜底）。

    计数器 _session_reference["_match_count"] 由 server.py 维护。
    不依赖 JSONL 文本解析——直接从 _session_reference 结构化数据构建摘要。
    """

    def build_summary(
        self, session_ref: dict, best_snapshot_path: str | None = None,
    ) -> str:
        """从 session_ref 结构化数据组装回顾摘要。"""
        match_count = session_ref.get("_match_count", 0)
        best = session_ref.get("best_metrics")
        prev_hist = session_ref.get("_prev_hist")

        lines = [f"⛔ 已完成 {match_count} 轮 match_reference 迭代。", ""]

        if best_snapshot_path:
            lines.append(f"🏆 最佳状态已保存至关卡快照: {best_snapshot_path}")
            lines.append(f'   如需恢复，调 LoadLevel("{best_snapshot_path}")。')
            lines.append("")

        if best:
            lines.append("指标轨迹：")
            lines.append(f"    最佳 (第{best.get('round', '?')}轮): "
                         f"直方图={best.get('histogram_correlation', '?'):.2f}, "
                         f"R/B={best.get('rb_ratio', '?')} 🏆")
            if prev_hist is not None:
                lines.append(f"    最终 (第{match_count}轮): "
                             f"直方图={prev_hist:.2f}")
            lines.append("")

        lines.append("请向用户确认是否继续调整，或调 deactivate_skill 退出。")
        return "\n".join(lines)
```

> ⚠ v1 的 `_build_trajectory` / `_build_round_summaries` 从 JSONL 文本中用正则反解析指标——脆弱且依赖格式不变。v2 直接从 `_session_reference` 结构化数据构建摘要，零解析。

### 3b. server.py 硬终止检查

在 `match_reference` handler 的参数解析之后、参考图加载之前：

```python
if name == "match_reference":
    t0 = time.monotonic()
    ref_path_str = arguments.get("path", "")

    # 硬终止检查（Phase 3——最后一层防线）
    _match_count = _session_reference.get("_match_count", 0)
    if _match_count >= _MAX_MATCH_REFERENCE_CALLS and _stop_limit is not None:
        best_path = _session_reference.get("best_snapshot_path")
        summary = _stop_limit.build_summary(_session_reference, best_path)
        duration_ms = (time.monotonic() - t0) * 1000
        await _log_harness_call(name, arguments, summary, duration_ms)
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            isError=True,
        )
```

计数递增放在 `_session_reference.update(...)` 之后、量化指标计算之前：

```python
    _session_reference.update({"b64": ref_b64, "path": str(ref_path), "_loaded": True})

    # 递增 match_reference 调用计数
    _match_count = _session_reference.get("_match_count", 0) + 1
    _session_reference["_match_count"] = _match_count
```

### 3c. cli.py 注册

```python
from harness.stop_limit import StopLimitInterceptor

_stop_limit = StopLimitInterceptor()

interceptors: list[ToolCallInterceptor] = [
    DebugPreCallInterceptor(),
    ReadbackInterceptor(ue_client, _cache),
    _stop_limit,
    tool_logger,
    ...
]
```

传递 `_stop_limit` 给 `build_server(stop_limit=_stop_limit)`。

### 3d. build_server 签名扩展

```python
def build_server(
    ...
    stop_limit: "StopLimitInterceptor | None" = None,
    ...
):
```

闭包中 `nonlocal _stop_limit` + `_stop_limit = stop_limit`。

---

## Phase 4：Skill 软收敛提示

**文件：** `skills/match-atmosphere.yaml`

在"提示"部分追加：

```yaml
  - 每轮 match_reference 后，先查看量化指标中的"最佳记录"和"达标检测"提示。
    看到 ✅ 达标提示 → 当前状态已良好，本轮调整即为最后一轮微调——调完即止。
  - 看到 ⚠ 低于最佳记录 → 不要继续当前方向，考虑回退参数。
  - 看到 🔴 连续下降 → 立即停止当前方向，回退到上一轮参数。
  - 连续 2 轮 match_reference 的直方图相似度、R/B 比值、饱和度变化幅度均 < 5%
    → 已收敛，调 deactivate_skill 退出。
  - 上述阈值为经验值。如初始偏差极大（如夜间 vs 白天），
    自行根据初始偏差大小放宽阈值。
```

---

## Phase 5：测试

**文件：** 新增 `tests/test_stop_limit.py`

### 测试矩阵

| 测试 | 层级 | 验证内容 |
|------|:--:|------|
| `test_session_reference_not_replaced` | 集成 | Phase 0：`_session_reference` 使用 update 而非 replace，prev_metrics 跨轮存活 |
| `test_save_level_as_with_threshold` | 集成 | Phase 2：hist<0.75 不触发 SaveLevelAs，hist≥0.75+Δ≥0.03 触发 |
| `test_save_level_as_skip_small_improvement` | 集成 | Phase 2：Δhist=0.01 不触发保存 |
| `test_hard_stop_11th_call` | 单元 | Phase 3：第 11 次 match_reference 返回 isError=True |
| `test_session_reset_on_new_reference` | 集成 | Phase 1a：换参考图后 _match_count 归零、best_metrics 清空 |
| `test_best_metrics_persist_across_calls` | 集成 | Phase 1c：best_metrics 跨多轮累积不被覆盖 |

### 集成测试原则

集成级测试使用真实 PIL 图片 + 真实 `compute_match_metrics`，至少覆盖核心数据流路径。不 mock 整个 match_reference handler。

```python
class TestSessionReferencePersistence:
    """Phase 0: 验证 _session_reference 不被全量替换。"""

    @pytest.mark.asyncio
    async def test_prev_metrics_survives_across_calls(self, tmp_path: Path):
        """prev_metrics 应在第 2 轮 match_reference 中可用。"""
        # 使用真实 PIL 图片 → 真实 metrics → 验证 trend_lines 非空
        ...
```

```bash
uv run pytest tests/test_stop_limit.py -v
```

预期：6 passed。

---

## 最终验证

```bash
# 全量回归
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py

# 语法检查
uv run python -c "import ast; ast.parse(open('harness/stop_limit.py', encoding='utf-8').read()); print('OK')"
uv run python -c "import ast; ast.parse(open('harness/server.py', encoding='utf-8').read()); print('OK')"
```

---

## 自审清单

1. **防线分层（按触发时机排序）：**
   - [x] Phase 1b: 达标检测（hist>0.85 且 R/B偏差<15% → 建议停止）——最早触发
   - [x] Phase 1c: 连续下降检测（2轮下降 → 警告回退）
   - [x] Phase 1c: 最佳点追踪（每轮标注当前 vs 历史最佳）
   - [x] Phase 2: 自动快照（hist≥0.75 且 Δ≥0.03 → SaveLevelAs）
   - [x] Phase 3: 硬终止（10 次 → 兜底拦截）
   - [x] Phase 4: Skill 软收敛（提示 LLM 自行收敛）

2. **Bug 修复：**
   - [x] Phase 0: `_session_reference = {...}` → `_session_reference.update({...})`
   - [x] Phase 1a: 换参考图时 Session 状态重置
   - [x] Phase 3a: build_summary 从结构化数据构建（不再解析 JSONL 文本）

3. **阈值有数据依据（analysis.md 10轮轨迹）：**
   - [x] hist≥0.75: R6=0.82 是 session 中首次"好"结果
   - [x] Δhist≥0.03: 正常振荡步长 ±0.02-0.04，0.03 过滤噪声
   - [x] 连续下降=2: R8→R9→R10 滑坡模式，给 1 轮容错
   - [x] 硬终止=10: session 中 10 轮后即使出现过最优也滑坡到 0.64

4. **规格修正：**
   - [x] LoadLevel: "弹对话框"→"不弹对话框，bSaveDirty 控制"
   - [x] UE 工具: "需新增"→"✅ 已完成"

5. **影响分析：**
   - 修改 server.py：~100 行（Phase 0-3，含 Bug 修复 + 4 段判断注入 + 快照触发 + 硬终止入口 + Session 重置）
   - 新增 stop_limit.py：~60 行（Phase 3a，简化版——从结构化状态构建摘要）
   - 修改 cli.py：~5 行（注册 + 传参）
   - 新增 test_stop_limit.py：~200 行（含集成测试）
   - 修改 match-atmosphere.yaml：~10 行
   - 修改 level-persistence-toolset.md：~5 行（修正 LoadLevel 描述 + 标记完成）
   - 不影响现有 331 测试

---

## v1→v2 变更记录

| # | v1 问题 | v2 修正 | 讨论结论 |
|:--:|------|------|------|
| 1 | 硬终止=10 排第一优先级 | Phase 3 兜底；达标检测+连续下降为主防线 | 认可 |
| 2 | `_session_reference = {...}` 替换 Bug | Phase 0 前置修复 → `update()` | 前置 PR |
| 3 | SaveLevelAs 每轮最佳都触发 | Phase 2: hist≥0.75 且 Δ≥0.03 | 经验阈值 |
| 4 | 无 Session 重置机制 | Phase 1a: 新参考图路径时重置 | 认可 |
| 5 | build_summary 从 JSONL 文本正则解析 | Phase 3a: 从结构化 `_session_reference` 构建 | — |
| 6 | LoadLevel "弹对话框" | 修正为 "不弹对话框" | 错误规划 |
| 7 | UE 工具标"需新增" | 标"✅ 已完成" | 已完成 |
| 8 | 测试 4 层 deep mock | Phase 5: 增加集成级测试 | — |
| 9 | 快照路径日期前缀跨天行为未说明 | 文档备注已知限制 | — |
