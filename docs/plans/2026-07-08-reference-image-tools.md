# 参考图工具 + Vision Agent Comparison Implementation Plan

> **依赖:** Plan 1（`compute_match_metrics` from `harness/verification/metrics.py`）
> **被依赖:** Plan 3（Skills 引用这 3 个 tool 名）

**Goal:** 在 `VisionSubAgent` 上新增 `compare_with_reference()` 方法，在 `server.py` 注册 3 个新 Harness 工具（`build_atmosphere_mapping`、`match_reference`、`vision_compare`）。

**Architecture:** 最小扩展——不新建 handler 模块。`compare_with_reference()` 直接作为 `VisionSubAgent` 的新方法，复用 `_call_vision_api` + `_parse_verdict`。3 个 tool handler 挂在 `call_tool` 的现有 `if name == ...` 路由下，和已有的 `vision_*` 系列并存。

**Tech Stack:** Python 3.12+, httpx, PIL (Pillow), pytest + pytest-asyncio + pytest-mock

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `harness/verification/vision_agent.py` | **扩展** | `VisionSubAgent.compare_with_reference()` 方法 |
| `harness/server.py` | **扩展** | `build_server`: 注册 3 个 tool schema；`call_tool`: 加 3 个 handler |
| `tests/test_verification_interceptor.py` | **扩展** | 新增 `TestReferenceImageTools` 类 |

为什么不开新文件：
- `compare_with_reference()` 是 VisionSubAgent 的第三个"对话模式"（check / continue_with_question / compare_with_reference），天然属于同一 class
- 3 个 tool handler 遵循 `call_tool` 函数已有的 `if name == "vision_*"` 模式，加三个 if 分支即可
- 不需要新建 handler 模块、不需要新建 interceptor——这些工具是 LLM 主动调用的 Harness 工具，和 `vision_ask` 的架构地位完全一致

---

### Task 1: `compare_with_reference()` — 双图对比方法

**Files:**
- Modify: `harness/verification/vision_agent.py`

- [ ] **Step 1: 阅读现有 VisionSubAgent.check() 的调用链确认复用点**

`VisionSubAgent.check()`（`vision_agent.py:92`）的流程：
```
check(image_b64, question, scene_context) → 组装 messages → _call_vision_api → _parse_verdict → VisionVerdict
```

`compare_with_reference()` 的区别仅在于 messages 包含两张图（参考图 + 当前图），其他全部复用。

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
            question: 对比提问（8 维度固定提问 或 单组件三态判定）
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

### Task 2: `vision_compare` tool — server 注册 + handler

**Files:**
- Modify: `harness/server.py`

- [ ] **Step 1: 在 `list_tools` 注册 tool schema**

在 `build_server` → `list_tools` 中，`vision_status` 的 tool 注册代码之后（约在 `vision_agent.py` 中 `vision_reset` 注册后 5 行），追加：

