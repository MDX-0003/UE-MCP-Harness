# match_reference 叫停机制 — 实施计划 v3

> 讨论记录: 2026-07-14 | 分析日志: `6c81ae0340b0e5acdd9489a5a4b7aba8/tool_calls.jsonl`
> v2→v3: 达标阈值 0.85 降为 0.70 倒计时机制 + SaveLevelAs 不再切换关卡 + 轮次感知 + LoadLevel 措辞修正

**核心设计：** 直方图≥0.70 后给 LLM 精确 3 轮倒计时——"你已接近目标，限 3 次内收敛"，到期硬切断。10 轮总上限保留为兜底。

---

## 机制总览

```
match_reference 调用
    │
    ├─ 检查 1: 倒计时已归零？ → pre_call 硬拦截（isError=True）
    ├─ 检查 2: 总轮次 > max(10, 达成轮次+3)？ → pre_call 硬拦截
    │
    ├─ 执行 match_reference（截图 + MiMo + 量化指标）
    │
    ├─ hist ≥ 0.70 且倒计时未激活？ → 激活倒计时=3
    ├─ 输出中加入倒计时提示（剩余 N 次）
    ├─ hist 首次跨过 0.70？ → SaveLevelAs 快照（bOpenSavedLevel=false）
    │
    └─ 返回 LLM
```

**两层硬限制：**

| 层级 | 触发条件 | 上限 |
|------|------|:--:|
| 倒计时切断 | hist≥0.70 达成后倒计时归零 | 达成轮 + 3 |
| 总轮次兜底 | 始终未达标，轮次耗尽 | 10 轮 |

**总上限公式：** `max(10, countdown_start_round + 3)`

| 达成时机 | 总上限 | 说明 |
|:--:|:--:|------|
| R1 达成 0.75 | 4 | LLM 只有 R2/R3/R4 三轮微调 |
| R8 达成 0.71 | 11 | max(10, 8+3) |
| R10 达成 0.72 | 13 | max(10, 10+3) |
| 始终未达 0.70 | 10 | 10 轮后硬终止兜底 |

---

## 前置条件：UE 端 ✅

LevelPersistenceToolset 七工具已完成。SaveLevelAs 新增参数：

| 参数 | 类型 | 默认 | 行为 |
|------|------|:--:|------|
| `TargetPath` | string | 必填 | 快照目标路径 |
| `bOpenSavedLevel` | bool | false | false=存快照+同步存回原关卡，编辑器留在原关卡 |

`bOpenSavedLevel=false` 是关键——SaveLevelAs 不会像 v2 那样静默切换编辑器当前关卡。

---

## Harness 端：涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `harness/server.py` | 倒计时逻辑 + SaveLevelAs 阈值修改 + 轮次显示 + LoadLevel 措辞 |
| 修改 | `harness/stop_limit.py` | build_summary 措辞修正（LoadLevel 仅用于回退） |
| 修改 | `harness/cli.py` | （v2 已注册，无需改动） |
| 修改 | `tests/test_stop_limit.py` | 倒计时测试 + SaveLevelAs 阈值测试 + 轮次显示测试 |
| 修改 | `skills/match-atmosphere.yaml` | 倒计时提示 + 收敛建议 |
| 删除 | `docs/plans/PLAN_0714_stop_mechanisms_v2.md` | 被 v3 取代 |

---

## Task 1：倒计时机制（核心改动）

**文件：** `harness/server.py`

### 1a. 倒计时状态管理

在 `_session_reference` 中新增三个键：

```
_countdown_remaining: int | None   # None=未激活, 3/2/1/0=倒计时中
_countdown_start_round: int        # 倒计时激活时的轮次
_max_allowed_rounds: int           # 动态总上限
```

### 1b. pre_call 硬拦截（在 match_reference handler 入口）

在现有 10 轮硬终止检查处，合并倒计时检查：

