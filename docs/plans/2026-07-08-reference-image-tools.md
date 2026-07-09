# 参考图工具 + Vision Agent 扩展 Implementation Plan

> **依赖:** Plan 1（`compute_match_metrics` from `harness/verification/metrics.py`）
> **被依赖:** Plan 3（Skills 引用这 3 个 tool 名）

**Goal:** 在 `VisionSubAgent` 上新增 `compare_with_reference()` 和 `classify()` 两个方法，在 `server.py` 注册 3 个新 Harness 工具（`build_atmosphere_mapping`、`match_reference`、`vision_compare`）。

**Architecture:** 最小扩展——不新建 handler 模块。`compare_with_reference()` 和 `classify()` 直接作为 `VisionSubAgent` 的新方法，复用 `_call_vision_api` + `_parse_verdict` / `_extract_json_object`。3 个 tool handler 挂在 `call_tool` 的现有 `if name == ...` 路由下，和已有的 `vision_*` 系列并存。

**Tech Stack:** Python 3.12+, httpx, PIL (Pillow), pytest + pytest-asyncio + pytest-mock

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `harness/verification/vision_agent.py` | **扩展** | `VisionSubAgent.compare_with_reference()` + `classify()` 方法 |
| `harness/server.py` | **扩展** | `build_server`: 注册 3 个 tool schema；`call_tool`: 加 3 个 handler；模块级 helper `_render_mapping_markdown()` |
| `tests/test_verification_interceptor.py` | **扩展** | 新增 `TestReferenceImageTools` 类 |

---

### Task 1: `compare_with_reference()` — 双图对比方法

**Files:**
- Modify: `harness/verification/vision_agent.py`

- [ ] **Step 1: 阅读现有 `VisionSubAgent.check()` 确认复用点**

`VisionSubAgent.check()`（`vision_agent.py:92`）的流程：
```
check(image_b64, question, scene_context) → 组装 messages → _call_vision_api → _parse_verdict → VisionVerdict
```

`compare_with_reference()` 的区别仅在于 messages 包含两张图（参考图 + 当前图）且不追加到 `self._history`。

- [ ] **Step 2: 实现 `compare_with_reference()`**

在 `VisionSubAgent` 类中，`continue_with_question` 方法之后（`vision_agent.py:226`）插入：

```python
    async def compare_with_reference(
        self,
        ref_image_b64: str,
        cur_image_b64: str,
        question: str,
        scene_context: str = "",
    ) -> VisionVerdict:
        """双图对比——参考图 vs 当前截图，不记入 Session 对话历史。

        与 check() 的区别：
          - 同时发送两张图（参考图 + 当前图），而非单张
          - 不追加到 self._history（单次对比，不影响 Session 内的多轮对话）
          - 复用 _call_vision_api + _parse_verdict

        Args:
            ref_image_b64: 参考图 base64 PNG
            cur_image_b64: 当前截图 base64 PNG
            question: 对比提问
            scene_context: 可选场景上下文

        Returns:
            VisionVerdict
        """
        self._call_count += 1

        user_message = question
        if scene_context:
            user_message += f"\n\n场景上下文：\n{scene_context}"
        user_message += _VISION_FORMAT_REMINDER

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": ref_image_b64,
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": cur_image_b64,
                        },
                    },
                    {"type": "text", "text": user_message},
                ],
            }
        ]

        try:
            response = await _call_vision_api(self.config, messages)
            return _parse_verdict(response)
        except Exception as e:
            logger.error("Vision 双图对比失败: %s", e)
            return VisionVerdict(
                answer=f"Vision 双图对比失败: {e}",
                confidence="low",
                caveats=["请检查 Vision API key 和网络连接后重试"],
            )
```

- [ ] **Step 3: 验证语法**

```bash
uv run python -c "from harness.verification.vision_agent import VisionSubAgent; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add harness/verification/vision_agent.py
git commit -m "feat: add VisionSubAgent.compare_with_reference() for dual-image comparison"
```

---

### Task 2: `classify()` — 纯文本 MiMo 分类方法

**Files:**
- Modify: `harness/verification/vision_agent.py`

**设计决策（经 grill 确认）：**
- 放在 `VisionSubAgent` 上作为方法（复用 `self.config` + `_call_vision_api`）
- 纯文本——无 image block，发 prompt 收 JSON dict
- 不追加到 `self._history`
- 复用已有 `_extract_json_object()`（`vision_agent.py:411`）

