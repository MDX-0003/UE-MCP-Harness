---
name: plan-0706-0707-summary
description: PLAN_0706 (class_name) 和 PLAN_0707 (Vision 统一输出) 的快速索引——核心设计决策和实现文件
metadata:
  type: project
---

# PLAN_0706 & PLAN_0707 设计决策

## PLAN_0706: class_name 会话内补全

**决策**: 四层写入路径（全部内存态，不落盘）
1. 名称推断: `infer_class_name("SpotLight_0")` → `"SpotLight"`
2. L3 刷新填入
3. add_to_scene 顺手填 (`nc.payload["actor_type"]`)
4. get_class 读回填

**已裁掉 Step 5**（Vision 惰性 UE 查询）——名称推断覆盖 88% actor，miss 的全是引擎内部 Actor。

**关键文件**: `harness/state/normalize.py` (infer_class_name), `harness/state/interceptor.py` (_handle_add_to_scene, _handle_get_class), `harness/state/refresher.py` (_parse_actor_list fix)

## PLAN_0707: Vision 统一结构化输出

**决策**: 
- 取消三模式 (verify/question/describe) → 统一 JSON 输出
- `VisionVerdict.pass_` → `None`（不做二元判定）
- 用 `response_format` JSON 模式 + `_VISION_FORMAT_REMINDER` 替代纯 prompt 工程
- `vision_calls.jsonl` 实时记录 Q&A（不受 off-by-one 影响）

**不要做的事**（已讨论并否决）:
- 不要在 `_parse_verdict` 加旧格式兼容——通过 prompt 修复根本原因
- 不要把 Step 5（惰性 UE 查询）加回来——ROI 太低

**关键文件**: `harness/verification/vision_agent.py` (全部核心), `harness/verification/session.py` (附近几何体 + _append_vision_call_log), `harness/server.py` (徽章), `harness/cli.py` (instructions 灯光 SOP + close_active + instructions.md)

Why: 这两份 PLAN 是 07-06/07-07 两天的主要工作。后续开发应在此基础上增量，不要重新讨论已解决的问题。
How to apply: 改 Vision/class_name 相关代码前先读对应的 PLAN 文档和此记忆。
