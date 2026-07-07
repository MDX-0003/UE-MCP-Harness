# Harness Bug 分析报告

> 基准会话：eb83e070 (07-06 14:25) → f1ee6931 (07-07 11:10) → **5e295893 (07-07 15:37) — 最新**
> 状态更新：2026-07-07

---

## P0-1 [已解决] StateCache 全链路失效：参数模式与真实工具对不上

**修复**：`normalize_tool_args` + `NormalizedCall`，全部 12 个 handler 已迁移。

---

## P0-2 [已解决] `class_name` 在全代码库中从未被赋值 → "Unknown×N"

**修复**：四层写入路径（PLAN_0706 Step 1-4 + Bug 1/2 修复）。

---

## P0-3 [已解决] 提问模式 verdict 硬编码 `pass=True`

**修复**：PLAN 0707（统一结构化输出 + `response_format` JSON 模式 + `_VISION_FORMAT_REMINDER`）。

---

## P1-4 [已解决] verdict 正文被 max_tokens 掐断

**修复**：max_tokens 从 1024 → 2048 → **4096**。最新会话 5e295893 中两份 verdict 均完整，每份含 4 条 observations + 中文详细描述，无截断。`stop_reason` 检测已就位。

**关联 P1-9**：JSON 截断导致结构化碎裂的问题随之解决。

---

## P0-8 [已解决] Vision Session 未被关闭

**修复**：
1. `VisionSessionManager.close_active()` — Harness 关闭时自动归档未关闭的 Vision session
2. 最新会话 5e295893 中 LLM 主动调了 `vision_reset`（line 51），说明 instructions 引导生效
3. `vision_sessions/d36d4a4f.json` 正常归档（screenshot_count: 2, question_count: 3）

---

## P1-10 [已改善] LLM 在光源无照射面时收敛于图标颜色

**已做**：
1. instructions 新增灯光修改 SOP（空间检查 → 移灯 → 改属性 → 验证受光面）
2. `build_scene_context` 自动注入"附近几何体"信息

**最新会话 5e295893 表现**：
- LLM 调 `get_actor_transform` 获取了全部 8 个 StaticMesh 的位置 ✅
- `FocusOnActors` 对准的是 **StaticMesh**（不是灯光图标）✅
- vision_screenshot question 问的是"surfaces of the static meshes"、"shadows on the ground" ✅
- Vision 回答关注被照亮表面，正确识别冷暖色对比和明暗关系 ✅

**残留风险**：instructions 引导是软约束，极端场景下 LLM 仍可能退化。长期应加 Harness 端检测——Vision 报告 confidence=low 且 caveats 含"无被照表面"时，在返回中显式追加 `⚠ Vision 无法验证光照效果，请先确认几何体在灯光范围内。`

---

## P1-5 JSONL 记录失真：off-by-one + 关键内容缺失

**状态**：**部分解决**。

**已做**：`vision_calls.jsonl` 实时记录每次 Vision 调用的完整 Q&A 对，不受 off-by-one 影响。

**残留**：`tool_calls.jsonl` 中 vision_screenshot 的 `verdict` 字段仍为旧格式 `{"pass": null, "reason": ""}`（off-by-one + 旧字段名）。`vision_calls.jsonl` 已覆盖此需求，`tool_calls.jsonl` 的遗留字段可后续清理或标记 deprecated。

---

## P1-6 Session 归档统计三处失真

**状态**：未修复。question_count、context_sources、tool_call_count 口径均未修正。

---

## P2-7 Vision 推理质量 + 相机 SOP 的缺口

**状态**：**部分改善**。
- system prompt 已加入"判断灯光看被照亮表面，不看 gizmo"
- instructions 已加入灯光 SOP 和相机预设角度
- 最新会话中 LLM 正确将相机对准几何体而非灯光图标 ✅
- 截图仍为 1024×321（高度偏低），但 Vision 判断质量明显提升

---

## 新追踪能力（2026-07-03 新增）

| 产物 | 位置 | 内容 |
|:---|:---|:---|
| `instructions.md` | `{session_dir}/` | LLM 收到的完整行为准则 |
| `vision_calls.jsonl` | `{session_dir}/` | 每次 Vision Q&A 实时记录（question + verdict） |
| `vision_sessions/{id}.json` | `{session_dir}/vision_sessions/` | 关闭时自动归档（不再依赖 `vision_reset`） |

---

## 修复优先级建议（2026-07-03 更新）

| 优先级 | Bug | 状态 | 说明 |
|:---:|:---|:---:|:---|
| **P1** | P1-10 图标颜色收敛 | 改善 | 最新会话验证通过。残留：Harness 端 confidence=low 时显式警告 |
| **P1** | P1-5 JSONL 失真 | 部分解决 | `vision_calls.jsonl` 已覆盖；tool_calls.jsonl 旧字段待清理 |
| **P2** | P1-6 Session 归档失真 | 未修 | 纯记账问题 |
| **P2** | P2-7 相机 SOP | 改善 | 截图高度偏低待修 |

已解决的 P0-1/P0-2/P0-3/P1-4/P1-9/P0-8 从表中移除。