- [ ] **Step 1: 实现 `classify()`**

在 `VisionSubAgent` 类中，`compare_with_reference` 方法之后插入：

```python
    async def classify(self, prompt: str) -> dict[str, Any]:
        """纯文本分类——发 prompt 给 MiMo，返回 parsed JSON dict。

        与 check() 的区别：
          - 无图片输入（纯文本消息）
          - 不追加到 self._history（单次分类，不影响 Session 多轮对话）
          - 返回 raw dict 而非 VisionVerdict

        Args:
            prompt: 纯文本 prompt，末尾应包含 JSON 输出格式约束

        Returns:
            parsed JSON dict

        Raises:
            ValueError: MiMo 返回无法解析为 JSON
        """
        self._call_count += 1

        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ]

        try:
            response = await _call_vision_api(self.config, messages)
        except Exception as e:
            raise ValueError(f"MiMo 纯文本调用失败: {e}") from e

        json_str = _extract_json_object(response)
        if json_str is None:
            raise ValueError(
                f"MiMo 返回中未找到 JSON 对象。"
                f"原始返回前 300 字符: {response[:300]}"
            )

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"MiMo JSON 解析失败: {e}\n"
                f"提取的 JSON 文本前 200 字符: {json_str[:200]}"
            ) from e
```

需要在文件顶部确认 `json` 和 `Any` 已导入。`_extract_json_object` 已在 `vision_agent.py:411` 定义。

- [ ] **Step 2: 验证语法**

```bash
uv run python -c "from harness.verification.vision_agent import VisionSubAgent; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add harness/verification/vision_agent.py
git commit -m "feat: add VisionSubAgent.classify() for text-only MiMo classification"
```

---

### Task 3: `vision_compare` tool — server 注册 + handler

**Files:**
- Modify: `harness/server.py`

- [ ] **Step 1: 在 `list_tools` 注册 tool schema**

在 `build_server` → `list_tools` 中，`vision_status` 的 tool 注册代码之后追加：

```python
        result.append(Tool(
            name="vision_compare",
            description=(
                "双图对比验证——参考图 vs 当前截图。针对单个氛围组件做三态判定"
                "（✓ closer / ≈ similar / ✗ further）。"
                "默认复用 Session 内最新截图，不消耗额外截图 token。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "enum": ["DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
                                 "VolumetricCloud", "PostProcessVolume"],
                        "description": "要对比的氛围组件",
                    },
                    "reuse_screenshot": {
                        "type": "boolean",
                        "default": True,
                        "description": "复用 Session 内最新截图。false 时需先调 vision_screenshot。",
                    },
                },
                "required": ["component"],
            },
        ))
```

- [ ] **Step 2: 在 `call_tool` 添加 handler**

在 `vision_status` handler 之后追加：

```python
        if name == "vision_compare":
            t0 = time.monotonic()
            if vision_session_manager is None:
                return CallToolResult(content=[TextContent(
                    type="text", text="Vision Session Manager 未初始化。",
                )], isError=True)
            session = vision_session_manager.get_active()
            if session is None or not session.screenshots:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text="没有可复用的截图。请先调 vision_screenshot 获取当前视口截图。",
                )], isError=True)

            component = arguments.get("component", "")
            if component not in (
                "DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
                "VolumetricCloud", "PostProcessVolume",
            ):
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"无效的 component: '{component}'。"
                         f"可选: DirectionalLight, SkyAtmosphere, "
                         f"ExponentialHeightFog, VolumetricCloud, PostProcessVolume",
                )], isError=True)

            # 复用 Session 内最新截图
            latest_ss = session.screenshots[-1]
            cur_b64 = latest_ss.b64

            # 参考图来自 _session_reference（match_reference handler 存入）
            ref_b64 = _session_reference.get("b64") if _session_reference else None
            if ref_b64 is None:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text="未找到参考图。请先调 match_reference(path) 加载参考图。",
                )], isError=True)

            # 单组件三态判定提问
            question = (
                f"仅关注 {component} 对画面氛围的影响，忽略其他组件的差异。\n"
                f"当前场景在 {component} 的表现，与参考图相比：\n"
                f"  ✓ closer — 更接近参考图了\n"
                f"  ≈ similar — 没有明显变化\n"
                f"  ✗ further — 更远离参考图了\n\n"
                f"选择 ✓/≈/✗，给一句佐证。"
            )

            agent = VisionSubAgent(config)
            try:
                verdict = await agent.compare_with_reference(
                    ref_b64, cur_b64, question,
                )
            except Exception as e:
                duration_ms = (time.monotonic() - t0) * 1000
                err_text = f"vision_compare 失败: {e}"
                await _log_harness_call(name, arguments, err_text, duration_ms, error=e)
                return CallToolResult(content=[TextContent(
                    type="text", text=err_text,
                )], isError=True)

            duration_ms = (time.monotonic() - t0) * 1000
            result_text = json.dumps({
                "answer": verdict.answer,
                "confidence": verdict.confidence,
                "caveats": verdict.caveats,
                "observations": verdict.observations,
            }, ensure_ascii=False, indent=2)
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

- [ ] **Step 3: 验证 server 语法**

```bash
uv run python -c "from harness.server import build_server; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add harness/server.py
git commit -m "feat: add vision_compare tool (dual-image per-component tri-state verdict)"
```

---

### Task 4: `match_reference` tool — server 注册 + handler

**Files:**
- Modify: `harness/server.py`

**设计说明：** `match_reference` 需要存储参考图 b64 供后续 `vision_compare` 复用。用 `build_server` closure 变量 `_session_reference`，和已有的 `_cached_raw_tools` 模式一致。

- [ ] **Step 1: 在 `build_server` 开始处添加参考图状态**

在 `_cached_raw_tools` 声明之后插入：

```python
    # ---- 参考图会话状态（match_reference / vision_compare 共享） ----
    _session_reference: dict[str, Any] = {}
