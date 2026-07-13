# 移除 match_reference 内置视角自动调节 — 实施计划

> **适用执行方式：** 可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务实施。
> 步骤使用 checkbox (`- [ ]`) 语法追踪进度。

**目标：** 从 `match_reference` 工具中删除基于 MiMo 单图估计的视角自动对齐功能，代之以信息性的"视角方向"相对比较（不自动操作相机）。

**架构：** 删除 3 个函数 (`_analyze_viewpoint`, `_get_landscape_z`, `_CAMERA_ALIGN_TOOLS`)、1 个 prompt 常量 (`_VIEWPOINT_PROMPT`)、约 80 行视角对齐代码，以及整个 `test_camera_alignment.py` 测试文件。在 `match_reference` 的 MiMo 8 维度提问中新增"视角方向"维度（纯信息输出，不驱动机位操作）。

**技术栈：** Python 3.12+, async/await, MiMo mimo-v2.5-pro, pytest

---

## 背景与决策依据

### 问题现象

Session `ebf03fe240915441486f4282cdcabd68` 中，`match_reference("Ref0.png")` 将 UE 视口从正确的平视（pitch=0°）自动改为俯视（pitch=-20°, height_offset=400）：

```
📷 视角已自动修正: 原 pitch=0° → -20°, 高度 offset=400
```

参考图本身是平视拍摄的，MiMo 因画面中天空占比较小（Landscape 地形起伏导致）而误判为俯视。

### 根因

1. **单图→3D 相机参数是不适定问题**。同一张 2D 图像可由无限种相机位姿+场景几何组合产生。MiMo (`mimo-v2.5-pro`) 作为通用 VLM 不具备单图视角估计能力。
2. **`_VIEWPOINT_PROMPT` 依赖粗糙启发式**。"地平线在画面中间 → pitch=-5~0 / 能看到天空地面占下半 → pitch=-15~-30"——在有山峰/高地形的 UE Landscape 场景中完全失效。
3. **`_analyze_viewpoint` 绕过 VisionSubAgent**，直接调 `_call_vision_api`，不记入 `vision_calls.jsonl`，无审计轨迹。

### 决策：删除内置视角自动对齐

| 考量 | 判断 |
|------|------|
| MiMo 能否可靠估计单图视角？ | 不能——模型能力的硬边界，非 prompt 工程可修复 |
| 正确调节的收益 vs 错误调节的代价 | 正确时省 1 次 `SetCameraTransform`；错误时**主动破坏正确视角**，LLM 需察觉+回退。错误代价 >> 正确收益 |
| 是否符合 SRP？ | 否——氛围匹配与视角匹配是两个独立关注点 |
| LLM 能否自行调节相机？ | 能——`SetCameraTransform` 和 `FocusOnActors` 已在 tools_allowlist 中 |

### 替代方案：信息性视角方向提示

在 `match_reference` 的 MiMo 8 维度提问中新增第 9 维度——**相对比较**而非绝对估计：

```
视角方向 (Viewpoint Direction): looking_more_up / similar / looking_more_down
```

MiMo 不输出角度数字，只判断当前截图比参考图更仰视/相似/更俯视。LLM 拿到方向后**自行决定**是否及如何调整相机。这利用了 MiMo 的双图对比能力（已经在 8 维度验证中正常工作），避开了单图绝对估计的不适定问题。

---

## 文件结构

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `harness/server.py` | 删除 6 处代码块 + 新增 1 行 prompt |
| 删除 | `tests/test_camera_alignment.py` | 整个文件（约 250 行） |
| 新增 | `docs/plans/PLAN_0713_remove_viewpoint_auto_align.md` | 本计划文档 |

---

### Task 1: 删除 server.py 中的视角对齐主逻辑块

**文件：**
- 修改: `harness/server.py:645-722`

**说明：** 删除从 `# ---- 视角自动对齐 ----` 到 `# ---- 视角对齐结束 ----` 的完整代码块，包括 `camera_aligned`、`_analyze_viewpoint` 调用、`_get_landscape_z` 调用、`SetCameraTransform` 调用和重截图逻辑。

- [ ] **Step 1: 定位并删除视角对齐代码块**

删除 `harness/server.py` 中 `_session_reference = {"b64": ref_b64, "path": str(ref_path)}` 之后的整个相机对齐区块，从：

