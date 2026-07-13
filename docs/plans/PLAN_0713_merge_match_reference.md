# 合并 match_reference 和 vision_compare 为单一工具 — 实施计划

> **适用执行方式：** 可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务实施。

**目标：** 删除 `vision_compare`，保留唯一的双图对比工具 `match_reference(path)`。每次调用 = 新截图 + MiMo 9 维度差异 + 量化指标。LLM 根据自己刚做的操作 + 指标变化方向判断下一步。

**架构：** `match_reference(path)` — 无 component 参数，无条件分支。`vision_compare` 工具和 handler 删除。MiMo 定性判断与量化指标冲突时，以量化指标为准。

**技术栈：** Python 3.12+, async/await, MiMo mimo-v2.5-pro, pytest

---

## 背景与设计决策

### 问题

当前有两个工具在做双图对比：

| | match_reference | vision_compare |
|------|:--:|:--:|
| 截图 | 新截 | 复用 session 截图 |
| MiMo 对比 | 9 维度完整 | 单组件 ✓/≈/✗ |
| 量化指标 | ✅ R/B, 亮度, 饱和度 | ❌ |
| 调用成本 | ~22s, ~3000 tokens | ~10s, ~500 tokens |

LLM 天然倾向用更轻量的 `vision_compare` 做迭代，但 `vision_compare` 没有量化指标。结果是 LLM 永远得不到确定性的数据反馈（如 session 164992d7 所见——LLM 从未重新调 `match_reference`，仅依赖 MiMo 的定性判断导致红色螺旋）。

### 决策

1. **只保留一个双图对比工具**：`match_reference(path)`。消除 LLM 的二选一歧义。
2. **始终新截图**。不复用 session 截图——参考图对比需要最新视口状态。
3. **始终返回量化指标**。R/B 比值、亮度、饱和度、直方图相似度。
4. **不做单组件对比**。MiMo 无法从渲染画面中隔离单个组件的贡献——DirectionalLight、SkyAtmosphere、Fog 的视觉效果混在同一像素里。让 MiMo "仅关注 X 组件"是在要求它做一件做不到的事，`vision_compare` 的反复自相矛盾已验证了这一点。
5. **LLM 自主判断组件级方向**。LLM 知道自己刚调了什么组件（它刚调了 `set_properties`），拿到量化指标后自己判断 R/B 比值是向参考值收敛还是偏离——这比 MiMo 说 "✗ further" 可靠得多。
6. **冲突时量化优先**。"两者一致则高置信，不一致时以量化指标为准"。

---

## 涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `harness/server.py` | 删除 `vision_compare` handler 和工具注册 |
| 修改 | `skills/match-atmosphere.yaml` | 删除 vision_compare 引用，改信任层级，简化迭代步骤 |
| 删除 | 无文件删除（handler 内联删除） | — |

---

### Task 1: 删除 vision_compare handler

**文件：**
- 修改: `harness/server.py`

- [ ] **Step 1: 删除 vision_compare handler 块**

定位并删除 `if name == "vision_compare":` 整块（约 60 行，包含 `_session_reference` 读取、截图复用、question 构造、VisionSubAgent 调用、结果组装、`_log_harness_call`）。

- [ ] **Step 2: 删除 vision_compare 工具注册**

在 `list_tools()` 函数中，删除 `vision_compare` 的工具定义条目。该条目包含 `name`、`description`、`inputSchema` 三个字段。

- [ ] **Step 3: 更新注释**

将行末注释 `# ---- 参考图会话状态（match_reference / vision_compare 共享） ----` 改为 `# ---- 参考图会话状态 ----`。

- [ ] **Step 4: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py', encoding='utf-8').read()); print('OK')"
```

预期：`OK`

---

### Task 2: 更新 match_reference 输出文本

**文件：**
- 修改: `harness/server.py` — `match_reference` handler 的返回文本组装部分

**说明：** `match_reference` 的核心逻辑（截图、量化、MiMo 9 维度提问）不需要改。只在返回文本末尾追加两段提示：工具使用引导 + 冲突处理规则。

- [ ] **Step 1: 判断是否首次加载（据此决定提示强度）**

在 `_session_reference = {"b64": ref_b64, "path": str(ref_path)}` 之后，加一个标记：

```python
            _is_first_load = _session_reference.get("_loaded") is None
            _session_reference = {"b64": ref_b64, "path": str(ref_path), "_loaded": True}
