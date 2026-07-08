# 参考图 Skills + Instructions Implementation Plan

> **依赖:** Plan 2（Skill YAML 引用 `match_reference`、`build_atmosphere_mapping`、`vision_compare` 3 个工具名）
> **可并行:** 与 Plan 2 同时开发——纯 YAML/文本层，不依赖 Python 测试通过

**Goal:** 新增 `match-atmosphere` 和 `scene-lighting` 两个 Skill，删除 `evening-lighting`，瘦身 `scene-verification`，重写 Base instruction 文本，更新 `snapshotter.py` 的 `write_session_json`。

**Architecture:** 纯配置层——无 Python 逻辑变更（snapshotter 只加两个 JSON 字段）。Skills 是 YAML 文件，直接放 `skills/` 目录由 SkillRegistry 自动扫描。Instructions 文本替换在 `SystemContextProvider.AGENT_IDENTITY`。

**Tech Stack:** YAML, Python (snapshotter 的 2 个字段)

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `skills/match-atmosphere.yaml` | **新建** | 参考图氛围匹配流程 |
| `skills/scene-lighting.yaml` | **新建** | 局部灯光精确调整流程 |
| `skills/evening-lighting.yaml` | **删除** | 被 match-atmosphere 替代 |
| `skills/scene-verification.yaml` | **修改** | 瘦身 SOP 步骤文本 |
| `harness/context/prompt.py` | **修改** | `SystemContextProvider.AGENT_IDENTITY` 重写 |
| `harness/observability/snapshotter.py` | **修改** | `write_session_json` 加 `reference_image` + `mapping_path` |

---

### Task 1: 新建 `match-atmosphere.yaml`

**Files:**
- Create: `skills/match-atmosphere.yaml`

- [ ] **Step 1: 创建 Skill YAML**

```yaml
# 参考图氛围匹配 Skill
name: match-atmosphere
description: "参考图驱动的场景氛围匹配：加载参考图 → 8 维度对比 → 按组件逐项调整 → 三态验证闭环"
triggers:
  - "参考图"
  - "参考这张图"
  - "匹配这张图"
  - "match reference"
  - "氛围"
  - "atmosphere"
  - "像这张图"
  - "match atmosphere"
  - "参考这个"
  - "调氛围"
  - "整体氛围"
  - "照着这张图"
tools_allowlist:
  # 氛围组件操作
  - "SceneTools.find_actors"
  - "SceneTools.add_to_scene_from_class"
  - "ObjectTools.list_properties"
  - "ObjectTools.get_properties"
  - "ObjectTools.set_properties"
  - "ActorTools.get_actor_transform"
  - "ActorTools.set_actor_transform"
  # 相机与截图
  - "EditorAppToolset.SetCameraTransform"
  - "EditorAppToolset.FocusOnActors"
  - "EditorAppToolset.GetCameraTransform"
  # Harness 自有工具
  - "build_atmosphere_mapping"
  - "match_reference"
  - "vision_compare"
  - "vision_screenshot"
  - "vision_ask"
  - "vision_tell"
  - "vision_reset"
  - "vision_status"
  - "deactivate_skill"
steps: |
  ## 参考图氛围匹配流程

  ### Step 1 — 生成参数映射
  调 build_atmosphere_mapping()，Harness 自动扫描 5 类氛围组件的可用属性
  并生成维度→属性的映射。映射通过 LLM 阅读解析。

  ### Step 2 — 加载参考图并对比
  调 match_reference("<path>")，返回 8 维度方向性差异清单。

  ### Step 3 — 逐组件调整
  阅读 atmosphere-mapping 中的属性列表，找到差异对应的 UE 属性。
  对每个有差异的组件：
    a. 调 get_properties 获取当前值
    b. 根据差异方向调整属性（参考 mapping 里的属性名）
    c. 调 set_properties 写入
    d. 调 vision_compare(component) 验证方向
       ✓ closer → 下一个组件
       ≈ similar → 加大调整幅度或换属性，回到 b
       ✗ further → 反向调整，回到 b

  ### Step 4 — 整体确认
  全部组件 ✓ 后，调 vision_screenshot 确认整体效果。
  如仍有偏差，回到 Step 3 微调。

  ### 提示
  - vision_compare 默认复用已有截图，token 消耗极低，可放心高频使用
  - 每轮迭代后 L2 读回会自动确认写入值（ReadbackInterceptor 已在链中）
  - 多实例组件（>1 个）优先调整最可能影响整体的那一个
```