```python
            # ---- 视角自动对齐 ----
            camera_aligned = False
```

到：

```python
            # ---- 视角对齐结束 ----
```

之间的所有内容。删除后，`_session_reference = ...` 行之后应直接衔接 `# 4. 量化指标` 注释。

- [ ] **Step 2: 验证删除后的语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`（无语法错误）

注意：此时其他引用（`_analyze_viewpoint`、`_get_landscape_z` 等）仍然存在但不再被调用，后续 Task 继续清理。

---

### Task 2: 删除 camera_aligned 输出块

**文件：**
- 修改: `harness/server.py:787-793`

- [ ] **Step 1: 删除 camera_aligned 条件输出块**

删除以下代码块（位于 MiMo 8 维度差异和量化指标之间）：

```python
            if camera_aligned:
                align_note = (
                    f"📷 视角已自动修正: 原 pitch={cur_view["pitch"]:.0f}° → {ref_view["pitch"]:.0f}°,"
                    f" 高度 offset={ref_view["height_offset"]:.0f}"
                )
                lines.insert(0, align_note)
                lines.insert(1, "")
```

删除后，`if trend_lines:` 块之后直接接 `lines.append("")`。

- [ ] **Step 2: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`

---

### Task 3: 删除 _VIEWPOINT_PROMPT 常量

**文件：**
- 修改: `harness/server.py:1663-1680`

- [ ] **Step 1: 删除 _VIEWPOINT_PROMPT**

定位并删除以下代码块（在 `_build_trend_summary` 返回之后、`_analyze_viewpoint` 之前）：

```python
_VIEWPOINT_PROMPT = (
    "评估这张截图的拍摄视角。"
    "UE 坐标系：pitch=0 为水平向前，pitch=-90 为垂直向下看地面。\n"
    ...
    '{"pitch": <推测数字>, "height_offset": <推测数字>}'
)
```

- [ ] **Step 2: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`

---

### Task 4: 删除 _analyze_viewpoint 函数

**文件：**
- 修改: `harness/server.py:1679-1722`

- [ ] **Step 1: 删除 _analyze_viewpoint 函数**

删除整个 `async def _analyze_viewpoint(config, image_b64)` 函数体：

```python
async def _analyze_viewpoint(
    config: Config, image_b64: str,
) -> "dict[str, float] | None":
    """MiMo 单图视角分析，返回 {pitch, height_offset} 或 None.
    ...
    """
    messages = [...]
    try:
        response = await _call_vision_api(...)
    except Exception:
        return None
    ...
    return {"pitch": pitch, "height_offset": height_offset}
```

- [ ] **Step 2: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`

---

### Task 5: 删除 _get_landscape_z 函数和 _CAMERA_ALIGN_TOOLS 字典

**文件：**
- 修改: `harness/server.py:1724-1770`

- [ ] **Step 1: 删除 _CAMERA_ALIGN_TOOLS 字典**

```python
_CAMERA_ALIGN_TOOLS: dict[str, str] = {
    "find_actors": "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
    "get_actor_bounds": "toolset_registry.toolsets.core.actor.ActorTools.get_actor_bounds",
    "get_camera": "ToolsetRegistry.EditorAppToolset.GetCameraTransform",
    "set_camera": "ToolsetRegistry.EditorAppToolset.SetCameraTransform",
}
```

- [ ] **Step 2: 删除 _get_landscape_z 函数**

删除整个 `async def _get_landscape_z(ue_client)` 函数体（约 40 行）。

- [ ] **Step 3: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`

---

### Task 6: 在 match_reference 的 MiMo 提问中新增"视角方向"维度

**文件：**
- 修改: `harness/server.py` — `match_reference` 处理块中的 `question` 变量

**说明：** 将 MiMo 的 8 维度提问扩展为 9 维度。新增维度不要求输出角度数字，只做双图相对比较。

- [ ] **Step 1: 定位 match_reference 中的 question 变量定义**

当前位置在 Task 1 删除视角对齐代码块后的 `# 5. MiMo 8 维度双图对比` 附近。将：

```python
            question = (
                "请从以下 8 个维度比较当前截图与参考图的差异。"
```

改为：

```python
            question = (
                "请从以下 9 个维度比较当前截图与参考图的差异。"
```

