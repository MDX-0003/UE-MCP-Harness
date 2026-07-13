# PLAN 0708 — 参考图氛围匹配设计

**状态：** 设计完成，待实现
**依赖：** Issue 016 Part A (ReadbackInterceptor) — 写后自动读回 + 徽章告警
**关联：** [[016-readback-and-reference-image]]

## 动机

旧 Issue 016 Part B 的规划「Vision 双图对比 → 非结构化差异 → LLM 自行拆步骤」存在两个根本问题：

1. **随机性过高**：Vision 每次返回的自由文本差异描述不一致，LLM 拆解步骤的方式也不一致
2. **断桥**：Vision 不认得 UE 参数名，无法从"色温偏冷"推导出该调哪一个 tool 的哪个属性

### 核心洞察

场景氛围由 **5 类固定组件**控制：DirectionalLight、SkyAtmosphere、ExponentialHeightFog、VolumetricCloud、PostProcessVolume。这 5 类的属性名随 UE 版本变化，但"哪些类控制氛围"是领域常数。

因此 Harness 可以：
- **固定对比维度**（8 个，预写在代码里）
- **自动生成参数映射**（动态扫描 5 类组件 → MiMo 筛选氛围属性 → 写入 mapping.md）
- **三态验证节点**（vision_compare 返回 ✓/≈/✗，不要求 Vision 理解参数名）

LLM 得到的是结构化的方向性差异 + 已完成的参数映射 + 确定性的验证反馈。它只需做"根据差异和映射决定调哪个参数的哪个方向"——这正是 LLM 擅长的事。

---

## 新增 Harness 工具（3 个）

### 1. `build_atmosphere_mapping()`

**职责**：扫描场景中 5 类氛围组件的可用属性，生成「视觉维度 → UE 属性」映射表。

**内部流程**：

```
1. ue_client 批量 find_actors(5 种类型)
   - *DirectionalLight*, *SkyAtmosphere*, *ExponentialHeightFog*,
     *VolumetricCloud*, *PostProcessVolume*
   - 多实例标记（>1 个时提示 LLM 确认），空实例标记"需创建"

2. 对每个找到的 Actor 调 list_properties → 获取属性名列表

3. 组装纯文本 prompt 发给 MiMo：
   "以下是从 UE 场景中提取的 5 类氛围组件及其所有属性名。
    请筛选与氛围视觉表现相关的属性（排除碰撞、Tick、调试等无关属性）。
    对每个属性标注其影响的高维维度：brightness / contrast / color_temp /
    color_cast / saturation / haze / shadow_direction / sky。
    输出 JSON。"

4. MiMo 返回结构化 JSON → 写入 {session_dir}/atmosphere-mapping.md
```

**返回**：

```
氛围组件扫描完成：
  DirectionalLight:      1 个 (DirectionalLight_1)
  SkyAtmosphere:         1 个 (SkyAtmosphere_0)
  ExponentialHeightFog:  1 个 (ExponentialHeightFog_0)
  PostProcessVolume:     1 个 (PostProcessVolume_0)
  VolumetricCloud:       未找到 → 请调 add_to_scene_from_class 创建

映射已生成：{N} 个氛围相关属性 → {session_dir}/atmosphere-mapping.md
```

**调用频率**：每会话一次（场景中 5 类 Actor 不常变）。

---

### 2. `match_reference(path: str)`

**职责**：加载参考图，与当前场景做 8 维度整体对比。

**内部流程**：

```
1. 读取参考图文件 → PIL Image 对象
2. 截当前 UE 视口 → PIL Image 对象
3. 两张图各自转 base64 → 组装 Vision 消息（双图 + 8 维度固定提问）→ 发给 MiMo
4. 同时（Step 1-2 得到的 PIL Image 未被释放）调 compute_match_metrics(ref, cur)
   → 5 项全图统计指标（<10ms，不阻塞 MiMo）
5. 解析 MiMo 结构化回答 → 差异清单
6. 指标写入 {session_dir}/match-metrics.json
7. 参考图拷贝到 {session_dir}/references/
8. 对比结果 + 指标写入 session log
```