- [ ] **Step 2: 验证 YAML 语法**

```bash
uv run python -c "import yaml; yaml.safe_load(open('skills/match-atmosphere.yaml', encoding='utf-8')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 3: Commit**

```bash
git add skills/match-atmosphere.yaml
git commit -m "feat: add match-atmosphere skill (reference-image-driven atmosphere matching)"
```

---

### Task 2: 新建 `scene-lighting.yaml`

**Files:**
- Create: `skills/scene-lighting.yaml`

- [ ] **Step 1: 创建 Skill YAML**

```yaml
# 局部灯光精确调整 Skill
name: scene-lighting
description: "局部灯光精确调整：单灯/少灯的位置、颜色、强度、旋转属性修改与验证"
triggers:
  - "灯光"
  - "点光源"
  - "聚光灯"
  - "调整灯"
  - "改灯光"
  - "lighting"
  - "light"
tools_allowlist:
  - "SceneTools.find_actors"
  - "ActorTools.get_actor_transform"
  - "ActorTools.set_actor_transform"
  - "ObjectTools.list_properties"
  - "ObjectTools.get_properties"
  - "ObjectTools.set_properties"
  - "EditorAppToolset.SetCameraTransform"
  - "EditorAppToolset.FocusOnActors"
  - "EditorAppToolset.GetCameraTransform"
  - "vision_screenshot"
  - "vision_ask"
  - "vision_tell"
  - "vision_reset"
  - "vision_status"
  - "deactivate_skill"
steps: |
  ## 灯光修改 SOP

  修改灯光属性前，必须先完成空间检查。视觉效果需在被照亮的表面上判断。

  ### 前置检查
  a. 确认灯光附近有可见几何体：
     调 get_actor_transform(灯光) 获取位置，
     调 get_actor_transform(StaticMeshActor) 获取几何体位置，
     计算距离。PointLight/SpotLight 有效半径约 1000 UE 单位，
     RectLight 约 2000 单位。超出则先调 set_actor_transform 移灯到目标附近。

  b. 确认灯光朝向目标：
     SpotLight 调 rotation 使光锥对准目标物体。

  c. 确认后再改属性：
     上面两步全部确认后，再调 set_properties 改 LightColor/Intensity。

  d. 验证时关注被照亮的表面：
     vision_screenshot 的问题包含"被照亮表面呈现什么颜色"，
     不问"图标是否可见"。

  ### 如果 Vision 返回"场景为空""无被照表面""无法观察光照效果"
  → 不要继续调整灯光属性！回到前置检查 a，先把灯移到几何体附近。
```

- [ ] **Step 2: 验证 YAML**

```bash
uv run python -c "import yaml; yaml.safe_load(open('skills/scene-lighting.yaml', encoding='utf-8')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 3: Commit**

```bash
git add skills/scene-lighting.yaml
git commit -m "feat: add scene-lighting skill (local light precise adjustment SOP)"
```

---

### Task 3: 删除 `evening-lighting.yaml`

**Files:**
- Delete: `skills/evening-lighting.yaml`

- [ ] **Step 1: 确认文件存在并检查是否有引用**

```bash
ls skills/evening-lighting.yaml
```
Expected: 文件存在

检查 CLAUDE.md 是否引用了 evening-lighting：
```bash
uv run python -c "print('evening-lighting' in open('CLAUDE.md', encoding='utf-8').read())"
```
Expected: `True`（CLAUDE.md 的 "废弃" 段落提到了它）