```

- [ ] **Step 2: 在 `list_tools` 注册 tool schema**

在 `vision_compare` 注册之后追加：

```python
        result.append(Tool(
            name="match_reference",
            description=(
                "加载参考图，与当前 UE 视口做 8 维度整体对比（亮度/对比度/色温/色调偏移/"
                "饱和度/大气密度/阴影方向/天空表现）。返回结构化方向性差异 + 5 项量化指标。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "参考图文件路径（PNG/JPEG）",
                    },
                },
                "required": ["path"],
            },
        ))
```

- [ ] **Step 3: 在 `call_tool` 添加 handler**

在 `vision_compare` handler 之后追加：

```python
        if name == "match_reference":
            t0 = time.monotonic()
            ref_path_str = arguments.get("path", "")

            # 1. 加载参考图
            try:
                from PIL import Image as PILImage
                from pathlib import Path as _Path
                ref_path = _Path(ref_path_str).expanduser().resolve()
                if not ref_path.exists():
                    return CallToolResult(content=[TextContent(
                        type="text", text=f"参考图不存在: {ref_path}",
                    )], isError=True)
                ref_img = PILImage.open(ref_path).convert("RGB")
            except Exception as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"加载参考图失败: {e}",
                )], isError=True)

            # 2. 截当前视口
            try:
                from harness.verification.capturer import capture as capturer_capture
                max_w, max_h = config.vision_max_size
                screenshot = await capturer_capture(
                    ue_client, max_w, max_h, mode="viewport",
                )
                cur_b64 = screenshot.data_b64
            except Exception as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"截图失败: {e}",
                )], isError=True)

            # 3. 参考图 → base64
            import io as _io
            buf = _io.BytesIO()
            ref_img.save(buf, format="PNG")
            ref_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            # 保存状态（供 vision_compare 复用）
            nonlocal _session_reference
            _session_reference = {"b64": ref_b64, "path": str(ref_path)}

            # 4. 量化指标（与 MiMo 调用并行推进，纯计算 <10ms）
            from harness.verification.metrics import compute_match_metrics
            try:
                cur_img = _b64_to_pil(cur_b64)
            except Exception:
                cur_img = None

            metrics_error: str | None = None
            metrics_result = None
            if cur_img is not None:
                try:
                    metrics_result = compute_match_metrics(ref_img, cur_img)
                    _session_reference["metrics"] = metrics_result
                except Exception as e:
                    logger.warning("量化指标计算失败（非致命）: %s", e)
                    metrics_error = str(e)
            else:
                metrics_error = "当前截图无法解码为 PIL Image"

            # 5. MiMo 8 维度双图对比
            question = (
                "请从以下 8 个维度比较当前截图与参考图的差异。"
                "每个维度只输出方向性判定，不需要描述绝对值：\n\n"
                "亮度 (Brightness):       darker / similar / brighter\n"
                "对比度 (Contrast):       lower / similar / higher\n"
                "色温 (Color Temperature): cooler / similar / warmer\n"
                "色调偏移 (Color Cast):    none / 偏X色\n"
                "饱和度 (Saturation):      less_saturated / similar / more_saturated\n"
                "大气密度 (Haze):          clearer / similar / hazier\n"
                "阴影方向 (Shadow Direction): 方向描述 + 是否一致\n"
                "天空表现 (Sky):           颜色/云量/渐变的差异方向\n\n"
                "每个判定配一句话佐证（你看到什么让你这样判断）。"
            )

            agent = VisionSubAgent(config)
            try:
                verdict = await agent.compare_with_reference(ref_b64, cur_b64, question)
            except Exception as e:
                duration_ms = (time.monotonic() - t0) * 1000
                err_text = f"match_reference Vision 调用失败: {e}"
                await _log_harness_call(name, arguments, err_text, duration_ms, error=e)
                return CallToolResult(content=[TextContent(
                    type="text", text=err_text,
                )], isError=True)

            # 6. 组装返回文本
            duration_ms = (time.monotonic() - t0) * 1000
            ref_w, ref_h = ref_img.size

            lines = [
                f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
                "",
                "MiMo 8 维度差异：",
                verdict.answer,
            ]

            # 量化指标表格
            if metrics_result:
                m = metrics_result
                lines.append("")
                lines.append("量化指标（全图统计，不受视点移动影响）：")
                lines.append(f"{'':>12} {'参考图':>8} {'当前':>8} {'差异':>10}")
                lines.append(
                    f"{'亮度':>12} {m['luminance']['ref']:>8.1f} "
                    f"{m['luminance']['cur']:>8.1f} {m['luminance']['delta_pct']:>+9.1f}%"
                )
                lines.append(
                    f"{'对比度':>12} {m['contrast']['ref']:>8.1f} "
                    f"{m['contrast']['cur']:>8.1f} {m['contrast']['delta_pct']:>+9.1f}%"
                )
                ct = m["color_temperature"]
                lines.append(
                    f"{'色温':>12} {'R/B=' + str(ct['ref_r_b_ratio']):>8} "
                    f"{'R/B=' + str(ct['cur_r_b_ratio']):>8}"
                )
                lines.append(
                    f"{'饱和度':>12} {m['saturation']['ref']:>8.1f} "
                    f"{m['saturation']['cur']:>8.1f} {m['saturation']['delta_pct']:>+9.1f}%"
                )
                lines.append(
                    f"{'直方图相似度':>12} {'':>8} {'':>8} "
                    f"{m['histogram_correlation']:>10.2f} (0→完全不同, 1→完全一致)"
                )
            elif metrics_error:
                lines.append(f"\n⚠ 量化指标计算失败: {metrics_error}")
                lines.append("MiMo 分析仍然有效。")

            # 提醒
            lines.append("")
            lines.append("下一步：如尚未生成参数映射，请调 build_atmosphere_mapping()。")
            lines.append("完成后对照映射和差异调整各组件。交叉参考 MiMo 分析和量化指标——")
            lines.append("两者一致则高置信，不一致则以 MiMo 为主、量化指标为参考修正。")

            result_text = "\n".join(lines)
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