关键设计：Step 3-4 共享 Step 1-2 的 PIL Image 对象，避免 base64→文件→PIL 的重复 I/O。Step 4 在 MiMo 调用期间并行执行（计算量远小于 API 延迟），不影响响应速度。

**8 维度固定提问：**

```
请从以下 8 个维度比较当前截图与参考图的差异。每个维度只输出方向性判定，不需要描述绝对值：

亮度 (Brightness):       darker / similar / brighter
对比度 (Contrast):       lower / similar / higher
色温 (Color Temperature): cooler / similar / warmer
色调偏移 (Color Cast):    none / 偏X色
饱和度 (Saturation):      less_saturated / similar / more_saturated
大气密度 (Haze):          clearer / similar / hazier
阴影方向 (Shadow Direction): 方向描述 + 是否一致
天空表现 (Sky):           颜色/云量/渐变的差异方向

每个判定配一句话佐证（你看到什么让你这样判断）。
```

**返回**：

```
参考图：sunset_beach.png (1024×768, R/B=1.42 偏暖, 亮度=98.5)

MiMo 8 维度差异：
  色温:     warmer    （参考图偏暖，当前偏冷。参考图物体表面有橙色反光）
  大气密度:  hazier    （参考图远处山体轮廓更模糊）
  天空:     darker + 偏紫  （参考图天空上方深蓝过渡到地平线橙紫）
  亮度:     similar
  对比度:   similar
  饱和度:   similar
  色调偏移:  none
  阴影方向:  similar

量化指标（全图统计，不受视点移动影响）：
         参考图    当前    差异
  亮度     98.5    132.7   +34.7%  当前更亮
  对比度   45.2     38.1   -15.7%
  色温    R/B=1.42 R/B=0.89 -37.3%  当前偏冷
  饱和度   78.3     65.1   -16.9%
  直方图相似度  0.67  (0→完全不同, 1→完全一致)

详细指标 → {session_dir}/match-metrics.json

氛围组件状态：
  DirectionalLight, SkyAtmosphere, ExponentialHeightFog,
  PostProcessVolume → 已存在
  VolumetricCloud → 需创建

下一步：如尚未生成参数映射，请调 build_atmosphere_mapping()。
完成后对照映射和差异调整各组件。交叉参考 MiMo 分析和量化指标——两者一致则高置信，
不一致则以 MiMo 为主、量化指标为参考修正。
```

**调用频率**：每次用户更换参考图时触发。

---

### 3. `vision_compare(component: str)`

**职责**：针对单个氛围组件做双图方向性判定。**不截新图时复用 session 内已有截图，不消耗额外截图 token。**

**参数**：

```json
{
  "component": {
    "type": "string",
    "enum": ["DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
             "VolumetricCloud", "PostProcessVolume"],
    "description": "要对比的组件"
  },
  "reuse_screenshot": {
    "type": "boolean",
    "default": true,
    "description": "复用 session 内最新截图。false 时重截当前视口。"
  }
}
```

**内部流程**：

```
1. 取当前截图（复用或新截）
2. 组装 Vision 消息（参考图 + 当前图 + 固定提问）→ 发给 MiMo
3. 解析三态结论
```

**固定提问：**

```
仅关注 {component} 对画面氛围的影响，忽略其他组件的差异。
当前场景在 {component} 的表现，与参考图相比：
  ✓ closer — 更接近参考图了
  ≈ similar — 没有明显变化
  ✗ further — 更远离参考图了

选择 ✓/≈/✗，给一句佐证。
```

**返回**（`VisionVerdict` 结构化输出，与现有 Vision 工具格式一致）：

```json
{
  "answer": "当前场景在 DirectionalLight 的表现更接近参考图了。光色偏暖，墙面开始出现参考图中的橙色反光。",
  "confidence": "high",
  "caveats": [],
  "observations": [
    {"what": "DirectionalLight 方向性变化", "finding": "✓ closer", "confidence": "high"}
  ],
  "need_more_info": false,
  "question": ""
}
```

**调用频率**：每组件每轮迭代。`reuse_screenshot=true` 时只消耗文本 token，可高频调用。

---

## 量化指标（`match_reference` 附属输出）

### 设计目标