```python
        result.append(Tool(
            name="vision_compare",
            description=(
                "双图对比验证——参考图 vs 当前截图。针对单个氛围组件做三态判定（✓ closer / ≈ similar / ✗ further）。"
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

在 `call_tool` 函数中，`vision_status` handler 之后（约在 `build_server` → `call_tool` 中 `if name == "vision_status":` 块之后），追加：

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
                    type="text", text="没有可复用的截图。请先调 vision_screenshot 获取当前视口截图。",
                )], isError=True)

            component = arguments.get("component", "")
            if component not in (
                "DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
                "VolumetricCloud", "PostProcessVolume",
            ):
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"无效的 component: '{component}'。"
                         f"可选: DirectionalLight, SkyAtmosphere, ExponentialHeightFog, "
                         f"VolumetricCloud, PostProcessVolume",
                )], isError=True)

            # 复用最新截图
            latest_ss = session.screenshots[-1]
            cur_b64 = latest_ss.b64
            ref_b64 = _get_reference_image_b64(arguments)
            if ref_b64 is None:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text="未找到参考图。请先调 match_reference(path) 加载参考图。",
                )], isError=True)

            # 组装单组件三态判定提问
            question = (
                f"仅关注 {component} 对画面氛围的影响，忽略其他组件的差异。\n"
                f"当前场景在 {component} 的表现，与参考图相比：\n"
                f"  ✓ closer — 更接近参考图了\n"
                f"  ≈ similar — 没有明显变化\n"
                f"  ✗ further — 更远离参考图了\n\n"
                f"选择 ✓/≈/✗，给一句佐证。"
            )

            agent = VisionSubAgent(vision_session_manager._config)
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

- [ ] **Step 3: 验证 server 能正常启动（语法检查）**

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

### Task 3: `match_reference` tool — server 注册 + handler

**Files:**
- Modify: `harness/server.py`（追加 handler + tool schema）
- Modify: `harness/server.py`（追加辅助函数 `_get_reference_image_b64`、`_session_reference` 全局状态）

**设计说明：** `match_reference` 需要存储参考图 b64 供后续 `vision_compare` 复用。不引入新模块——用一个 closure 变量 `_session_reference` 挂在 `build_server` 作用域内。

- [ ] **Step 1: 在 `build_server` 开头添加参考图状态**

在 `build_server` 函数中，`_cached_raw_tools` 声明之后插入：

```python
    # ---- 参考图会话状态（match_reference / vision_compare 共享） ----
    _session_reference: dict[str, Any] = {}  # {"b64": str, "path": str, "metrics": dict}
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

            # 加载参考图
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

            # 截当前视口
            try:
                from harness.verification.capturer import capture as capturer_capture
                max_w, max_h = config.vision_max_size
                screenshot = await capturer_capture(
                    ue_client, max_w, max_h, mode="viewport",
                )
                cur_b64 = screenshot.data_b64
                cur_img = _b64_to_pil(cur_b64)
            except Exception as e:
                return CallToolResult(content=[TextContent(
                    type="text", text=f"截图失败: {e}",
                )], isError=True)

            # 参考图 → base64
            import io as _io
            buf = _io.BytesIO()
            ref_img.save(buf, format="PNG")
            ref_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            # 保存参考图状态（供 vision_compare 复用）
            nonlocal _session_reference
            _session_reference = {"b64": ref_b64, "path": str(ref_path)}

            # 8 维度固定提问
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

            # MiMo 双图对比（异步发起）
            agent = VisionSubAgent(config)
            metrics_result = None
            try:
                from harness.verification.metrics import compute_match_metrics
                metrics_result = compute_match_metrics(ref_img, cur_img)
                _session_reference["metrics"] = metrics_result
            except Exception as e:
                logger.warning("量化指标计算失败（非致命）: %s", e)
                _session_reference["metrics_error"] = str(e)

            try:
                verdict = await agent.compare_with_reference(ref_b64, cur_b64, question)
            except Exception as e:
                duration_ms = (time.monotonic() - t0) * 1000
                err_text = f"match_reference Vision 调用失败: {e}"
                await _log_harness_call(name, arguments, err_text, duration_ms, error=e)
                return CallToolResult(content=[TextContent(
                    type="text", text=err_text,
                )], isError=True)

            # 组装返回文本
            duration_ms = (time.monotonic() - t0) * 1000
            ref_w, ref_h = ref_img.size
            ref_rb = round(metrics_result["color_temperature"]["ref_r_b_ratio"], 2) if metrics_result else "?"
            ref_lum = round(metrics_result["luminance"]["ref"], 1) if metrics_result else "?"

            lines = [
                f"参考图：{ref_path.name} ({ref_w}×{ref_h}, R/B={ref_rb}, 亮度={ref_lum})",
                "",
                "MiMo 8 维度差异：",
                verdict.answer,
            ]

            if metrics_result:
                lines.append("")
                lines.append("量化指标（全图统计，不受视点移动影响）：")
                lines.append(f"{'':>12} {'参考图':>8} {'当前':>8} {'差异':>10}")
                m = metrics_result
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
            elif "metrics_error" in _session_reference:
                lines.append(f"\n⚠ 量化指标计算失败: {_session_reference['metrics_error']}")
                lines.append("MiMo 分析仍然有效。")

            lines.append("")
            lines.append("氛围组件状态：")
            lines.append("  DirectionalLight, SkyAtmosphere, ExponentialHeightFog,")
            lines.append("  PostProcessVolume → 已存在（默认假设——调 build_atmosphere_mapping() 确认）")
            lines.append("  VolumetricCloud → 需确认")

            lines.append("")
            lines.append("下一步：如尚未生成参数映射，请调 build_atmosphere_mapping()。")
            lines.append("完成后对照映射和差异调整各组件。交叉参考 MiMo 分析和量化指标——")
            lines.append("两者一致则高置信，不一致则以 MiMo 为主、量化指标为参考修正。")

            result_text = "\n".join(lines)
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