- [ ] **Step 2: 在维度列表末尾新增"视角方向"维度**

在 `"天空表现 (Sky):           颜色/云量/渐变的差异方向"` 之后新增：

```python
                "视角方向 (Viewpoint Direction): looking_more_up / similar / looking_more_down\n\n"
                "注意：视角方向只比较相机俯仰角（向上看 vs 向下看），"
                "不考虑相机距离和目标对象。"
                "如果两张图拍摄的是完全不同的场景/对象，填 'different_scene'。"
```

- [ ] **Step 3: 更新后续引用**

将 `"MiMo 8 维度差异："` 改为 `"MiMo 9 维度差异："`。

- [ ] **Step 4: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py').read()); print('OK')"
```

预期：`OK`

---

### Task 7: 删除测试文件 test_camera_alignment.py

**文件：**
- 删除: `tests/test_camera_alignment.py`

- [ ] **Step 1: 删除整个测试文件**

```bash
Remove-Item "tests/test_camera_alignment.py"
```

- [ ] **Step 2: 运行全部测试确认无回归**

```bash
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

预期：全部通过（数量从 331 降至约 329，因为删除了 2 个测试方法及其 fixture 函数）。

- [ ] **Step 3: 确认无其他文件 import test_camera_alignment**

```bash
uv run python -c "import ast, os; [print(f'{root}/{f}') for root,dirs,files in os.walk('.') for f in files if f.endswith('.py') for line in open(os.path.join(root,f)) if 'test_camera_alignment' in line]"
```

预期：无输出（无其他文件引用此测试模块）。

---

### Task 8: 最终验证与提交

- [ ] **Step 1: 运行完整测试套件**

```bash
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

预期：约 329 passed + 4 skipped。

- [ ] **Step 2: 确认 server.py 无残留引用**

```bash
uv run python -c "
import ast, sys
tree = ast.parse(open('harness/server.py').read())
names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
removed = {'_analyze_viewpoint', '_get_landscape_z', '_CAMERA_ALIGN_TOOLS', '_VIEWPOINT_PROMPT', 'camera_aligned', 'cur_view', 'ref_view'}
remaining = names & removed
if remaining:
    print(f'残留引用: {remaining}')
    sys.exit(1)
else:
    print('无残留引用')
"
```

预期：`无残留引用`

- [ ] **Step 3: 提交**

```bash
git add harness/server.py tests/test_camera_alignment.py
git commit -m "refactor: 移除 match_reference 内置视角自动对齐

删除基于 MiMo 单图估计的视角自动调节功能：
- _analyze_viewpoint / _get_landscape_z / _CAMERA_ALIGN_TOOLS
- _VIEWPOINT_PROMPT 常量
- camera_aligned 输出块
- test_camera_alignment.py

替代方案：match_reference 的 MiMo 提问新增第 9 维度'视角方向'
（相对比较 looking_more_up/similar/looking_more_down），
LLM 自行决定是否调节相机，不自动操作。

Closes: docs/plans/PLAN_0713_remove_viewpoint_auto_align.md
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审清单

1. **需求覆盖：**
   - [x] 删除视角自动对齐逻辑（Task 1）
   - [x] 删除对齐输出块（Task 2）
   - [x] 删除 `_VIEWPOINT_PROMPT`（Task 3）
   - [x] 删除 `_analyze_viewpoint`（Task 4）
   - [x] 删除 `_get_landscape_z` + `_CAMERA_ALIGN_TOOLS`（Task 5）
   - [x] 新增视角方向维度到 MiMo 提问（Task 6）
   - [x] 删除测试文件（Task 7）

2. **占位符检查：** 无 TBD/TODO/fill in details。所有步骤含具体代码和命令。

3. **类型一致性：** 无新增类型或接口——所有变更为删除或纯文本修改。

4. **影响分析：**
   - 删除的函数仅被 `match_reference` 内部使用，无外部调用者
   - `match_reference` 的外部接口不变（`path` 参数不变，返回值新增 1 行维度信息）
   - 测试从 ~331 降至 ~329（删除 2 个 test_camera_alignment 测试方法）
   - 无破坏性——LLM 已有 `SetCameraTransform`/`FocusOnActors` 工具可自行调节相机