MiMo 的 8 维度分析是主观判断，LLM 需要一个独立数据源来交叉校验。量化指标提供像素无关的全图统计值——两张图不需要从同一视点拍摄，因为它们不比较像素位置，只比较全局分布。

### 模块：`harness/verification/metrics.py`

纯计算模块，零新依赖（Pillow 已在 `pyproject.toml`）。不涉及 UE、MiMo、MCP 协议，只接受两个 Pillow `Image` 对象。

**入口函数：**

```python
def compute_match_metrics(ref: Image, cur: Image) -> dict:
    """计算参考图与当前图的 5 项量化指标。

    Args:
        ref: Pillow Image，参考图（RGB）
        cur: Pillow Image，当前截图（RGB）

    Returns:
        {
            "luminance":       {"ref": float, "cur": float, "delta_pct": float},
            "contrast":        {"ref": float, "cur": float, "delta_pct": float},
            "color_temperature": {"ref_r_b_ratio": float, "cur_r_b_ratio": float},
            "saturation":      {"ref": float, "cur": float, "delta_pct": float},
            "histogram_correlation": float  # 0..1
        }
    """
```

如果输入图像不是 RGB 或尺寸差异过大（>2x），函数抛出 `ValueError`。调用方（`match_reference` handler）将其捕获，在响应中追加 `⚠ 量化指标计算失败: {reason}` 而不阻断 MiMo 分析。

### 5 项指标详解

| # | 指标 | 计算方式 | 对应 MiMo 维度 | 视点敏感度 |
|---|------|---------|:---:|:---:|
| 1 | **`luminance`** | 逐像素加权亮度 `0.299R + 0.587G + 0.114B` → 全图均值 | 亮度 (Brightness) | ✅ 统计级，无视点 |
| 2 | **`contrast`** | 加权亮度的全图标准差（RMS contrast） | 对比度 (Contrast) | ✅ 统计级 |
| 3 | **`color_temperature`** | `R̄ / B̄`——红色通道均值 ÷ 蓝色通道均值。>1 偏暖，<1 偏冷。等亮度纯白光源 R/B≈1 | 色温 (Color Temperature) | ✅ 统计级 |
| 4 | **`saturation`** | RGB→HSV 后 S 通道均值。范围 0（灰）到 255（纯色） | 饱和度 (Saturation) | ✅ 统计级 |
| 5 | **`histogram_correlation`** | 三通道 256-bin 直方图拼接（768-bin）→ 余弦相似度。0=完全不同，1=分布完全一致 | 色调偏移 / 天空表现（辅助验证） | ✅ 分布级 |

后 3 项（色调偏移、阴影方向、大气密度、天空表现）没有量化指标——它们需要语义理解或图像分割，目前只由 MiMo 覆盖。

### 为什么不用 SSIM？

SSIM 比较的是相同像素位置的局部结构——对视点移动零容忍。在这个"氛围匹配"场景里，两张图可能因相机微调而有不同的像素排列。全图统计指标（均值、标准差、通道比、直方图）仅依赖像素值分布而不依赖位置，天然免疫视点变化。

### 指标与 MiMo 的交叉校验

| 场景 | LLM 应如何解读 |
|------|---------------|
| MiMo 说偏冷 + R/B 比也偏低 | 双通道一致 → 高置信度，放心调暖 |
| MiMo 说偏冷 + R/B 比接近 | MiMo 可能被色调偏移误导 → 再看 histogram_correlation |
| MiMo 说 similar + 指标差 30% | 以 MiMo 为主（Vision 比像素统计更准确），但心里有数 |
| 指标计算失败 | ⚠ 注记在响应中，MiMo 分析仍然有效 |

### `match-metrics.json` 结构