```

- [ ] **Step 2: 在返回文本末尾追加提示**

在 `result_text = "\n".join(lines)` 之前，追加：

```python
    lines.append("")
    lines.append("---")
    lines.append("")
    if _is_first_load:
        lines.append(
            "在存在参考图的任务里，每轮迭代请使用 "
            f"match_reference(\"{ref_path_str}\") 获取对比反馈，"
            "不要用 vision_ask 做氛围对比。"
        )
        lines.append("")
        lines.append(
            "match_reference 每次返回量化指标（R/B、亮度、饱和度）——"
            "这是确定性像素计算，不受 VLM 主观判断影响，是最可靠的调整指南针。"
        )
        lines.append(
            "⚠ MiMo 分析与量化指标方向一致 → 高置信；"
            "不一致 → 以量化指标为准。"
        )
```

- [ ] **Step 3: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py', encoding='utf-8').read()); print('OK')"
```

预期：`OK`

---

### Task 3: 更新 match-atmosphere Skill

**文件：**
- 修改: `skills/match-atmosphere.yaml`

- [ ] **Step 1: 删除 tools_allowlist 中的 vision_compare**

```yaml
# 删除此行：
  - "vision_compare"
```

- [ ] **Step 2: 更新描述**

```yaml
description: "参考图驱动的场景氛围匹配：加载参考图 → 9 维度对比 → 按组件逐项调整 → 量化指标导航验证闭环"
```

- [ ] **Step 3: 更新 Step 2 — 维度数 8 → 9**

```yaml
  ### Step 2 — 加载参考图并对比
  调 match_reference("<path>")，返回 9 维度方向性差异清单 + 5 项量化指标。
```

- [ ] **Step 4: 重写 Step 3 — 用指标替代 vision_compare**

```yaml
  ### Step 3 — 逐组件调整（按此顺序：先独立，后联动）
  从 match_reference 的差异清单和 Step 1 的映射表中，确定要调的组件和属性。
  **调整顺序很重要：** 先调独立组件（雾、体积云），再调会"染色"全局的组件（天空大气、方向光）。

  对每个组件：
    a. 调 get_properties 获取当前值
    b. 根据差异方向调整属性（参考映射表中的属性名和维度标注）
    c. 调 set_properties 写入
    d. 调 match_reference("<path>") 获取新的量化指标（R/B 比值、亮度、饱和度）
       **以量化指标为准判断方向。**
       - R/B 比值向参考值收敛 → 方向正确，下一个组件
       - R/B 比值偏离参考值 → 方向错误，反向调整，回到 b
       - 指标无明显变化 → 换属性或加大幅度，回到 b
```

- [ ] **Step 5: 更新"提示"部分**

```yaml
  ### 提示
  - match_reference 每次返回量化指标（R/B、亮度、饱和度）——这是最可靠的调整指南针。
    量化指标是确定性像素计算，不受 VLM 主观判断影响。
    **MiMo 分析与量化指标方向一致 → 高置信；不一致 → 以量化指标为准。**
  - 每轮调用 match_reference 自动截图，无需手动 vision_screenshot。
  - 每轮迭代后 L2 读回会自动确认写入值（ReadbackInterceptor 已在链中）
  - 多实例组件（>1 个）优先调整最可能影响整体的那一个
  - MiMo 9 维度差异提供整体方向参考，但具体每步调整以量化指标为导航
```

---

### Task 4: 最终验证

- [ ] **Step 1: 确认无残留 vision_compare 引用**

```bash
rg "vision_compare" harness/ skills/ tests/ --no-heading
```

预期：无输出。

- [ ] **Step 2: 运行测试套件**

```bash
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

预期：全部通过（`vision_compare` 无测试引用，不影响测试数量）。

- [ ] **Step 3: 提交**

```bash
git add harness/server.py skills/match-atmosphere.yaml
git commit -m "refactor: 删除 vision_compare，match_reference 为唯一双图对比工具

match_reference(path) — 始终新截图 + MiMo 9 维度 + 量化指标。
不做单组件对比：MiMo 无法从渲染画面中隔离单个组件贡献。
LLM 根据自己刚做的操作 + 指标变化方向判断下一步。
MiMo 与量化指标冲突时以量化为准。

更新 match-atmosphere Skill：量化指标 > MiMo。

Closes: docs/plans/PLAN_0713_merge_match_reference.md
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审清单

1. **需求覆盖：**
   - [x] 删除 vision_compare handler + 注册（Task 1）
   - [x] match_reference 输出：首次引导 + 量化优先提示 + 工具使用提醒（Task 2）
   - [x] 更新 Skill：删 vision_compare 引用 + 量化导航（Task 3）
   - [x] 不做 component 参数——无意义

2. **占位符检查：** 无 TBD/TODO。

3. **影响分析：**
   - `vision_compare` 无测试引用，删除安全
   - `match_reference` 外部接口不变（`path` 参数不变）
   - Skill 从"MiMo 三态判定驱动"改为"量化指标导航"，LLM 信任层级翻转
   - 与 vision_ask session 正交——match_reference 每次自包含，vision_ask 同图多问