- [ ] **Step 4: 添加模块级辅助函数 `_b64_to_pil`**

在 `build_server` 之后、`_parse_raw_result` 之前插入：

```python
def _b64_to_pil(b64: str) -> "Image.Image":
    """base64 PNG → PIL Image (RGB)."""
    import io as _io
    from PIL import Image as PILImage
    return PILImage.open(_io.BytesIO(base64.b64decode(b64))).convert("RGB")
```

确认 server.py 顶部已导入 `base64`；若未导入则追加 `import base64`。

- [ ] **Step 5: 验证 server 语法**

```bash
uv run python -c "from harness.server import build_server, _b64_to_pil; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add harness/server.py
git commit -m "feat: add match_reference tool (8-dimension vision comparison + 5 quantitative metrics)"
```

---

### Task 5: `build_atmosphere_mapping` tool — MiMo 分类 + 生成 mapping

**Files:**
- Modify: `harness/server.py`

**设计决策（经 grill 确认）：**
- MiMo 返回**维度分组 JSON**：`{"brightness": [{actor_type, property}, ...], "color_temp": [...], ...}`
- Handler 将 JSON 转写为 **Markdown 表格**（维度章节 → 组件 | 属性表）
- Tool response **内联完整映射**——LLM 当场就能用，不需要额外读文件
- 文件路径作为 fallback——上下文滚出后可重读

