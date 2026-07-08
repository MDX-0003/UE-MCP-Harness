---
name: reference-image-design-0708
description: Issue 016 Part B 参考图氛围匹配的重新设计——3 工具 + Skill 重构 + Instructions 分离
metadata:
  type: decision
---

# 参考图氛围匹配设计 (2026-07-08)

## 核心决策

旧 Issue 016 Part B 的"Vision 双图对比 → LLM 自行拆步骤"被推翻，因为 Vision 无法输出参数级指导（断桥）。

新设计：Harness 提供 3 个确定性工具 + Skill 提供流程模板。

## 3 个新工具

| 工具 | 职责 | 调用频率 |
|------|------|---------|
| `build_atmosphere_mapping()` | 扫描 5 组件属性 → MiMo 筛选 → 生成映射 JSON → `atmosphere-mapping.md` | 每会话一次 |
| `match_reference(path)` | 双图对比 → 8 维度固定提问 → 差异清单 | 每次换参考图 |
| `vision_compare(component)` | 单组件三态判定 ✓/≈/✗ | 每组件每轮迭代 |

## 关键架构原则

1. **8 个对比维度预写在代码里**（非开放提问），消除 Vision 输出的随机性
2. **映射由 MiMo 动态生成**（非静态映射表），适应不同 UE 版本/场景
3. **域知识分离**：Base instruction（通用 SOP）→ Skill（领域流程）
4. **氛围优先，局部次之**：两种 Skill 互斥，LLM 通过 activate_skill 切换

## Skill 变更

- 新增 `match-atmosphere`（流程型，不含参数值）
- 新增 `scene-lighting`（旧灯光 SOP 迁移至此）
- 删除 `evening-lighting`（硬编码配方被参考图取代）
- 瘦身 `scene-verification`（SOP 已进 Base instruction）

## 设计文档

`docs/PLAN_0708_reference_image_design.md`

**Why:** 旧设计的 Vision→LLM 链路存在随机性过高和断桥两个根本问题。氛围的 5 组件是领域常数，Harness 应利用这个常数做确定性编排，而非开放给 Vision 自由描述。

**How to apply:** 按设计文档的「涉及文件」表逐项实现。先 Part A（ReadbackInterceptor），后本设计的 3 个工具。
