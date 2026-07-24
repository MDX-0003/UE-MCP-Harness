# 019 — match_reference 状态类化 + atmosphere 模块提取

**类型：** AFK

**依赖关系：** 依赖 Issue 018（handler 分发机制已就位）；是 Issue 020（命名规整）的前置——reference.py/atmosphere.py 稳定后命名 sweep 才有效。

## 要构建什么

1. **ReferenceImageSession dataclass**：替代 server.py `_session_reference` 裸 dict 的 14 个魔法字符串键（`_match_count` / `_countdown_remaining` / `_max_allowed_rounds` / `best_metrics` / `_prev_hist` / `_decline_count` / `_snapshot_saved` / `best_snapshot_path` 等），均为显式字段；0714 叫停公式（总上限、激活当轮 -1、换参考图重置）变为方法。
2. **verification/reference.py**：match_reference handler + ReferenceImageSession + `ref_resolve_snapshot_path` + 趋势/指标渲染 + `ref_build_stop_summary`（原 StopLimitInterceptor.build_summary 归位）
3. **verification/atmosphere.py**：build_atmosphere_mapping handler + 现有全部 mapping 辅助函数（`_build_property_index` / `_build_mimo_prompt` / `_resolve_mimo_indices` / `_build_trend_summary` / `_render_mapping_markdown` / `_extract_actor_names` / `_extract_property_names` / `_resolve_component_properties` / `_item_to_name` / `_b64_to_pil` / `_unwrap_return_value_text` 等）
4. **同批修复 B1**：match_reference 连续下降检测 `lines`→`body_lines` 的 UnboundLocalError
5. **StopLimitInterceptor 废止**：它不是拦截器（不覆盖任何钩子）——`ref_build_stop_summary` 函数入 reference.py，stop_limit.py 文件删除，拦截器链中去掉空转项

## 验收标准

### ReferenceImageSession

- [ ] dataclass 字段覆盖 `_session_reference` 全部 14 个魔法键
- [ ] `check_stop() -> str | None`：0714 硬终止判定——倒计时归零 或 匹配轮次超总上限 → 返回 stop reason；None 表示继续。**必须在截图/MiMo 调用之前执行**（0714 红线）
- [ ] `begin_round(ref_path: str) -> bool`：换参考图时清空全部累积状态（倒计时/最佳点/下降计数/快照标记），返回是否为首次加载
- [ ] `activate_countdown(hist: float)`: hist ≥ 0.70 且倒计时未激活时 → `countdown_remaining = 3`，`max_allowed_rounds = match_count + 3`；07514 公式：`max(10, countdown_start_round + 3)`
- [ ] `record_metrics(m: dict) -> RoundEvents`：更新 best_metrics（🏆 新最佳）、prev_hist（连续下降检测 ≥2 轮 ⚠）、倒计时递减每轮 -1
- [ ] `snapshot_path_resolver` 回调：原 `_ensure_best_snapshot_path` 的逻辑作为可注入的 async callable（Session 自己不调 UE——保持依赖方向），rename `ref_resolve_snapshot_path`

### 模块拆分

- [ ] `harness/verification/reference.py`（新增）：match_reference handler（~200 行线性七步流程）+ `ref_build_stop_summary` + `ref_render_metrics_trend`（原 `_build_trend_summary`）+ ReferenceImageSession
- [ ] `harness/verification/atmosphere.py`（新增）：build_atmosphere_mapping handler + 全部 mapping 辅助函数（在原 server.py 中是模块级函数，一并移入）。MiMo prompt 函数改名 `_build_classify_prompt`（去供应商名耦合）
- [ ] `harness/stop_limit.py` 删除；测试 test_stop_limit.py 改为从 reference.py import `ref_build_stop_summary` 和 ReferenceImageSession
- [ ] server.py 旧函数全部删除或 thin-wrapper（见兼容策略）；server.py:50 死导入同批清理（若 Phase 1 未清）
- [ ] `_b64_to_pil` 移入 capturer.py 删除 server 重复（`capture_b64_to_pil`，Issue 017 若未做则在此做）

### match_reference handler 线性化

- [ ] check_stop → 加载参考图 → `capture_screenshot` → compute metrics（失败降级 ⚠ 注记不阻断）→ record_metrics（含 SaveLevelAs 快照**一次**——首次 hist≥0.70 且 `_snapshot_saved` 未标记时存，`bOpenSavedLevel=false`）→ Vision 双图对比 → 渲染（header 含倒计时状态 + 趋势 + 9 维度 + 指标表 + 尾部固定引导文本，输出文本**逐字保留**）

### 回归验证

- [ ] `uv run pytest tests/ -v` 全量绿
- [ ] test_stop_limit / test_build_atmosphere_mapping / test_metrics 全绿（import 路径更新后）
- [ ] match_reference 输出文本与重构前逐行比对一致（diff review）
- [ ] 拦截器链去掉 StopLimit 后顺序不变（它无钩子，有否等价）
- [ ] 换参考图 → 倒计时/最佳点/下降计数/快照标记全部清空（单测）

## 设计说明

**为什么 atmosphere 单独模块？** match_reference（对比）与 build_atmosphere_mapping（扫描+分类）是两个独立的领域操作，共用 Vision + MiMo 管线但 handler 体量都很大（~330 行 + ~180 行）。分开比塞进一个文件更清晰，也便于 Issue 016 Part B 未来扩展参考图对比逻辑。

**StopLimitInterceptor 的真实身份**：它不覆盖 pre_call/post_call，拦截器链中空转，真实用途是被 server.py 直接调 `build_summary`。转正为普通函数放入 reference.py 消除"Is this an interceptor?"的认知负担。

**`_b64_to_pil` 去留**：server.py 的这个函数与 capturer.py 的 `_parse_png_dimensions` / PIL resize 逻辑重叠（Issue 017+021 的截图收敛范围）。本 Issue 先做基础移动（→ capturer.py），Issue 021 做进一步合并。

## 涉及文件

- `harness/verification/reference.py`：新增（~400 行）
- `harness/verification/atmosphere.py`：新增（~350 行）
- `harness/stop_limit.py`：**删除**
- `harness/server.py`：删 ~500 行旧 match_reference / build_atmosphere_mapping 逻辑 + mapping helper 函数；保留 thin re-export shim（见兼容策略）
- `harness/cli.py`：删除 StopLimit 的链位（222 行实例化 + 227 行列表项 + 278 行传给 build_server 的 `stop_limit=` 参数 + build_server 签名中去掉 `stop_limit` 参数）
- `harness/verification/capturer.py`：+ `capture_b64_to_pil`
- 测试：test_stop_limit.py / test_build_atmosphere_mapping.py / test_metrics.py / test_verification_interceptor.py（_render_mapping_markdown 导入路径更新）