- [ ] **Step 2: 删除文件**

```bash
git rm skills/evening-lighting.yaml
```

- [ ] **Step 3: 更新 CLAUDE.md 中 evening-lighting 相关引用**

在 CLAUDE.md 中搜索 `evening-lighting`，检查是否需要更新。当前 CLAUDE.md 在"瘦身：scene-verification Skill"段落下提到了 evening-lighting 的废弃。这一步在 Task 6（neat-freak 收尾）中统一处理。

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: remove evening-lighting skill (superseded by match-atmosphere)"
```

---

### Task 4: 瘦身 `scene-verification.yaml`

**Files:**
- Modify: `skills/scene-verification.yaml`

**瘦身原则：** SOP 步骤已精简进 Base instruction 的"通用验证 SOP"，Skill YAML 的 `steps` 缩为一行引用。工具白名单保持不动。

- [ ] **Step 1: 编辑 `steps` 字段**

将 `skills/scene-verification.yaml` 的 `steps: |` 整段替换为：

```yaml
steps: |
  场景修改后的标准视觉验证流程（详见 Base instruction 中的「通用验证 SOP」）。
  简版：L2 读回确认 → 相机定位（预设角度轮换） → vision_screenshot(具体提问) → vision_ask 追问 → vision_reset 闭环。

  灯光验证特别注意：在被照亮的表面上判断效果，不看编辑器图标颜色。
```

原来的完整 SOP 文本（Step 1-5 + 快捷提示，约 40 行）不再需要——Base instruction 已覆盖核心流程。

- [ ] **Step 2: 验证 YAML 语法**

```bash
uv run python -c "import yaml; d = yaml.safe_load(open('skills/scene-verification.yaml', encoding='utf-8')); print(f'steps: {len(d[\"steps\"])} chars, tools: {len(d[\"tools_allowlist\"])}'); print('OK')"
```
Expected: `OK`（steps 明显变短，tools 数量不变）

- [ ] **Step 3: Commit**

```bash
git add skills/scene-verification.yaml
git commit -m "refactor: slim scene-verification skill steps (delegate to Base instruction)"
```

---

### Task 5: 重写 Base instruction

**Files:**
- Modify: `harness/context/prompt.py`

- [ ] **Step 1: 阅读现有的 `SystemContextProvider.AGENT_IDENTITY`**

当前定义在 `prompt.py:~44`（`SystemContextProvider` 类的 `AGENT_IDENTITY` 属性）。确认位置后原地替换。

- [ ] **Step 2: 替换 `AGENT_IDENTITY`**

将 `AGENT_IDENTITY` 类变量替换为：

```python
    AGENT_IDENTITY = (
        "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n"
        "\n"
        "## 工作模式\n"
        "当前有两种主要工作模式，根据用户意图选择：\n"
        "\n"
        "  氛围优先 → activate_skill(\"match-atmosphere\")\n"
        "    参考图驱动的整体场景氛围调整：光照、天空、雾、云、后处理\n"
        "\n"
        "  局部调整 → activate_skill(\"scene-lighting\")\n"
        "    单灯/少灯的精确属性调整：位置、颜色、强度、旋转\n"
        "\n"
        "  自由探索 → deactivate_skill（或保持不激活任何 Skill）\n"
        "    所有工具可用，适合查询、浏览、非标准操作\n"
        "\n"
        "## 通用验证 SOP\n"
        "任何场景修改后：\n"
        "  1. 修改后的写入值由 Harness 自动读回验证（⚠ 徽章提示失配）\n"
        "  2. 相机定位：用预设角度轮换确保视口对准目标\n"
        "     pitch=-25 yaw=45 / pitch=-20 yaw=90 / pitch=-55 yaw=0 / pitch=-15 yaw=0\n"
        "  3. vision_screenshot(question=\"具体问题\") 做视觉验证\n"
        "  4. 需要时用 vision_ask 追问，完成后 vision_reset 闭环\n"
        "\n"
        "  灯光验证特别注意：在被照亮的表面上判断效果，不看编辑器图标颜色。\n"
    )