- [ ] **Step 1: 在 `list_tools` 注册 tool schema**

在 `match_reference` 注册之后追加：

```python
        result.append(Tool(
            name="build_atmosphere_mapping",
            description=(
                "扫描场景中 5 类氛围组件（DirectionalLight/SkyAtmosphere/"
                "ExponentialHeightFog/VolumetricCloud/PostProcessVolume），"
                "通过 MiMo 筛选氛围相关属性并按 8 维度（亮度/对比度/色温/色调偏移/"
                "饱和度/大气密度/阴影方向/天空）分类，生成维度→属性的映射表。"
                "每会话调用一次即可。"
            ),
            inputSchema={"type": "object", "properties": {}},
        ))
```

- [ ] **Step 2: 在 `call_tool` 添加 handler**

在 `match_reference` handler 之后追加：

```python
        if name == "build_atmosphere_mapping":
            t0 = time.monotonic()

            ATMOSPHERE_TYPES = [
                "DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
                "VolumetricCloud", "PostProcessVolume",
            ]

            # Step 1: 扫描 5 类组件
            scan_lines: list[str] = []
            actors_found: dict[str, list[str]] = {}
            all_properties: dict[str, dict[str, list[str]]] = {}
            # all_properties: {actor_type: {actor_name: [prop_name, ...]}}

            for actor_type in ATMOSPHERE_TYPES:
                try:
                    result_text = await ue_client.call_tool(
                        "SceneTools.find_actors",
                        {"glob": f"*{actor_type}*", "tag": ""},
                    )
                    parsed = _parse_raw_result(result_text)
                    actor_list = _extract_actor_names(parsed)
                    actors_found[actor_type] = actor_list
                    count = len(actor_list)
                    if count == 1:
                        scan_lines.append(f"  {actor_type}: 1 个 ({actor_list[0]})")
                    elif count > 1:
                        scan_lines.append(
                            f"  {actor_type}: {count} 个 "
                            f"({', '.join(actor_list[:3])}"
                            f"{'...' if count > 3 else ''}) ⚠ 多实例，需确认"
                        )
                    else:
                        scan_lines.append(
                            f"  {actor_type}: 未找到 → "
                            f"请调 add_to_scene_from_class 创建"
                        )
                except Exception as e:
                    scan_lines.append(f"  {actor_type}: 查询失败 ({e})")

            # Step 2: 对每个找到的 Actor 获取属性名列表
            for actor_type, actor_names in actors_found.items():
                if not actor_names:
                    continue
                all_properties[actor_type] = {}
                for actor_name in actor_names[:1]:  # 每类只取第一个
                    try:
                        props_result = await ue_client.call_tool(
                            "ObjectTools.list_properties",
                            {"actor_name": actor_name},
                        )
                        props_parsed = _parse_raw_result(props_result)
                        props_text = _extract_parsed_text(
                            props_parsed, props_result,
                        )
                        prop_names = _extract_property_names(props_text)
                        all_properties[actor_type][actor_name] = prop_names
                    except Exception as e:
                        logger.warning(
                            "获取 %s 属性列表失败: %s", actor_name, e,
                        )
                        all_properties[actor_type][actor_name] = []

            # Step 3: 组装 MiMo 分类 prompt
            prompt_parts = [
                "以下是从 UE 场景中提取的 5 类氛围组件及其所有属性名。",
                "请筛选与氛围视觉表现相关的属性（排除碰撞、Tick、调试等无关属性）。",
                "对每个属性标注其影响的高维维度：",
                "brightness / contrast / color_temp / color_cast / saturation "
                "/ haze / shadow_direction / sky。",
                "",
            ]
            for actor_type in ATMOSPHERE_TYPES:
                props = all_properties.get(actor_type, {})
                if not props:
                    prompt_parts.append(
                        f"### {actor_type}: (场景中未找到此组件)"
                    )
                    continue
                for actor_name, prop_names in props.items():
                    prompt_parts.append(f"### {actor_type} ({actor_name})")
                    if prop_names:
                        for p in prop_names:
                            prompt_parts.append(f"  - {p}")
                    else:
                        prompt_parts.append("  (获取属性失败)")
                    prompt_parts.append("")

            prompt_parts.append(
                "输出 JSON，格式如下（一个属性可标注多个维度）："
            )
            prompt_parts.append(json.dumps({
                "brightness": [
                    {"actor_type": "DirectionalLight", "property": "Intensity"},
                ],
                "color_temp": [
                    {"actor_type": "DirectionalLight", "property": "LightColor"},
                    {"actor_type": "PostProcessVolume", "property": "WhiteBalance"},
                ],
            }, indent=2, ensure_ascii=False))
            prompt_parts.append("")
            prompt_parts.append("只输出 JSON，不要有其他文字。")

            prompt = "\n".join(prompt_parts)

            # Step 4: MiMo 分类
            agent = VisionSubAgent(config)
            try:
                mapping = await agent.classify(prompt)
            except ValueError as e:
                duration_ms = (time.monotonic() - t0) * 1000
                err_text = f"MiMo 分类失败: {e}"
                # 降级：返回原始属性列表，LLM 自行筛选
                fallback_lines = [
                    "⚠ MiMo 分类失败，以下是 5 类组件的原始属性列表。",
                    "请 LLM 自行筛选氛围相关属性并调整。",
                    "",
                ]
                for actor_type in ATMOSPHERE_TYPES:
                    props = all_properties.get(actor_type, {})
                    if not props:
                        continue
                    for actor_name, prop_names in props.items():
                        fallback_lines.append(
                            f"## {actor_type} ({actor_name})"
                        )
                        for p in prop_names:
                            fallback_lines.append(f"  - {p}")
                        fallback_lines.append("")
                await _log_harness_call(
                    name, arguments,
                    f"MiMo 失败，返回原始属性列表 ({err_text})",
                    duration_ms, error=e,
                )
                return CallToolResult(content=[TextContent(
                    type="text", text="\n".join(fallback_lines),
                )])

            # Step 5: JSON → Markdown 表格
            md_content = _render_mapping_markdown(mapping)
            total_props = sum(
                len(props) for props in mapping.values()
            )

            # Step 6: 写入文件（fallback 路径）
            mapping_path = ""
            if snapshot_recorder is not None:
                try:
                    log_base = config.log_dir / snapshot_recorder._snapshot_dir.name
                    mapping_path = str(log_base / "atmosphere-mapping.md")
                    _Path(mapping_path).write_text(md_content, encoding="utf-8")
                    snapshot_recorder.set_mapping_path(mapping_path)
                except Exception as e:
                    logger.warning("写入 atmosphere-mapping.md 失败: %s", e)

            # Step 7: 组装返回——内联完整映射 + 扫描摘要
            duration_ms = (time.monotonic() - t0) * 1000
            result_text = (
                "氛围组件扫描完成：\n"
                + "\n".join(scan_lines)
                + f"\n\n映射已生成：{total_props} 个氛围相关属性"
                + (
                    f" → {mapping_path}"
                    if mapping_path else ""
                )
                + "\n\n---\n\n"
                + md_content
            )

            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

- [ ] **Step 3: 添加辅助函数**

**`_extract_property_names`** — 从 `list_properties` 返回值提取属性名列表。在 `_extract_actor_names` 旁边（模块级）追加：

```python
def _extract_property_names(parsed_text: str | None) -> list[str]:
    """从 list_properties 的返回文本中提取属性名列表."""
    if not parsed_text:
        return []
    names: list[str] = []
    for line in parsed_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 格式: "  property_name: type" 或 "  property_name (type)"
        # 取冒号或括号前的部分
        for delim in (":", " ("):
            if delim in line:
                name = line.split(delim)[0].strip()
                if name and not name.startswith("#") and not name.startswith("//"):
                    names.append(name)
                break
        else:
            # 无分隔符——可能是纯属性名
            if not line.startswith("#") and len(line) < 100:
                names.append(line)
    return names
