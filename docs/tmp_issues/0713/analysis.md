# Session 678ecb45 问题分析

> 2026-07-13 | 日志见 `docs/tmp_issues/0713/logs/tool_calls.jsonl`

---

## 问题 A：find_actors 的 Input Validation Error 未被日志记录

### 现象

LLM 在 Line 3 之前还有一次**未出现在 JSONL 中的调用**。LLM 端观察到的流程：

1. LLM 调用 `find_actors({"glob": "*Light*"})` — **缺少必填参数 `tag`**
2. MCP 返回 `"Input validation error: 'tag' is a required property"`
3. LLM 自行推断："The tag parameter is required... Let me try with an empty string"
4. LLM 重试 `find_actors({"glob": "*Light*", "tag": ""})` → **成功**（Line 3）

但 JSONL 只记录了第 4 步的成功调用，第 1 步的失败调用完全消失。

### 根因

MCP SDK 的参数校验（`inputSchema.required`）发生在 `call_tool` handler **之前**。当 LLM 发送缺少 `required` 字段的请求时，SDK 直接在传输层拒绝，返回 `isError=True`。这跳过了 Harness 的 `call_tool` → `interceptor chain` → `ToolCallLogger` 路径，因此不被记录。

### 影响

- **调试盲区**：无法从日志中看到 LLM 遇到了什么阻碍、如何自行纠正的。只能从 LLM 的思维链（如果记录了）中推断。
- **错误模式不可见**：如果 `tag` 参数命名持续误导 LLM，这个问题在日志中完全不可见——只有成功的调用被记录。
- **Skill compliance 无法验证**：无法判断 LLM 是否因为工具调用失败而偏离了 Skill 流程。

### 优化方向

1. **Harness 层拦截 validation error**：在 MCP Server 的 error handler 中捕获 `isError=True` 的响应，写入独立的 `tool_errors.jsonl`。
2. **工具描述改进**：`find_actors` 的 `tag` 参数描述应明确说明"不是类名匹配，是 Actor Tag 组件匹配"。需要 UE 侧配合或 Harness 在 tools/list 阶段注入描述覆盖。
3. **Skill 内提供精确调用示例**：在 match-atmosphere Skill 的 Step 1 中明确写出 `find_actors({glob: "*Light*", tag: ""})` 的正确参数组合。

---

## 问题 B：LLM 收敛后滑坡——在错误中循环

### 关键数据点：逐轮量化指标

| 轮次 | Line | R/B 比值 (ref=1.2387) | 饱和度 (ref=62.0) | 亮度 (ref=114.4) | 直方图相似度 | 方向 |
|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 初始 | 2 | 1.0982 | 35.2 | 158.7 | 0.73 | — |
| R1 | 33 | **1.2764** ✅ | **56.9** ✅ | 163.2 | 0.66 | R/B 接近参考 |
| R2 | 36 | 3.184 ❌ | 170.4 ❌ | 133.9 | 0.54 | 严重过热！ |
| R3 | 40 | 2.8022 | 167.8 | 147.3 | 0.57 | 仍然过热 |
| R4 | 44 | 2.4601 | 154.9 | 156.3 | 0.60 | 开始回落 |
| R5 | 47 | **1.4763** ✅ | 85.8 | 163.4 | 0.64 | 恢复中 |
| R6 | 51 | 1.4942 | 85.8 | 144.7 | 0.82 ⬆ | 大幅改善 |
| **R7** | **54** | **1.4077** | **73.1** | **122.9** | **0.85** 🏆 | **最佳点！** |
| R8 | 58 | 1.5435 | 89.7 | 120.4 | 0.84 ⬇ | 开始滑坡 |
| R9 | 62 | 1.2866 | 61.1 | 159.5 | 0.66 ⬇ | 崩溃 |
| R10 | 66 | 1.2513 | 55.8 | 159.2 | 0.64 ⬇ | 继续恶化 |

### 收敛→滑坡的时间线

**R1 (Line 33)：LLM 第一次调整就接近了目标。** DirectionalLight 设为 intensity=3.5, temp=4000K, LightColor=(1,0.75,0.35)。R/B=1.28，仅比参考值 1.24 高 3%。饱和度 56.9，比参考值 62 低 8%。这是非常好的第一轮结果。

**但 LLM 没有停下。** Skill 流程说"逐个组件调整"，LLM 继续调了 SkyAtmosphere 和 Fog。第二轮 match_reference 显示 R/B 从 1.28 跳到 3.18——SkyAtmosphere 的 Rayleigh 暖色设定和 Fog 的暖色 inscattering 叠加，把色温推向了极端。

**R2→R7：LLM 在振荡中逐渐找回方向。** 通过反向调整温度、降低 intensity，直方图相似度稳步回升到 0.85。这是全程最佳结果——亮度仅偏离 7%，R/B 偏差仅 14%。