```

保留 `VERIFICATION_SOP_HINT` 不变（它独立于 `AGENT_IDENTITY`，仍可用于场景验证提示）。

- [ ] **Step 3: 验证 prompt 组装不报错**

```bash
uv run python -c "
from harness.context.prompt import SystemContextProvider
p = SystemContextProvider()
text = p.render(state=None, active_skill=None)
assert 'match-atmosphere' in text
assert 'scene-lighting' in text
assert '通用验证 SOP' in text
print('OK')
"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add harness/context/prompt.py
git commit -m "feat: rewrite Base instruction with dual-mode guidance + verification SOP"
```

---

### Task 6: `snapshotter.py` — `write_session_json` 加字段

**Files:**
- Modify: `harness/observability/snapshotter.py`

- [ ] **Step 1: 定位 `write_session_json` 方法并加字段**

在 `SnapshotRecorder.write_session_json()` 中（`snapshotter.py` 约在 `def write_session_json(self) -> None:` 附近），在构建的 session JSON dict 中追加两个字段：

```python
    def write_session_json(self) -> None:
        """写入 session.json 摘要."""
        try:
            session_file = self._snapshot_dir / "session.json"
            data = {
                # ... 现有字段保持不变 ...
                # 追加以下两个字段：
                "reference_image": getattr(self, "_reference_path", None),
                "mapping_path": getattr(self, "_mapping_path", None),
            }
            session_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("写入 session.json 失败: %s", e)
```

**注意：** 实际编辑需要先 `get_editing_context` 查看准确的代码位置和现有字段列表。这里只描述意图：在 session.json 的 dict 中追加两个可空字段，不影响现有字段。

- [ ] **Step 2: 添加外部通知方法**

在 `SnapshotRecorder` 类中追加两个方法，供 `match_reference` / `build_atmosphere_mapping` handler 调用：

```python
    def set_reference_image(self, path: str) -> None:
        """记录参考图路径（match_reference handler 调用）."""
        self._reference_path = path

    def set_mapping_path(self, path: str) -> None:
        """记录映射文件路径（build_atmosphere_mapping handler 调用）."""
        self._mapping_path = path
```

同时在 `__init__` 中初始化：

```python
        self._reference_path: str | None = None
        self._mapping_path: str | None = None
```

- [ ] **Step 3: 验证语法**

```bash
uv run python -c "from harness.observability.snapshotter import SnapshotRecorder; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add harness/observability/snapshotter.py
git commit -m "feat: add reference_image + mapping_path fields to SnapshotRecorder.write_session_json"
```

---

### Task 7: 最终验证

- [ ] **Step 1: 确认 evening-lighting.yaml 已删除、新 skills 存在**

```bash
ls skills/evening-lighting.yaml 2>&1 && echo "SHOULD NOT EXIST" || echo "OK - removed"
ls skills/match-atmosphere.yaml skills/scene-lighting.yaml skills/scene-verification.yaml
```
Expected: evening-lighting 不存在，另外三个存在

- [ ] **Step 2: 运行全量测试**

```bash
uv run pytest tests/ -v
```
Expected: 337+ passed

- [ ] **Step 3: 验证 SkillRegistry 能加载新 Skill**

```bash
uv run python -c "
from harness.context.skill_registry import SkillRegistry
r = SkillRegistry()
r.load_skills()
names = [s.name for s in r.list_skills()]
assert 'match-atmosphere' in names, f'match-atmosphere not found in {names}'
assert 'scene-lighting' in names, f'scene-lighting not found in {names}'
assert 'evening-lighting' not in names, f'evening-lighting should be removed'
print('All skills OK:', names)
"
```
Expected: `All skills OK: [...]`

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git diff --cached --stat
git commit -m "feat: complete reference image skills + instructions (Plan 0708 Part C)"
```