```python
if name == "match_reference":
    t0 = time.monotonic()
    ref_path_str = arguments.get("path", "")

    # 硬终止检查：倒计时归零 或 总轮次超限
    _match_count = _session_reference.get("_match_count", 0)
    _max_allowed = _session_reference.get("_max_allowed_rounds", 10)
    _countdown = _session_reference.get("_countdown_remaining")

    should_stop = False
    stop_reason = ""

    if _countdown is not None and _countdown <= 0:
        should_stop = True
        stop_reason = f"倒计时已归零（hist≥0.70 达成后已用尽 3 次调整机会）"
    elif _match_count >= _max_allowed:
        should_stop = True
        stop_reason = f"已达到最大轮次限制（{_max_allowed} 轮）"

    if should_stop and _stop_limit is not None:
        best_path = _session_reference.get("best_snapshot_path")
        summary = _stop_limit.build_summary(
            _session_reference, best_path, stop_reason,
        )
        duration_ms = (time.monotonic() - t0) * 1000
        await _log_harness_call(name, arguments, summary, duration_ms)
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            isError=True,
        )
```

### 1c. 倒计时激活（量化指标计算后）

```python
if metrics_result:
    hist = m["histogram_correlation"]
    _countdown = _session_reference.get("_countdown_remaining")

    # 倒计时激活条件：hist ≥ 0.70 且倒计时尚未激活
    if _countdown is None and hist >= 0.70:
        _session_reference["_countdown_remaining"] = 3
        _session_reference["_countdown_start_round"] = _match_count
        _session_reference["_max_allowed_rounds"] = max(10, _match_count + 3)

    # 倒计时递减（每轮 -1）
    if _session_reference.get("_countdown_remaining") is not None:
        _session_reference["_countdown_remaining"] -= 1
```

> **注意递减顺序：** 激活后在本轮立即 -1，即"第 1 次调整机会"从下一轮开始。激活轮本身不算在 3 次内——激活时 `_countdown_remaining` 先设为 3，然后 -1 → 2，表示"还有 2 次机会"。

### 1d. 倒计时输出

在 match_reference 输出 header 中显示动态轮次信息：

```python
_max_allowed = _session_reference.get("_max_allowed_rounds", 10)
_countdown = _session_reference.get("_countdown_remaining")

if _countdown is not None and _countdown >= 0:
    lines = [
        f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
        f"第 {_match_count} 轮（最多 {_max_allowed} 轮，"
        f"⏳ 直方图已达 0.70+，剩余 {_countdown} 次调整机会）",
    ]
elif _countdown is not None and _countdown < 0:
    lines = [
        f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
        f"第 {_match_count} 轮（最多 {_max_allowed} 轮，"
        f"⏳ 调整机会已用完，本轮为最后一轮）",
    ]
else:
    lines = [
        f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
        f"第 {_match_count} 轮（最多 {_max_allowed} 轮）",
    ]
```

### 1e. 移除旧达标检测

删除 v2 的 `if hist > 0.85 and rb_delta < 0.15: lines.append("✅ ...")` 逻辑块——被倒计时机制取代。

**保留：** 最佳点追踪（🏆/⚠）、连续下降检测（🔴）、SaveLevelAs（修改后）。

---

## Task 2：SaveLevelAs 阈值 + 参数修改

**文件：** `harness/server.py`

### 2a. 触发条件修改

**v2 逻辑（删除）：**
```python
if best is None:
    should_save = True  # 首次调用就存（导致 hist=0.51 的基线也被保存）
elif hist >= 0.75 and delta >= 0.03:
    should_save = True
```

**v3 逻辑：**
```python
should_save = False
if hist >= 0.70 and not _session_reference.get("_snapshot_saved"):
    should_save = True  # 仅在 hist 首次跨过 0.70 时保存一次
    _session_reference["_snapshot_saved"] = True
```

> 理由：快照的唯一目的是保留一个"足够好"的关卡副本供人类回退。一次即可。hist 从 0.70→0.80 的改善不值得再存一份——它们都是在同一关卡文件上的增量修改。

### 2b. 调用参数

```python
await ue_client.call_tool(
    "LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs",
    {"TargetPath": snapshot_path},  # bOpenSavedLevel 默认 false
)
```

### 2c. 快照消息

```python
lines.append(
    f"💾 快照已保存至 {snapshot_path}，编辑器仍在原关卡。"
    f"仅当需要回退错误操作时才调 LoadLevel，请勿无故加载快照。"
)
```

---

## Task 3：StopLimitInterceptor 措辞修正

**文件：** `harness/stop_limit.py`

### 3a. build_summary 签名扩展

