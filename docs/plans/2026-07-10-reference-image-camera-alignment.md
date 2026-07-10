# PLAN 0710 — 参考图视角自动对齐

**状态：** 设计完成，待实现
**依赖：** Plan 0708（match_reference handler 已就位）
**关联：** [[ue-mcp-tool-naming]]（handler 内调 UE 工具必须用完全限定名）

## 动机

LLM 在参考图氛围匹配中反复使用预设俯视角度（pitch=-25/-20），从未尝试接近水平的视角（pitch≈-5）。根因是所有 Harness 相机引导都围绕"俯瞰地面验证修改"设计，没有"匹配参考图构图"的认知。

`match_reference` handler 已具备截图能力和双图对比能力——可以在对比前自动检测视角偏差并修正，免去 LLM 手动试错相机的摩擦。

## 设计

### 核心流程

```
match_reference("ref.png")
  │
  ├─ 1. 参考图视角分析（首次调用时做一次，缓存到 _session_reference["ref_view"]）
  │     MiMo 单图（仅参考图，纯文本提问）
  │     → {"pitch": -10, "height_offset": 170}
  │
  ├─ 2. 截当前 UE 视口 → cur_b64（常规截图，和现在的逻辑一致）
  │
  ├─ 3. 当前截图视角分析
  │     MiMo 单图（仅当前截图，纯文本提问）
  │     → {"pitch": -55, "height_offset": 3000}
  │
  ├─ 4. 判断: abs(ref.pitch - cur.pitch) > 30？
  │     |−10 − (−55)| = 45 > 30 → YES，触发自动修正
  │
  ├─ 5. Harness 调相机:
  │     SetCameraTransform(pitch=ref.pitch, location.z = landscape_z + ref.height_offset)
  │     （保留当前 x, y, yaw 不动——水平方向无法可靠推断）
  │     重截视口 → cur_b64 已更新为新视角
  │
  ├─ 6. MiMo 双图 8 维度对比（视角已对齐）
  │
  └─ 7. 返回体: 视角修正摘要（如有修正）+ 趋势（如有上次）+ 8 维度 + 量化指标
```

### 关键设计决定

| 决定 | 选择 | 理由 |
|------|------|------|
| 步骤 1 缓存 | `_session_reference["ref_view"]`（已复用同名结构） | 参考图不变，视角分析只需一次 |
| 偏差阈值 | `abs(pitch_diff) > 30°` | 30° 以上是质的差异（平视 vs 俯视），以下 LLM 自己微调 |
| 高度获取 | 调 UE `get_actor_bounds` 找 Landscape；找不到时默认 z=0 | 不能硬编码——不同场景 Landscape 高度不同 |
| 水平方向（yaw） | 不自动调 | 单张图无法可靠判断 yaw，超出 MiMo 能力 |
| auto-correct 失败 | 降级到原流程（跳过步骤 5，用原始截图做对比） | 不因视角修正失败阻断 match_reference |

### MiMo 视角分析 Prompt

**步骤 1（参考图视角）：**

```
评估这张参考图的拍摄视角。

UE 坐标系：pitch=0 为水平向前，pitch=-90 为垂直向下看地面。

pitch 数值参考：
  - 地平线在画面中间，相机几乎水平 → pitch 在 -5 到 0
  - 能看到天空，地面占下半部分 → pitch 在 -15 到 -30
  - 几乎看不到天空，全部是地面/物体 → pitch 在 -50 到 -70
  - 不确定时取中间值，粒度 5°

相机离地表高度（UE 单位，1 人身高≈170）：
  - 贴近地面 → 50
  - 人眼或略高 → 170
  - 几层楼 → 800
  - 更大高度 → 2000~5000，根据画面推断

只输出 JSON，不要其他文字：
{"pitch": <推测数字>, "height_offset": <推测数字>}
```

**步骤 3（当前截图视角）：** 相同 prompt，对象换成当前截图。

### 涉及改动

| 文件 | 改动 |
|------|------|
| `harness/server.py` | `match_reference` handler 内插入视角分析 + 自动修正逻辑（约 60 行） |
| `docs/plans/2026-07-10-reference-image-camera-alignment.md` | 本文档 |

不涉及新文件、新模块、新工具——所有逻辑在 `match_reference` handler 内完成。

### 不做的事

- 不做 yaw 自动对齐——单图不可靠，可能引入错误
- 不通过 vision_compare 做视角修正——那是 LLM 调用的工具，这里是 handler 内部自动化
- 不做多次重试——一次修正失败就降级，不阻塞
- 不需要新 memory 条目——工具名用完全限定名的规则已记录在 [[ue-mcp-tool-naming]]

### 工具名

handler 内部需要调的 UE 工具及其完全限定名（从 tools/list 确认）：

| 用途 | 完全限定名 |
|------|-----------|
| 找 Landscape | `toolset_registry.toolsets.core.scene.SceneTools.find_actors` |
| 拿高度 | `toolset_registry.toolsets.core.actor.ActorTools.get_actor_bounds` |
| 调相机 | `toolset_registry.EditorAppToolset.SetCameraTransform` |
| 截图 | 复用现有 `capturer.capture()` |