```

**`_render_mapping_markdown`** — JSON → Markdown 表格。同样模块级：

```python
def _render_mapping_markdown(mapping: dict[str, Any]) -> str:
    """将维度分组映射 dict 转为 Markdown 表格。

    Args:
        mapping: MiMo classify() 返回的 dict，
            {"brightness": [{actor_type, property}, ...], ...}

    Returns:
        渲染后的 Markdown 文本
    """
    DIM_LABELS: dict[str, str] = {
        "brightness": "亮度 (Brightness)",
        "contrast": "对比度 (Contrast)",
        "color_temp": "色温 (Color Temperature)",
        "color_cast": "色调偏移 (Color Cast)",
        "saturation": "饱和度 (Saturation)",
        "haze": "大气密度 (Haze)",
        "shadow_direction": "阴影方向 (Shadow Direction)",
        "sky": "天空表现 (Sky)",
    }

    lines = ["# Atmosphere Mapping", ""]
    total = 0

    for dim_key, dim_label in DIM_LABELS.items():
        props = mapping.get(dim_key)
        if not props or not isinstance(props, list) or len(props) == 0:
            continue
        total += len(props)
        lines.append(f"## {dim_label}")
        lines.append("")
        lines.append("| 组件 | 属性 |")
        lines.append("|------|------|")
        for entry in props:
            if not isinstance(entry, dict):
                continue
            actor_type = entry.get("actor_type", "")
            prop = entry.get("property", "")
            if actor_type and prop:
                lines.append(f"| {actor_type} | {prop} |")
        lines.append("")

    lines.insert(1, f"共 {total} 个氛围相关属性")
    lines.insert(2, "")
    return "\n".join(lines)