- [ ] **Step 4: 添加辅助函数**

在 `call_tool` 之后、`_parse_raw_result` 之前插入两个辅助函数：

```python
def _b64_to_pil(b64: str) -> "Image.Image":
    """base64 PNG → PIL Image (RGB)."""
    import io as _io
    from PIL import Image as PILImage
    return PILImage.open(_io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _get_reference_image_b64(arguments: dict) -> str | None:
    """从 _session_reference 获取参考图 base64。返回 None 如未加载。"""
    nonlocal = None  # 占位——实际通过 build_server closure 访问
    # 实际实现：直接读取 _session_reference["b64"]
    return _session_reference.get("b64") if _session_reference else None
```

**注意：** 上面的 `_b64_to_pil` 放在 `build_server` 外作为模块级函数；`_get_reference_image_b64` 直接内联在 `vision_compare` handler 中引用 `_session_reference`。

- [ ] **Step 5: 修复 `_get_reference_image_b64` 为内联访问**

在 `vision_compare` handler 中，删除对 `_get_reference_image_b64()` 的调用，直接改为：

```python
            ref_b64 = _session_reference.get("b64") if _session_reference else None
```

同时在 `match_reference` handler 顶部已有的 `nonlocal _session_reference` 声明即可（Step 3 中 `_session_reference = {"b64": ref_b64, ...}` 前面已有）。

- [ ] **Step 6: 验证 server 语法**