```python
def build_summary(
    self, session_ref: dict,
    best_snapshot_path: str | None = None,
    stop_reason: str = "",
) -> str:
```

### 3b. LoadLevel 措辞

```python
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
```

### 3c. 原因说明

```python
lines = [f"⛔ {stop_reason}", ""]
```

---

## Task 4：Skill 软收敛提示更新

**文件：** `skills/match-atmosphere.yaml`

```yaml
  - **收敛判断：** 每轮 match_reference 查看轮次信息中的倒计时提示。
    看到 ⏳ "剩余 N 次调整机会" → 倒计时中，优先调整影响最大的参数，勿浪费机会在微调上。
    看到 ⏳ "本轮为最后一轮" → 本轮即为最终轮，调完后必须 deactivate_skill 退出。
    看到 🏆 新最佳记录 → 方向正确，继续。⚠ 低于最佳 → 考虑回退。🔴 连续下降 → 立即回退。
  - 连续 2 轮 match_reference 的直方图相似度、R/B 比值、饱和度变化幅度均 < 5%
    → 已收敛，调 deactivate_skill 退出。
```

---

## Task 5：测试更新

**文件：** `tests/test_stop_limit.py`

### 测试矩阵

| 测试 | 验证内容 |
|------|------|
| `test_countdown_activates_at_hist_70` | hist≥0.70 时倒计时激活，_max_allowed 更新为 max(10, round+3) |
| `test_countdown_decrements_each_round` | 倒计时每轮 -1，归零后硬拦截 |
| `test_countdown_hard_stop` | 倒计时归零后 match_reference 返回 isError=True |
| `test_r1_achieve_70_max_4_rounds` | R1 达成 → 最多 4 轮，R5 被拦截 |
| `test_no_countdown_if_below_70` | 始终未达 0.70 → 10 轮后硬终止兜底 |
| `test_save_level_as_only_once` | hist≥0.70 首次跨过时保存，后续即使 hist 更高也不再保存 |
| `test_round_display_shows_countdown` | 输出中包含"剩余 N 次调整机会" |
| `test_build_summary_loadlevel_wording` | 硬终止摘要中 LoadLevel 措辞正确 |
| `test_build_summary_no_snapshot` | 无快照时不输出恢复提示 |

预期：9 passed。

---

## Task 6：清理

- [ ] 删除 `docs/plans/PLAN_0714_stop_mechanisms_v2.md`
- [ ] 运行全量回归：`uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py`

---

## 变更总结

| v2 | v3 | 理由 |
|------|------|------|
| hist>0.85 → ✅ 达标提示 | hist≥0.70 → 激活 3 轮倒计时 | 0.85 两轮 session 仅一次触发，0.70 更务实 |
| 达标检测纯文本提示 | pre_call 硬拦截 | 到期必切，不依赖 LLM 自觉 |
| SaveLevelAs 每轮新最佳都存 | 仅 hist 首次 ≥0.70 存一次 | 减少 I/O + 快照唯一目的=存档点 |
| SaveLevelAs 无 bOpenSavedLevel | 默认 false，编辑器留在原关卡 | 修复关卡静默切换问题 |
| LoadLevel "如需恢复" | "仅回退错误操作时用" | 防止 LLM 无故加载快照 |
| 输出无轮次信息 | 动态显示 "第 N 轮（最多 M 轮）" | LLM 可感知进度 |
| 达标检测 | 删除，被倒计时取代 | 功能冗余 |
| 10 轮硬终止 | 保留为兜底（未达标时） | 保底安全网 |

---

## 自审清单

- [x] 倒计时硬拦截在 pre_call 阶段（不浪费截图/MiMo/Vision 调用）
- [x] 倒计时归零和总轮次超限使用同一拦截路径
- [x] SaveLevelAs 仅在首次 ≥0.70 时触发一次
- [x] bOpenSavedLevel 默认 false——编辑器不切换关卡
- [x] 快照消息明确告知编辑器状态
- [x] LoadLevel 措辞限缩为"仅回退用"
- [x] 轮次信息动态反映倒计时状态
- [x] Session 重置（换参考图）时清空倒计时状态（Phase 1a 逻辑复用）
- [x] 10 轮兜底在始终未达标时仍生效
- [x] R1 达成 → max 4 轮的极端情况正确处理