```json
{
  "reference_path": "sunset_beach.png",
  "reference_size": [1024, 768],
  "current_size": [1920, 1080],
  "computed_at": "2026-07-08T15:30:00Z",
  "luminance": {
    "ref": 98.5,
    "cur": 132.7,
    "delta_pct": 34.7,
    "unit": "weighted grayscale (0-255)",
    "note": "当前场景更亮"
  },
  "contrast": {
    "ref": 45.2,
    "cur": 38.1,
    "delta_pct": -15.7,
    "unit": "stddev of weighted grayscale",
    "note": "当前场景更灰/平"
  },
  "color_temperature": {
    "ref_r_b_ratio": 1.42,
    "cur_r_b_ratio": 0.89,
    "unit": "red_mean / blue_mean",
    "note": "ref>1 偏暖, cur<1 偏冷"
  },
  "saturation": {
    "ref": 78.3,
    "cur": 65.1,
    "delta_pct": -16.9,
    "unit": "HSV S channel mean (0-255)",
    "note": "当前场景饱和度更低"
  },
  "histogram_correlation": {
    "value": 0.67,
    "unit": "cosine similarity (0-1)"
  }
}
```

**调用频率**：随 `match_reference` 每次调用自动计算。不单独暴露为工具。

### 新增：`match-atmosphere` Skill

```yaml
name: match-atmosphere
description: "参考图氛围匹配：拿到参考图后，按 5 组件逐项调整场景氛围至逼近参考图"
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
  # Harness 工具
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
  并生成维度→属性的映射表。映射保存在 {session_dir}/atmosphere-mapping.md。

  ### Step 2 — 加载参考图并对比
  调 match_reference("<path>")，返回 8 维度方向性差异清单。

  ### Step 3 — 逐组件调整
  阅读 atmosphere-mapping.md，找到差异对应的 UE 属性。
  对每个有差异的组件：
    a. 调 get_properties 获取当前值
    b. 根据差异方向调整属性（参考 mapping 里的维度标注）
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

### 废弃：`evening-lighting` Skill

删除 `skills/evening-lighting.yaml`。

理由：它的硬编码参数值（如 `RGB(1.0, 0.7, 0.4)`、`Intensity 30-50%`）被参考图机制完全覆盖。用户想调黄昏时，给出黄昏参考图 → `match_reference` → 得到场景特定的差异方向和幅度——比预写参数更灵活且跨场景可复用。

如其触发词（"黄昏""傍晚""sunset"）被匹配到，LLM 可引导用户提供参考图并使用 `match-atmosphere` 流程。

### 瘦身：`scene-verification` Skill

保留工具白名单过滤功能。SOP 步骤已精简进 Base instruction，Skill YAML 的 `steps` 可缩为一行引用 Base instruction 的内容。不对 LLM 行为产生额外影响。

---

## Instructions 重构

### 原则

- Base instructions 只放通用内容，≤20 行
- 领域知识分别放入对应的 Skill
- 氛围优先、局部灯光次之——通过 Skill 命名和排序暗示优先级

### 新 Base instructions（替换现有 `instructions.md`）

```
你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。

## 工作模式
当前有两种主要工作模式，根据用户意图选择：

  氛围优先 → activate_skill("match-atmosphere")
    参考图驱动的整体场景氛围调整：光照、天空、雾、云、后处理

  局部调整 → activate_skill("scene-lighting")
    单灯/少灯的精确属性调整：位置、颜色、强度、旋转

  自由探索 → deactivate_skill（或保持不激活任何 Skill）
    所有工具可用，适合查询、浏览、非标准操作

## 通用验证 SOP
任何场景修改后：
  1. 修改后的写入值由 Harness 自动读回验证（⚠ 徽章提示失配）
  2. 相机定位：用预设角度轮换确保视口对准目标
     pitch=-25 yaw=45 / pitch=-20 yaw=90 / pitch=-55 yaw=0 / pitch=-15 yaw=0
  3. vision_screenshot(question="具体问题") 做视觉验证
  4. 需要时用 vision_ask 追问，完成后 vision_reset 闭环

  灯光验证特别注意：在被照亮的表面上判断效果，不看编辑器图标颜色。
```

### 新增：`scene-lighting` Skill（从旧 instruction 迁移）

```yaml
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

---

## 完整用户流程