```

- [ ] **Step 4: 确认 `_extract_actor_names` 已就位**

Task 4 中已添加。验证：

```bash
uv run python -c "from harness.server import _extract_actor_names, _extract_property_names, _render_mapping_markdown; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: 确认 server.py 顶部 imports 完备**

需确认以下 import 已存在，缺失则追加：
```python
import json
import base64
from typing import Any
```

- [ ] **Step 6: 验证 server 语法**

```bash
uv run python -c "from harness.server import build_server; print('OK')"
```
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add harness/server.py
git commit -m "feat: add build_atmosphere_mapping tool (MiMo classify → dimension-grouped markdown mapping)"
```

---

### Task 6: 测试 — TestReferenceImageTools

**Files:**
- Modify: `tests/test_verification_interceptor.py`

- [ ] **Step 1: 写 `compute_match_metrics` 集成冒烟测试**

```python
# ---- Reference Image Tools (Plan 0708) ----


class TestReferenceImageMetrics:
    """compute_match_metrics 的快速冒烟（详细测试在 test_metrics.py）."""

    def test_identical_images(self):
        from harness.verification.metrics import compute_match_metrics
        from PIL import Image
        ref = Image.new("RGB", (50, 40), (100, 150, 200))
        cur = Image.new("RGB", (50, 40), (100, 150, 200))
        result = compute_match_metrics(ref, cur)
        assert result["histogram_correlation"] == pytest.approx(1.0, abs=0.002)
        assert result["luminance"]["delta_pct"] == pytest.approx(0.0, abs=0.2)

    def test_different_images_detected(self):
        from harness.verification.metrics import compute_match_metrics
        from PIL import Image
        ref = Image.new("RGB", (50, 40), (255, 255, 255))
        cur = Image.new("RGB", (50, 40), (0, 0, 0))
        result = compute_match_metrics(ref, cur)
        assert result["luminance"]["delta_pct"] < -99
        assert result["histogram_correlation"] < 0.1