```bash
uv run python -c "from harness.server import build_server; print('OK')"
```
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add harness/server.py
git commit -m "feat: add match_reference tool (8-dimension vision comparison + 5 quantitative metrics)"
```

---

### Task 4: `build_atmosphere_mapping` tool — server 注册 + handler

**Files:**
- Modify: `harness/server.py`

- [ ] **Step 1: 在 `list_tools` 注册 tool schema**

在 `match_reference` 注册之后追加：

```python
        result.append(Tool(
            name="build_atmosphere_mapping",
            description=(
                "扫描场景中 5 类氛围组件（DirectionalLight/SkyAtmosphere/ExponentialHeightFog/"
                "VolumetricCloud/PostProcessVolume），生成「视觉维度 → UE 属性」映射表。"
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

            # Step 1: 扫描场景中的 5 类组件
            scan_lines: list[str] = []
            actors_found: dict[str, list[str]] = {}

            for actor_type in ATMOSPHERE_TYPES:
                try:
                    result_text = await ue_client.call_tool(
                        "SceneTools.find_actors",
                        {"glob": f"*{actor_type}*", "tag": ""},
                    )
                    # 解析 actor 名称列表
                    parsed = _parse_raw_result(result_text)
                    actor_list = _extract_actor_names(parsed)
                    actors_found[actor_type] = actor_list
                    count = len(actor_list)
                    if count == 1:
                        scan_lines.append(f"  {actor_type}: 1 个 ({actor_list[0]})")
                    elif count > 1:
                        scan_lines.append(
                            f"  {actor_type}: {count} 个 ({', '.join(actor_list[:3])}"
                            f"{'...' if count > 3 else ''}) ⚠ 多实例，需确认"
                        )
                    else:
                        scan_lines.append(f"  {actor_type}: 未找到 → 请调 add_to_scene_from_class 创建")
                except Exception as e:
                    scan_lines.append(f"  {actor_type}: 查询失败 ({e})")

            # Step 2: 对找到的 Actor 获取属性列表
            mapping_lines: list[str] = [
                "# Atmosphere Mapping",
                f"# 生成时间: {datetime.now(timezone.utc).isoformat()}",
                "",
                "## 氛围组件属性映射",
                "",
            ]

            for actor_type, actor_names in actors_found.items():
                if not actor_names:
                    continue
                actor_name = actor_names[0]  # 取第一个
                try:
                    props_result = await ue_client.call_tool(
                        "ObjectTools.list_properties",
                        {"actor_name": actor_name},
                    )
                    props_parsed = _parse_raw_result(props_result)
                    props_text = _extract_parsed_text(props_parsed, props_result)
                    mapping_lines.append(f"### {actor_type} ({actor_name})")
                    mapping_lines.append(f"```")
                    mapping_lines.append(props_text or "(无属性)")
                    mapping_lines.append(f"```")
                    mapping_lines.append("")
                except Exception as e:
                    mapping_lines.append(f"### {actor_type} ({actor_name})")
                    mapping_lines.append(f"获取属性失败: {e}")
                    mapping_lines.append("")

            duration_ms = (time.monotonic() - t0) * 1000

            result_text = (
                "氛围组件扫描完成：\n"
                + "\n".join(scan_lines)
                + "\n\n---\n\n"
                + "\n".join(mapping_lines)
                + "\n\n下一步：LLM 自行从此映射中识别氛围相关属性，"
                + "对照 match_reference 返回的维度差异进行调整。"
            )

            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
```

- [ ] **Step 3: 添加 `_extract_actor_names` 辅助函数**

在 `_b64_to_pil` 旁边（模块级）追加：

```python
def _extract_actor_names(parsed: Any) -> list[str]:
    """从 find_actors 返回值中提取 actor 名称列表."""
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    if isinstance(parsed, dict):
        # 尝试常见 key
        for key in ("actors", "result", "data"):
            val = parsed.get(key)
            if isinstance(val, list):
                return [str(item) for item in val if item]
        # UE ToolsetRegistry returnValue 包装
        rv = parsed.get("returnValue")
        if isinstance(rv, list):
            return [str(item) for item in rv if item]
    # 尝试纯文本行
    text = str(parsed)
    lines = text.strip().split("\n")
    return [line.strip() for line in lines if line.strip() and not line.startswith("{")]
```

- [ ] **Step 4: 验证 server 语法**

```bash
uv run python -c "from harness.server import build_server, _extract_actor_names; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add harness/server.py
git commit -m "feat: add build_atmosphere_mapping tool (scan 5 atmosphere components + list properties)"
```

---

### Task 5: 测试 — TestReferenceImageTools

**Files:**
- Modify: `tests/test_verification_interceptor.py`

- [ ] **Step 1: 写 `compute_match_metrics` 集成测试**

在 `tests/test_verification_interceptor.py` 末尾追加：

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
        assert result["luminance"]["delta_pct"] < -99  # cur 比 ref 暗 >99%
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

        ref_b64 = _TINY_PNG_B64
        cur_b64 = _TINY_PNG_B64
        question = "比较两张图的亮度差异"

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
            verdict = await agent.compare_with_reference(ref_b64, cur_b64, question)

        assert verdict.answer == "亮度相似"
        assert verdict.confidence == "high"
        # 不应影响 session history
        assert agent.history_length == 0  # compare_with_reference 不记入历史

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
```

- [ ] **Step 2: 运行新测试**

```bash
uv run pytest tests/test_verification_interceptor.py::TestReferenceImageMetrics tests/test_verification_interceptor.py::TestVisionCompareWithReference -v
```
Expected: all PASS (~4 tests)

- [ ] **Step 3: 运行全量测试确认无回归**

```bash
uv run pytest tests/ -v
```
Expected: 337+ passed（含 Plan 1 的 14 个新测试）

- [ ] **Step 4: Commit**

```bash
git add tests/test_verification_interceptor.py
git commit -m "test: add ReferenceImageTools tests (metrics smoke + vision compare)"
```