**R7→R10：LLM 越过最佳点，继续调整。** 后续的 `set_properties` 操作（降低 intensity、调整 Fog、调整 SkyAtmosphere aerial perspective）把直方图相似度从 0.85 一路打到 0.64。

### 根因

1. **LLM 不知道"最好"是什么**：没有机制告诉 LLM "当前是最佳结果，可以停了"。LLM 依赖 MiMo 的定性描述（"色温更冷"/"对比度更低"），但 MiMo 的反馈是相对的、无记忆的——不知道上一轮更好还是这一轮更好。

2. **Skill 流程是"逐个组件全调完"**：没有"满意度检查"步骤。LLM 调完 4 个组件后自然继续微调，没有止损逻辑。

3. **MiMo 反馈存在漂移**：Line 54 (R7) 的 MiMo 说"色温相似 (similar)"——这应该是一个"已经够了"的信号。但 LLM 选择了继续调整 DirectionalLight intensity 和 fog density，而不是停下。

4. **没有 hard stop 机制**：Harness 没有在指标超过阈值时主动拦截——比如直方图相似度 >0.85 时告诉 LLM"已达到良好匹配"。

### Harness 端可以考虑的叫停/恢复机制

| 机制 | 触发条件 | 行为 | 实现难度 |
|------|------|------|:--:|
| **最佳点追踪** | 每次 match_reference 后对比"当前 vs 历史最佳"直方图相似度 | 输出中标注 🏆 最佳记录 + "当前指标体系已劣于第 N 轮最佳结果，考虑回退" | 低 |
| **收敛检测** | 连续 2 轮直方图相似度下降 | 输出中追加 "⚠ 指标连续下降，可能已越过最佳点。建议回退到上一轮参数。" | 低 |
| **达标阈值** | 直方图相似度 >0.85 且 R/B 偏差 <15% | 输出中追加 "✅ 指标已达到良好匹配范围。考虑停止调整。" | 低 |
| **发散报警** | R/B 偏差或饱和度偏差 >100% | 输出中追加 "🔴 R/B比值严重偏离参考值。当前调整方向可能完全错误，建议回退到初始参数。" | 低 |
| **自动快照** | 每次达到新的最佳直方图相似度 | 保存当前组件参数到 `_session_reference["best_state"]`，LLM 可请求恢复 | 中 |
| **硬限制** | 同一组件连续 3 轮 vision_compare/miMo 返回 worse | 输出 "stop" + 阻止继续调整该组件 | 高（侵入 Skill 流程） |

前三项可以**纯在 match_reference 输出中实现**——Harness 不需要知道 LLM 调整了什么，只需要对比本轮指标和上轮/历史最佳指标，输出判断性文字。这是最低侵入、最务实的方案。

### 具体推荐：在 match_reference 输出中加入三段判断

```python
# 伪代码——在 match_reference 输出组装时追加

best = _session_reference.get("best_metrics")
current_hist = metrics_result["histogram_correlation"]

# 1. 达标检查
if current_hist > 0.85 and abs(rb_delta_pct) < 0.15:
    lines.append("✅ 量化指标已达到良好匹配范围，建议停止调整。")

# 2. 最佳点追踪
if best is None or current_hist > best["histogram_correlation"]:
    _session_reference["best_metrics"] = metrics_result
    lines.append(f"🏆 新最佳记录：直方图相似度 {current_hist:.2f}")
elif best is not None:
    lines.append(
        f"⚠ 当前直方图相似度 {current_hist:.2f} 低于最佳记录 "
        f"{best['histogram_correlation']:.2f}（第 {best.get('round', '?')} 轮）。"
        f"可能已越过最佳点，考虑回退。"
    )

# 3. 连续下降检测
prev_hist = _session_reference.get("prev_hist_correlation", 0)
if prev_hist > 0 and current_hist < prev_hist:
    _decline_count = _session_reference.get("_decline_count", 0) + 1
    _session_reference["_decline_count"] = _decline_count
    if _decline_count >= 2:
        lines.append("🔴 直方图相似度连续下降，建议回退到上一轮参数并停止该方向调整。")
else:
    _session_reference["_decline_count"] = 0
_session_reference["prev_hist_correlation"] = current_hist
```

这些都不需要新增工具或改 Skill——纯在 `match_reference` 返回值里加智能判断。

---

## 附录：量化指标追踪表

完整数据见日志文件 `docs/tmp_issues/0713/logs/tool_calls.jsonl`。关键行号对应关系：

| 日志行 | 内容 |
|:--:|------|
| 1 | build_atmosphere_mapping 初始扫描 |
| 2 | match_reference 初始对比 |
| 33 | match_reference R1 — 最接近参考（R/B=1.28） |
| 54 | match_reference R7 — 直方图峰值 (0.85) |
| 58 | match_reference R8 — 开始滑坡 |
| 66 | match_reference R10 — 持续恶化 (0.64) |