class TestVisionCompareWithReference:
    """VisionSubAgent.compare_with_reference() 双图对比."""

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        from harness.config import Config
        cfg = MagicMock(spec=Config)
        cfg.vision_api_key = "test-key"
        cfg.vision_api_base_url = "https://test.example.com"
        cfg.vision_model = "test-model"
        return cfg

    async def test_compare_returns_verdict(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = json.dumps({
                "answer": "亮度相似",
                "confidence": "high",
                "caveats": [],
                "observations": [
                    {"what": "亮度比较", "finding": "similar", "confidence": "high"}
                ],
            })
            verdict = await agent.compare_with_reference(
                _TINY_PNG_B64, _TINY_PNG_B64, "比较亮度",
            )

        assert verdict.answer == "亮度相似"
        assert verdict.confidence == "high"
        assert agent.history_length == 0  # 不记入 session history

    async def test_compare_api_error_returns_fallback(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.side_effect = RuntimeError("API 不可达")
            verdict = await agent.compare_with_reference(
                _TINY_PNG_B64, _TINY_PNG_B64, "test",
            )

        assert "失败" in verdict.answer
        assert verdict.confidence == "low"
        assert len(verdict.caveats) > 0


class TestVisionClassify:
    """VisionSubAgent.classify() 纯文本 MiMo 分类."""

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        from harness.config import Config
        cfg = MagicMock(spec=Config)
        cfg.vision_api_key = "test-key"
        cfg.vision_api_base_url = "https://test.example.com"
        cfg.vision_model = "test-model"
        return cfg

    async def test_classify_returns_dict(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        expected = {
            "brightness": [
                {"actor_type": "DirectionalLight", "property": "Intensity"},
            ],
            "color_temp": [
                {"actor_type": "DirectionalLight", "property": "LightColor"},
            ],
        }

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = (
                "以下是分类结果：\n"
                + json.dumps(expected, ensure_ascii=False)
                + "\n分类完成。"
            )
            result = await agent.classify("测试 prompt")

        assert result == expected
        assert agent.history_length == 0  # 不记入 session history

    async def test_classify_no_json_raises_value_error(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = "没有 JSON 的纯文本回复"
            with pytest.raises(ValueError, match="未找到 JSON"):
                await agent.classify("测试 prompt")

    async def test_classify_malformed_json_raises_value_error(self, mock_config):
        from harness.verification.vision_agent import VisionSubAgent
        agent = VisionSubAgent(mock_config)

        with patch(
            "harness.verification.vision_agent._call_vision_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = '{"broken": }'
            with pytest.raises(ValueError, match="JSON 解析失败"):
                await agent.classify("测试 prompt")


class TestRenderMappingMarkdown:
    """_render_mapping_markdown() 维度分组 JSON → Markdown 表格."""

    def test_basic_rendering(self):
        from harness.server import _render_mapping_markdown
        mapping = {
            "brightness": [
                {"actor_type": "DirectionalLight", "property": "Intensity"},
                {"actor_type": "SkyAtmosphere", "property": "SunIntensity"},
            ],
            "color_temp": [
                {"actor_type": "DirectionalLight", "property": "LightColor"},
            ],
        }
        md = _render_mapping_markdown(mapping)
        assert "## 亮度 (Brightness)" in md
        assert "| DirectionalLight | Intensity |" in md
        assert "| SkyAtmosphere | SunIntensity |" in md
        assert "## 色温 (Color Temperature)" in md
        assert "| DirectionalLight | LightColor |" in md
        assert "共 3 个氛围相关属性" in md

    def test_empty_dimension_skipped(self):
        from harness.server import _render_mapping_markdown
        mapping = {
            "brightness": [],
            "contrast": [],
        }
        md = _render_mapping_markdown(mapping)
        # 空维度不出现在输出中
        assert "亮度" not in md
        assert "对比度" not in md

    def test_missing_dimension_key_skipped(self):
        from harness.server import _render_mapping_markdown
        mapping = {
            "brightness": [
                {"actor_type": "DirectionalLight", "property": "Intensity"},
            ],
        }
        md = _render_mapping_markdown(mapping)
        # 缺失的维度章节不应出现
        assert "## 色温" not in md
        assert "共 1 个氛围相关属性" in md
```

- [ ] **Step 2: 运行新测试**

```bash
uv run pytest tests/test_verification_interceptor.py::TestReferenceImageMetrics \
  tests/test_verification_interceptor.py::TestVisionCompareWithReference \
  tests/test_verification_interceptor.py::TestVisionClassify \
  tests/test_verification_interceptor.py::TestRenderMappingMarkdown -v
```
Expected: all PASS (~8 tests)

- [ ] **Step 3: 运行全量测试确认无回归**

```bash
uv run pytest tests/ -v
```
Expected: 337+ passed（含 Plan 1 的 ~14 个 + Plan 2 的 ~8 个）

- [ ] **Step 4: Commit**

```bash
git add tests/test_verification_interceptor.py
git commit -m "test: add ReferenceImageTools tests (metrics smoke + vision compare + classify + markdown render)"
```

---

## 自检

| 检查项 | 状态 |
|--------|------|
| SPEC 覆盖 — 3 个 tool 全部有 handler | ✅ |
| SPEC 覆盖 — `compare_with_reference()` 方法 | ✅ Task 1 |
| SPEC 覆盖 — `classify()` 方法（MiMo 纯文本） | ✅ Task 2 |
| SPEC 覆盖 — MiMo 返回维度分组 JSON → Markdown 表格 | ✅ Task 5 |
| SPEC 覆盖 — tool response 内联完整映射 | ✅ Task 5 Step 7 |
| 无占位符 — 所有代码块完整 | ✅ |
| 类型一致性 — `classify() → dict` 在 Task 2/Task 5/Task 6 签名一致 | ✅ |
| 类型一致性 — `compare_with_reference()` 在 Task 1/Task 3/Task 4 签名一致 | ✅ |
| 不新建文件/模块 | ✅ 全部扩展现有文件 |