```
用户: "参考 sunset_beach.png 这张图调氛围"

LLM → activate_skill("match-atmosphere")
     "match-atmosphere Skill 已激活"

LLM → build_atmosphere_mapping()
     "扫描完成，55 个属性已映射 → atmosphere-mapping.md"
     
LLM → match_reference("sunset_beach.png")
     "参考图 R/B=1.42 偏暖。MiMo: 色温偏冷、雾偏淡、天空偏蓝 — 3 项差异。
      量化: 色温 R/B=0.89 (-37.3%)，吻合。亮度 +34.7%，待关注。
      → match-metrics.json"
     
LLM 读 atmosphere-mapping.md:
     色温 → DirectionalLight.LightColor, PostProcess.WhiteBalance
     大气密度 → ExponentialHeightFog.FogDensity
     天空 → SkyAtmosphere.*, SkyLight.*

LLM → set_properties(DirectionalLight, LightColor={r:1, g:0.75, b:0.5})
LLM → vision_compare("DirectionalLight") → ✓ closer

LLM → set_properties(ExponentialHeightFog, FogDensity=增加)
LLM → vision_compare("ExponentialHeightFog") → ≈ similar
LLM → 再调 FogInscatteringColor 偏暖
LLM → vision_compare("ExponentialHeightFog") → ✓ closer

LLM → set_properties(SkyAtmosphere, ...)
LLM → vision_compare("SkyAtmosphere") → ✓ closer

LLM → vision_screenshot → 整体确认

---

用户: "现在把那个点光源调亮一点"  ← 切换任务

LLM → deactivate_skill
LLM → activate_skill("scene-lighting")
     → 按灯光 SOP 找到点光源 → 空间检查 → 调 Intensity → 验证

---

用户: "再参考 night.png 调氛围"    ← 切换回氛围

LLM → deactivate_skill
LLM → activate_skill("match-atmosphere")
LLM → match_reference("night.png")  ← 只需重对比，mapping 复用
```

---

## 涉及文件

| 文件 | 改动 |
|------|------|
| `harness/server.py` | 新增 3 个 Harness 自有工具 handler：`build_atmosphere_mapping`、`match_reference`、`vision_compare`。`match_reference` handler 中集成 `compute_match_metrics()` 调用 |
| `harness/verification/metrics.py` | **新增**。5 项全图统计指标计算（纯 Pillow，零新依赖）。入口 `compute_match_metrics(ref, cur) → dict` |
| `harness/verification/vision_agent.py` | `VisionSubAgent` 新增 `compare_with_reference()` 方法（双图 + 单组件三态判定） |
| `harness/config.py` | 无新字段。3 个工具 + metrics 通过现有 `ue_client` 和 `vision_agent` 通道工作 |
| `skills/match-atmosphere.yaml` | 新增 |
| `skills/scene-lighting.yaml` | 新增（旧 instruction 灯光 SOP 迁移） |
| `skills/evening-lighting.yaml` | **删除** |
| `skills/scene-verification.yaml` | 瘦身（移除冗余 SOP 步骤文本） |
| `harness/context/prompt.py` | `SystemContextProvider` 重写 Base instruction 文本（精简 + 双模式引导） |
| `harness/observability/snapshotter.py` | `write_session_json` 增加 `reference_image` 和 `mapping_path` 字段 |
| `tests/test_verification_interceptor.py` | 新增 `TestReferenceImageTools`（覆盖 3 个工具的核心路径） |
| `tests/test_metrics.py` | **新增**。覆盖 `compute_match_metrics` 的 5 个指标 + 边界（非 RGB 输入、尺寸差异过大、单色图像） |
| `docs/issues/016-readback-and-reference-image.md` | 更新 Part B（标记为"已重新设计"，指向本文档） |

---

## 不做的事

- 不给主 LLM 加多模态通道
- 不让 Vision 直接输出 UE 工具调用
- 不做参考图的自动检索/生成
- 不做静态参数映射表（动态发现替代）
- 不做 SSIM —— 像素位置敏感，在氛围匹配场景里视点变化导致失效；全图统计指标（均值/标准差/通道比）对视点免疫，更适合此场景
- 不做指标单独暴露为工具——随 `match_reference` 自动计算，不增加调用摩擦
- 不实现映射的跨会话持久化（未来记忆系统再做）
- `evening-lighting` Skill 的触发词不迁移到 `match-atmosphere`——用户请求"黄昏"时 LLM 在自由探索模式下自行判断是否需要参考图
