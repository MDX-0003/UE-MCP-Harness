"""面向 LLM 的 MCP Server。

使用 `mcp` Python SDK，通过 SSE transport 暴露 Harness 的 MCP 能力。
LLM 连接到此 Server（而非直接连 UE），Harness 在中间做代理。

Context Assembly（004）：
  - tools/list 经过 allowlist 过滤，自由探索模式仅暴露约 20 个核心工具
  - list_toolsets / describe_toolset 作为逃生通道始终可见
  - 三层系统 prompt 通过 ContextProvider 管线组装

拦截器链（003+008）：
  ToolCallInterceptor 的 pre/post 钩子挂在此处。每个工具调用穿越拦截器链后到达 UE。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import time
from typing import Any

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

from harness.client import McpClientSession, mcp_extract_text, mcp_parse_result, mcp_unwrap_return_text, mcp_unwrap_return_value
from harness.config import Config
from harness.state.normalize import state_parse_actor_names
from harness.context.filter import apply_filter
from harness.context.prompt import (
    SystemContextProvider,
    TaskContextProvider,
    ToolReferenceProvider,
    assemble_system_prompt,
)
from harness.context.provider import ContextProvider
from harness.context.skill_registry import SkillRegistry
from harness.interceptor import ToolCallCompleted
from harness.state.models import WorldState
from harness.tools import ToolContext, HarnessTool, tool_ok, tool_fail, log_local_call, emit_local_event, require_vision_manager, VISION_BADGES
from harness.context.skill_tools import (
    handle_activate_skill, handle_save_skill, handle_get_context, handle_deactivate_skill,
    _parse_skill_yaml_to_dict, _list_skill_names,
)
from harness.verification.vision_tools import (
    handle_vision_screenshot, handle_vision_ask, handle_vision_tell,
    handle_vision_reset, handle_vision_status,
)

logger = logging.getLogger("harness.server")


def build_server(
    config: Config,
    ue_client: McpClientSession,
    interceptors: list[ToolCallInterceptor] | None = None,
    context_providers: list[ContextProvider] | None = None,
    world_state: WorldState | None = None,
    skill_ref: list[dict | None] | None = None,
    snapshot_recorder: Any | None = None,
    pending_screenshot_ref: list[Any] | None = None,
    vision_session_manager: Any | None = None,
    skills_dir: "Path | None" = None,
    stop_limit: "StopLimitInterceptor | None" = None,
) -> Server:
    """构建 MCP Server 实例。

    Args:
        config: Harness 配置。
        ue_client: 已连接的 UE MCP Client。
        vision_session_manager: 可选 VisionSessionManager（Issue 015）。
        interceptors: ToolCallInterceptor 列表。
        context_providers: ContextProvider 列表。
        world_state: 共享 WorldState 实例（008 填充，get_context 消费）。
        skill_ref: 可选的单元素列表，用于向外部暴露当前活跃 Skill。
                   activate_skill/deactivate_skill 时会同步更新 skill_ref[0]。
        snapshot_recorder: 可选 SnapshotRecorder，Skill 激活/停用和 get_context
                          时通知其写入快照文件。

    Returns:
        配置好的 mcp Server 实例。
    """
    server = Server("ue-agent-harness")

    if interceptors is None:
        interceptors = [DebugPreCallInterceptor()]

    # ---- 工具缓存（list_tools 调用间共享） ----
    _cached_raw_tools: list[dict] = []
    # ---- 参考图会话状态 ----
    _session_reference: dict[str, Any] = {}
    _is_first_load: bool = False
    _session_mapping_generated: bool = False
    # ---- Phase 3: 硬终止拦截器 ----
    _stop_limit = stop_limit
    # ---- 005 Skill Registry ----
    _skills_dir = skills_dir if skills_dir is not None else (Path.home() / ".ue-harness" / "skills")
    skill_registry = SkillRegistry(skills_dir=_skills_dir)
    skill_registry.load_skills()

    async def _ensure_best_snapshot_path(ue, session_ref: dict) -> str:
        """构造快照路径: {当前关卡目录}/{MMDD}-{关卡名}.umap。

        首次调用时查询 UE 获取当前关卡路径，后续复用缓存。
        Session 重置（换参考图）时缓存被清空，重新查询。
        """
        cached = session_ref.get("_snapshot_base_path")
        if cached:
            return cached

        try:
            result = await ue.call_tool(
                "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
                {"LevelPath": ""},
            )
            parsed = mcp_parse_result(result)
            text = mcp_extract_text(parsed, result) or ""
            rv = json.loads(text) if isinstance(text, str) else {}
            pkg_path = rv.get("packagePath", "") if isinstance(rv, dict) else ""
        except Exception:
            pkg_path = ""

        from datetime import datetime
        date_prefix = datetime.now().strftime("%m%d")

        if pkg_path:
            parts = pkg_path.rsplit("/", 1)
            if len(parts) == 2:
                snapshot_path = f"{parts[0]}/{date_prefix}-{parts[1]}"
            else:
                snapshot_path = f"/Game/{date_prefix}-Snapshot"
        else:
            snapshot_path = f"/Game/{date_prefix}-Snapshot"

        session_ref["_snapshot_base_path"] = snapshot_path
        return snapshot_path

    # ---- Context Providers ----

    if context_providers is None:
        context_providers = [
            SystemContextProvider(),
            TaskContextProvider(),  # 005: 当 _active_skill 非 None 时注入 Tier 2
        ]

    # ---- ToolContext (依赖注入容器, Issue 018) ----
    from harness.tools import ToolContext as _ToolContext
    _ctx = _ToolContext(
        config=config,
        ue_client=ue_client,
        world_state=world_state,
        skill_registry=skill_registry,
        skill_ref=skill_ref,
        context_providers=context_providers,
        snapshot_recorder=snapshot_recorder,
        pending_screenshot_ref=pending_screenshot_ref,
        vision_session_manager=vision_session_manager,
        stop_limit=_stop_limit,
        post_interceptors=interceptors,
    )

    # 查找 ToolCallLogger 并挂到 _ctx（Issue 021 将改为 is 类型检查）
    for ic in interceptors:
        if ic.__class__.__name__ == "ToolCallLogger":
            _ctx.tool_logger = ic
            break

    # ---- 参考图 handlers (Plan 0708, Issue 018) ----

    async def _handle_match_reference(ctx: ToolContext, arguments: dict) -> CallToolResult:
        nonlocal _session_reference, _session_mapping_generated, _is_first_load, _stop_limit
        from harness.verification.vision_agent import VisionSubAgent
        t0 = time.monotonic()
        ref_path_str = arguments.get("path", "")

        # Phase 3: 硬终止检查（倒计时归零 或 总轮次兜底）
        _match_count = _session_reference.get("_match_count", 0)
        _countdown = _session_reference.get("_countdown_remaining")
        _max_allowed = _session_reference.get("_max_allowed_rounds", 10)

        should_stop = False
        stop_reason = ""

        if _countdown is not None and _countdown < 0:
            should_stop = True
            stop_reason = (
                "倒计时已归零"
                "（直方图≥0.70 达成后已用尽 3 次调整机会）"
            )
        elif _countdown is None and _match_count >= _max_allowed:
            should_stop = True
            stop_reason = f"已达到最大轮次限制（{_max_allowed} 轮）"

        if should_stop and _stop_limit is not None:
            best_path = _session_reference.get("best_snapshot_path")
            summary = _stop_limit.build_summary(
                _session_reference, best_path, stop_reason,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            log_local_call(ctx, "match_reference", arguments, summary, t0)
            return CallToolResult(
                content=[TextContent(type="text", text=summary)],
                isError=True,
            )

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

        _is_first_load = _session_reference.get("_loaded") is None
        _prev_path = _session_reference.get("path", "")
        _is_new_reference = (str(ref_path) != _prev_path)

        if _is_new_reference and not _is_first_load:
            # 换了参考图 → 重置所有累积状态
            _session_reference = {"_loaded": True}
            _is_first_load = True

        _session_reference.update({"b64": ref_b64, "path": str(ref_path), "_loaded": True})

        # 递增 match_reference 调用计数（Phase 1）
        _match_count = _session_reference.get("_match_count", 0) + 1
        _session_reference["_match_count"] = _match_count

        # 4. 量化指标
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

        # 4b. 趋势对比（vs 上一次 match_reference 调用）
        prev_metrics = _session_reference.get("prev_metrics")
        trend_lines: list[str] = []
        if prev_metrics is not None and metrics_result is not None:
            trend_lines = _build_trend_summary(prev_metrics, metrics_result)
        # 保存本次结果供下次对比
        _session_reference["prev_metrics"] = metrics_result

        # 5. MiMo 9 维度双图对比
        question = (
            "请从以下 9 个维度比较当前截图与参考图的差异。"
            "每个维度只输出方向性判定，不需要描述绝对值：\n\n"
            "亮度 (Brightness):       darker / similar / brighter\n"
            "对比度 (Contrast):       lower / similar / higher\n"
            "色温 (Color Temperature): cooler / similar / warmer\n"
            "色调偏移 (Color Cast):    none / 偏X色\n"
            "饱和度 (Saturation):      less_saturated / similar / more_saturated\n"
            "大气密度 (Haze):          clearer / similar / hazier\n"
            "阴影方向 (Shadow Direction): 方向描述 + 是否一致\n"
            "天空表现 (Sky):           颜色/云量/渐变的差异方向\n"
            "视角方向 (Viewpoint Direction): looking_more_up / similar / looking_more_down\n\n"
            "注意：视角方向只比较相机俯仰角（向上看 vs 向下看），"
            "不考虑相机距离和目标对象。"
            "如果两张图拍摄的是完全不同的场景/对象，填 'different_scene'。\n\n"
            "每个判定配一句话佐证（你看到什么让你这样判断）。"
        )

        agent = VisionSubAgent(config)
        try:
            verdict = await agent.compare_with_reference(ref_b64, cur_b64, question)
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            err_text = f"match_reference Vision 调用失败: {e}"
            log_local_call(ctx, "match_reference", arguments, err_text, t0, error=e)
            return CallToolResult(content=[TextContent(
                type="text", text=err_text,
            )], isError=True)

        # 6. 组装返回文本（正文先行，header 在倒计时激活后 prepend）
        duration_ms = (time.monotonic() - t0) * 1000
        ref_w, ref_h = ref_img.size

        body_lines: list[str] = []
        if trend_lines:
            body_lines.extend(trend_lines)
        body_lines.append("")

        body_lines.append("MiMo 9 维度差异：")
        body_lines.append(verdict.answer)
        _badges = {"high": "🟢", "medium": "🟡", "low": "🔴"}
        body_lines.append(
            f"置信度: {_badges.get(verdict.confidence, '🟡')} "
            f"{verdict.confidence}"
        )
        if verdict.observations:
            body_lines.append("")
            body_lines.append("分项观察：")
            for obs in verdict.observations:
                what = obs.get("what", "")
                finding = obs.get("finding", "")
                conf = obs.get("confidence", "medium")
                ob = _badges.get(conf, "~")
                body_lines.append(f"  {ob} {what}: {finding}")

        if metrics_result:
            m = metrics_result

            hist = m["histogram_correlation"]
            rb_cur = m["color_temperature"]["cur_r_b_ratio"]

            # Phase 1: 倒计时激活（hist≥0.70 首次达成时）
            _countdown = _session_reference.get("_countdown_remaining")
            if _countdown is None and hist >= 0.70:
                _session_reference["_countdown_remaining"] = 3
                _session_reference["_countdown_start_round"] = _match_count
                _session_reference["_max_allowed_rounds"] = _match_count + 3

            # 倒计时递减
            if _session_reference.get("_countdown_remaining") is not None:
                _session_reference["_countdown_remaining"] -= 1

            body_lines.append("")
            body_lines.append("量化指标（全图统计，不受视点移动影响）：")
            body_lines.append(f"{'':>12} {'参考图':>8} {'当前':>8} {'差异':>10}")
            body_lines.append(
                f"{'亮度':>12} {m['luminance']['ref']:>8.1f} "
                f"{m['luminance']['cur']:>8.1f} {m['luminance']['delta_pct']:>+9.1f}%"
            )
            body_lines.append(
                f"{'对比度':>12} {m['contrast']['ref']:>8.1f} "
                f"{m['contrast']['cur']:>8.1f} {m['contrast']['delta_pct']:>+9.1f}%"
            )
            ct = m["color_temperature"]
            body_lines.append(
                f"{'色温':>12} {'R/B=' + str(ct['ref_r_b_ratio']):>8} "
                f"{'R/B=' + str(ct['cur_r_b_ratio']):>8}"
            )
            body_lines.append(
                f"{'饱和度':>12} {m['saturation']['ref']:>8.1f} "
                f"{m['saturation']['cur']:>8.1f} {m['saturation']['delta_pct']:>+9.1f}%"
            )
            body_lines.append(
                f"{'直方图相似度':>12} {'':>8} {'':>8} "
                f"{m['histogram_correlation']:>10.2f} (0→完全不同, 1→完全一致)"
            )

            # Phase 1c: 最佳点追踪 + 连续下降检测
            best = _session_reference.get("best_metrics")
            if best is None or hist > best.get("histogram_correlation", 0):
                _session_reference["best_metrics"] = {
                    "histogram_correlation": hist,
                    "round": _session_reference.get("_match_count", 0),
                    "rb_ratio": rb_cur,
                }
                body_lines.append("")
                body_lines.append(
                    f"🏆 新最佳记录：直方图相似度 {hist:.2f}"
                    f"（第 {_session_reference['best_metrics']['round']} 轮）"
                )

                # Phase 2: 最佳状态快照（仅首次跨过 0.70 时保存一次）
                if hist >= 0.70 and not _session_reference.get("_snapshot_saved"):
                    _session_reference["_snapshot_saved"] = True
                    snapshot_path = await _ensure_best_snapshot_path(
                        ue_client, _session_reference,
                    )
                    try:
                        await ue_client.call_tool(
                            "LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs",
                            {"TargetPath": snapshot_path},
                        )
                        _session_reference["best_snapshot_path"] = snapshot_path
                        body_lines.append("")
                        body_lines.append(
                            f"💾 快照已保存至 {snapshot_path}，编辑器仍在原关卡。"
                            f"仅当需要回退错误操作时才调 LoadLevel，请勿无故加载快照。"
                        )
                    except Exception as e:
                        logger.warning("SaveLevelAs 失败: %s", e)

            elif best is not None:
                best_hist = best.get("histogram_correlation", 0)
                best_round = best.get("round", "?")
                body_lines.append("")
                body_lines.append(
                    f"⚠ 当前直方图相似度 {hist:.2f} 低于最佳记录 "
                    f"{best_hist:.2f}（第 {best_round} 轮）。"
                    f"可能已越过最佳点，考虑回退到上一轮参数。"
                )

            # 连续下降检测
            prev_hist = _session_reference.get("_prev_hist")
            if prev_hist is not None and hist < prev_hist:
                _decline = _session_reference.get("_decline_count", 0) + 1
                _session_reference["_decline_count"] = _decline
                if _decline >= 2:
                    lines.append(
                        "🔴 直方图相似度连续 2 轮下降，"
                        "建议回退到上一轮参数并停止该方向调整。"
                    )
            else:
                _session_reference["_decline_count"] = 0
            _session_reference["_prev_hist"] = hist
        elif metrics_error:
            body_lines.append(f"\n⚠ 量化指标计算失败: {metrics_error}")
            body_lines.append("MiMo 分析仍然有效。")

        body_lines.append("")
        body_lines.append("---")
        body_lines.append("")
        if _is_first_load:
            body_lines.append(
                "在存在参考图的任务里，每轮迭代请使用 "
                f"match_reference(\"{ref_path_str}\") 获取对比反馈，"
                "不要用 vision_ask 做氛围对比。"
            )
            body_lines.append("")
        body_lines.append(
            "match_reference 每次返回量化指标（R/B、亮度、饱和度）——"
            "这是确定性像素计算，不受 VLM 主观判断影响，是最可靠的调整指南针。"
        )
        body_lines.append(
            "⚠ MiMo 分析与量化指标方向一致 → 高置信；"
            "不一致 → 以量化指标为准。"
        )

        # 组装最终输出：header（含倒计时状态）+ body
        _max_allowed = _session_reference.get("_max_allowed_rounds", 10)
        _countdown = _session_reference.get("_countdown_remaining")

        if _countdown is not None and _countdown > 0:
            header_lines = [
                f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
                f"第 {_match_count} 轮（最多 {_max_allowed} 轮，"
                f"⏳ 直方图已达 0.70+，剩余 {_countdown} 次调整机会）",
            ]
        elif _countdown is not None and _countdown == 0:
            header_lines = [
                f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
                f"第 {_match_count} 轮（最多 {_max_allowed} 轮，"
                f"⏳ 本轮为最后一次调整机会）",
            ]
        else:
            header_lines = [
                f"参考图：{ref_path.name} ({ref_w}×{ref_h})",
                f"第 {_match_count} 轮（最多 {_max_allowed} 轮）",
            ]

        lines = header_lines + body_lines

        result_text = "\n".join(lines)
        log_local_call(ctx, "match_reference", arguments, result_text, t0)
        return CallToolResult(content=[TextContent(type="text", text=result_text)])


    async def _handle_build_atmosphere_mapping(ctx: ToolContext, arguments: dict) -> CallToolResult:
        nonlocal _session_mapping_generated
        from harness.verification.vision_agent import VisionSubAgent
        t0 = time.monotonic()

        # 5 类氛围组件的 UE 类引用
        # 使用 actor_type (class refPath) 而非 glob:
        # UE find_actors 的 glob 匹配对长模式不可靠,
        # "*DirectionalLight*" 返回空但 "*Light*" 能找到,
        # class 引用是精确匹配, 无此问题.
        # 详见 docs/handoff/find_actors_glob_issue.md
        ATMOSPHERE_TYPES: dict[str, str] = {
            "DirectionalLight": "/Script/Engine.DirectionalLight",
            "SkyAtmosphere": "/Script/Engine.SkyAtmosphere",
            "ExponentialHeightFog": "/Script/Engine.ExponentialHeightFog",
            "VolumetricCloud": "/Script/Engine.VolumetricCloud",
            "PostProcessVolume": "/Script/Engine.PostProcessVolume",
        }

        # Step 1: 扫描 5 类组件
        scan_lines: list[str] = []
        actors_found: dict[str, list[str]] = {}

        for actor_type, class_path in ATMOSPHERE_TYPES.items():
            try:
                result_text = await ue_client.call_tool(
                    "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
                    {"tag": "", "actor_type": {"refPath": class_path}},
                )
                parsed = mcp_parse_result(result_text)
                actor_list = state_parse_actor_names(parsed)
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
                        f"  {actor_type}: 未找到"
                    )
            except Exception as e:
                scan_lines.append(f"  {actor_type}: 查询失败 ({e})")

        # 缺失组件汇总提示（一次性，不给 LLM 逐个下达操作指令的机会）
        missing_types = [
            at for at, names in actors_found.items() if not names
        ]
        if missing_types:
            scan_lines.append("")
            scan_lines.append(
                f"提示: {len(missing_types)} 类组件未找到"
                f"（{', '.join(missing_types)}）。"
                f"如需创建，使用 add_to_scene_from_class。"
            )

        # Step 2: 构建属性索引（含 component 子对象递归 + refPath 溯源）
        property_index: list[dict] = []
        next_idx = 1

        for actor_type, actor_names in actors_found.items():
            if not actor_names:
                continue
            for actor_name in actor_names[:1]:
                try:
                    # 2a. 获取 actor 顶层属性名
                    props_result = await ue_client.call_tool(
                        "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                        {"instance": {"refPath": actor_name}},
                    )
                    props_parsed = mcp_parse_result(props_result)
                    props_text = mcp_extract_text(
                        props_parsed, props_result,
                    )
                    # 解包可能的 returnValue 包装
                    props_text = mcp_unwrap_return_text(props_text or "")
                    actor_prop_names = _extract_property_names(props_text)

                    # 2b. 解析 component 引用，递归获取 component 级属性
                    direct_props, component_refs, comp_prop_names = \
                        await _resolve_component_properties(
                            ue_client, actor_name, actor_prop_names,
                        )

                    # 2c. 构建索引条目（component 指针字段被其子属性替换）
                    entries, next_idx = _build_property_index(
                        actor_type=actor_type,
                        actor_name=actor_name,
                        actor_prop_names=actor_prop_names,
                        component_refs=component_refs,
                        comp_prop_names=comp_prop_names,
                        start_index=next_idx,
                    )
                    property_index.extend(entries)
                except Exception as e:
                    logger.warning(
                        "获取 %s 属性列表失败: %s", actor_name, e,
                    )

        # Step 3: 组装 MiMo 分类 prompt（索引模式——MiMo 只输出整数）
        prompt = _build_mimo_prompt(property_index)

        # Step 4: MiMo 分类 + 索引解析
        agent = VisionSubAgent(config)
        try:
            mimo_output = await agent.classify(prompt)
            mapping = _resolve_mimo_indices(mimo_output, property_index)
        except ValueError as e:
            duration_ms = (time.monotonic() - t0) * 1000
            err_text = f"MiMo 分类失败: {e}"
            # 降级：返回原始属性索引列表（含 refPath 标注）
            fallback_lines = [
                "⚠ MiMo 分类失败，以下是原始属性索引列表。",
                "请 LLM 自行筛选氛围相关属性并调整。",
                "",
                "| 索引 | 组件 | 属性位置 (refPath) | 属性 |",
                "|------|------|-------------------|------|",
            ]
            for entry in property_index:
                short_ref = (
                    entry["refPath"].split(":")[-1]
                    if ":" in entry["refPath"]
                    else entry["refPath"]
                )
                fallback_lines.append(
                    f"| {entry['index']} | {entry['actor_type']} "
                    f"| `{short_ref}` | {entry['property']} |"
                )
            log_local_call(
                ctx, "build_atmosphere_mapping", arguments,
                f"MiMo 失败，返回原始属性索引列表 ({err_text})",
                t0, error=e,
            )
            return CallToolResult(content=[TextContent(
                type="text", text="\n".join(fallback_lines),
            )])

        # Step 5: JSON → Markdown 表格
        md_content = _render_mapping_markdown(mapping)
        total_props = sum(
            len(props) for props in mapping.values()
            if isinstance(props, list)
        )

        # Step 6: 写入文件（fallback 路径）
        mapping_path = ""
        if snapshot_recorder is not None:
            try:
                from pathlib import Path as _Path
                log_base = config.log_dir
                session_name = getattr(
                    snapshot_recorder, "_snapshot_dir", None,
                )
                if session_name is not None:
                    session_name = _Path(getattr(
                        session_name, "name", "",  # noqa — defensive
                    ))
                    # snapshot_recorder._snapshot_dir is a Path
                    pass
                mapping_path = str(log_base / "atmosphere-mapping.md")
                _Path(mapping_path).write_text(
                    md_content, encoding="utf-8",
                )
                snapshot_recorder.set_mapping_path(mapping_path)
            except Exception as e:
                logger.warning("写入 atmosphere-mapping.md 失败: %s", e)

        # Step 7: 组装返回——内联完整映射
        _session_mapping_generated = True
        duration_ms = (time.monotonic() - t0) * 1000
        result_text = (
            "氛围组件扫描完成：\n"
            + "\n".join(scan_lines)
            + f"\n\n映射已生成：{total_props} 个氛围相关属性"
            + (f" → {mapping_path}" if mapping_path else "")
            + "\n\n---\n\n"
            + md_content
        )

        log_local_call(ctx, "build_atmosphere_mapping", arguments, result_text, t0)
        return CallToolResult(content=[TextContent(type="text", text=result_text)])


    # ---- HarnessTool 注册表 (Issue 018) ----

    _harness_tools: list[HarnessTool] = [
        # Skill 工具
        HarnessTool(
            name="activate_skill",
            description="激活一个 Skill（按名称或描述片段匹配）。激活后可用工具限制为 Skill 白名单。",
            input_schema={
                "type": "object",
                "properties": {
                    "name_or_desc": {"type": "string", "description": "Skill 名称或描述片段"}
                },
                "required": ["name_or_desc"],
            },
            handler=handle_activate_skill,
        ),
        HarnessTool(
            name="save_skill",
            description="保存一个新的 Skill YAML 到 ~/.ue-harness/skills/。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 名称"},
                    "yaml_content": {"type": "string", "description": "完整的 Skill YAML 内容"},
                },
                "required": ["name", "yaml_content"],
            },
            handler=handle_save_skill,
        ),
        HarnessTool(
            name="get_context",
            description="获取 Harness 组装的完整系统上下文（UE 状态快照 + 活跃 Skill + 可用工具）。",
            input_schema={"type": "object", "properties": {}},
            handler=handle_get_context,
        ),
        HarnessTool(
            name="deactivate_skill",
            description="退出当前活跃的 Skill 模式，回到自由探索模式。",
            input_schema={"type": "object", "properties": {}},
            handler=handle_deactivate_skill,
        ),
        # Vision 工具
        HarnessTool(
            name="vision_screenshot",
            description=(
                "获取 UE 编辑器截图，追加到当前 Vision Session，可选附带针对性提问。"
                "如无活跃 Session 则自动创建。系统会自动注入场景上下文（dirty actors、"
                "最近操作记录）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["viewport", "editor", "asset"],
                        "description": "截图模式。viewport=仅视口画面；editor=合成编辑器窗口；asset=资产缩略图",
                        "default": "viewport",
                    },
                    "asset_path": {"type": "string", "description": "仅 mode=asset 时有效"},
                    "hide_ui": {"type": "boolean", "description": "隐藏编辑器 UI 覆盖层", "default": False},
                    "question": {
                        "type": "string",
                        "description": "可选。针对本次截图的首次提问。如 '所有立方体对齐了吗？'。Vision 会自动获得场景上下文。"
                    },
                },
            },
            handler=handle_vision_screenshot,
        ),
        HarnessTool(
            name="vision_ask",
            description=(
                "在当前 Vision Session 中追问（不截新图）。复用 Session 内所有截图和对话历史。"
                "系统自动附带最新场景上下文。必须先调 vision_screenshot 创建 Session。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "追问的具体问题。Vision 可引用之前的对话上下文。"
                    },
                },
                "required": ["question"],
            },
            handler=handle_vision_ask,
        ),
        HarnessTool(
            name="vision_tell",
            description=(
                "向当前 Vision Session 注入 LLM 的意图或预期（系统无法自动推断的信息）。"
                "不触发 API 调用。如需注入系统已知的数据（Actor 状态、修改记录），系统会自动完成。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "info": {
                        "type": "string",
                        "description": "任务级意图或预期。如：'目标是傍晚暖色光照，色温应为 4000K'。"
                    },
                },
                "required": ["info"],
            },
            handler=handle_vision_tell,
        ),
        HarnessTool(
            name="vision_reset",
            description="关闭当前 Vision Session（归档到日志），开启新 Session。在新任务开始或场景发生根本变化时调用。",
            input_schema={"type": "object", "properties": {}},
            handler=handle_vision_reset,
        ),
        HarnessTool(
            name="vision_status",
            description="查看当前 Vision Session 摘要：时长、截图数、提问数、上次结论。",
            input_schema={"type": "object", "properties": {}},
            handler=handle_vision_status,
        ),
        # 参考图工具
        HarnessTool(
            name="match_reference",
            description=(
                "加载参考图，与当前 UE 视口做 9 维度整体对比（亮度/对比度/色温/"
                "色调偏移/饱和度/大气密度/阴影方向/天空表现/视角方向）。返回结构化方向性差异"
                "+ 5 项量化指标。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "参考图文件路径（PNG/JPEG）",
                    },
                },
                "required": ["path"],
            },
            handler=_handle_match_reference,
        ),
        HarnessTool(
            name="build_atmosphere_mapping",
            description=(
                "扫描场景中 5 类氛围组件（DirectionalLight/SkyAtmosphere/"
                "ExponentialHeightFog/VolumetricCloud/PostProcessVolume），"
                "通过 MiMo 筛选氛围相关属性并按 8 维度分类，生成维度→属性映射。"
                "每会话调用一次即可。"
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_handle_build_atmosphere_mapping,
        ),
    ]

    async def _rebuild_tool_reference() -> list[dict]:
        """获取当前过滤后的工具列表（首次查 UE，后续复用缓存）。"""
        nonlocal _cached_raw_tools
        if not _cached_raw_tools:
            try:
                _cached_raw_tools = await ue_client.list_tools()
            except Exception as e:
                logger.error("获取工具列表失败: %s", e)
                return []

        # 应用过滤（自由探索模式或 Skill 模式）
        if skill_ref and skill_ref[0]:
            extra = frozenset(skill_ref[0].get("tools_allowlist", []))
            return apply_filter(_cached_raw_tools, config.default_tools_allowlist,
                                extra_allowed=extra, denylist=config.default_tools_denylist)
        else:
            return apply_filter(_cached_raw_tools, config.default_tools_allowlist,
                                denylist=config.default_tools_denylist)

    # ---- tools/list ----

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        filtered = await _rebuild_tool_reference()
        result: list[Tool] = []
        for t in filtered:
            result.append(Tool(
                name=t.get("name", ""),
                description=t.get("description", "") or f"UE 工具: {t.get('name', '')}",
                inputSchema=t.get("inputSchema", {"type": "object"}),
            ))

        # 从注册表收集 Harness 自有工具 spec
        for ht in _harness_tools:
            spec = ht.to_mcp_tool()
            result.append(Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            ))

        logger.info("LLM tools/list: 返回 %d 个工具（全量 %d 个，过滤后 + Harness 自有）",
                     len(result), len(_cached_raw_tools))
        return result

    # ---- tools/call ----

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        nonlocal _session_reference, _session_mapping_generated, _is_first_load, _stop_limit

        # 查注册表
        for tool in _harness_tools:
            if tool.name == name:
                return await tool.handler(_ctx, arguments)

        # UE 透传
        t_start = time.monotonic()
        error: Exception | None = None

        # === pre 阶段 ===
        for ic in interceptors:
            try:
                arguments = await ic.pre_call(name, arguments)
            except Exception as e:
                logger.error("预拦截 %s 失败: %s", type(ic).__name__, e)
                error = e
                break

        # === 实际调用 ===
        result_text: str | None = None
        if error is None:
            try:
                result_text = await ue_client.call_tool(name, arguments)
            except Exception as e:
                error = e

        duration_ms = (time.monotonic() - t_start) * 1000

        # === 解析（只做一次，各 interceptor 共享） ===
        parsed_raw = mcp_parse_result(result_text)
        parsed_text = mcp_extract_text(parsed_raw, result_text)

        # === post 阶段 ===
        event = ToolCallCompleted(
            name=name, args=arguments,
            raw_result=parsed_raw, parsed_text=parsed_text,
            error=error, duration_ms=duration_ms,
        )
        for ic in interceptors:
            try:
                await ic.post_call(event)
            except Exception as e:
                logger.error("后拦截 %s 失败: %s", type(ic).__name__, e)

        # 拦截器可能修改了 parsed_text（如 DriftAlertInterceptor 注入警告），
        # 将修改同步回 result_text 确保 LLM 看到
        if event.parsed_text is not None:
            result_text = event.parsed_text

        # Hard Boundary: load_level handler 标记了 _needs_refresh
        if world_state is not None and world_state._needs_refresh:
            from harness.state.hard_boundary import execute_hard_boundary
            try:
                hb_result = await execute_hard_boundary(
                    ue_client, world_state, reason="load_level",
                    expected_fingerprint=world_state.last_fingerprint,
                )
                world_state.last_fingerprint = hb_result.fingerprint
                world_state.drift_detected = hb_result.drift_detected
                logger.info("load_level 后 Hard Boundary 完成")
            except Exception as e:
                logger.warning("Hard Boundary 执行失败（非致命）: %s", e)

        if error:
            logger.error("工具调用失败: %s(%s) -> %s", name, arguments, error)
            return tool_fail(f"错误: {error}")

        logger.info("LLM tools/call: %s 完成 (%.0fms)", name, duration_ms)
        return tool_ok(result_text)

    # 挂一个调试用属性

    # 挂一个调试用属性
    server._harness_assemble_prompt = lambda state=None, skill=None: assemble_system_prompt(
        context_providers, state, skill or (skill_ref[0] if skill_ref else None)
    )

    return server


# ---- 参考图辅助函数 (Plan 0708) ----


def _b64_to_pil(b64: str) -> "Image.Image":
    """base64 PNG → PIL Image (RGB)."""
    import io as _io
    from PIL import Image as PILImage
    return PILImage.open(_io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _extract_property_names(parsed_text: str | None) -> list[str]:
    """从 list_properties 的返回文本中提取属性名列表.

    处理两种 UE 返回格式：
      1. 多行 name: type 格式（component 级 list_properties）:
         "intensity: float\\nlightColor: FLinearColor\\n..."
      2. 单行逗号分隔格式（actor 级 list_properties）:
         "[75 fields] directionalLightComponent, lightComponent, bHidden, ..."
    """
    if not parsed_text:
        return []
    names: list[str] = []
    for line in parsed_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 格式 1: "name: type" 或 "name (description)"
        for delim in (":", " ("):
            if delim in line:
                name = line.split(delim)[0].strip()
                if name and not name.startswith("#") \
                        and not name.startswith("//"):
                    names.append(name)
                break
        else:
            # 格式 2: 逗号分隔 — "[N fields] a, b, c, ..."
            if "," in line:
                # 去掉 "[N fields]" 前缀
                comma_part = line
                if line.startswith("[") and "fields]" in line:
                    bracket_end = line.index("fields]") + 7
                    comma_part = line[bracket_end:].strip()
                for part in comma_part.split(","):
                    name = part.strip()
                    if name and not name.startswith("#") \
                            and not name.startswith("{"):
                        names.append(name)
            elif not line.startswith("#") and not line.startswith("{") \
                    and len(line) < 100:
                names.append(line)
    return names


async def _resolve_component_properties(
    ue_client: "McpClientSession",
    actor_path: str,
    actor_prop_names: list[str],
) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """从 actor 属性中识别 component 引用字段，递归获取 component 级属性名.

    UE 的 Actor-Component 关系是两层结构：
      - Actor 顶层字段如 ``lightComponent`` 的值是 component refPath
      - 真正的氛围属性（intensity, lightColor 等）在 component 子对象上

    此函数识别以 "Component" 结尾的字段，调 get_properties 解析其 refPath，
    再对每个 component 调 list_properties 获取属性名。

    Returns:
        (direct_props, component_refs, comp_prop_names)
        - direct_props: actor 直接属性名列表（不含 component 指针字段）
        - component_refs: {field_name: component_refpath}
        - comp_prop_names: {field_name: [prop_name, ...]}
    """
    # 疑似 component 引用字段
    suspect_fields = [
        p for p in actor_prop_names
        if p.endswith("Component") or "Component" in p
    ]
    if not suspect_fields:
        return (actor_prop_names, {}, {})

    # 调 get_properties 解析这些字段的实际值（refPath）
    try:
        result_text = await ue_client.call_tool(
            "toolset_registry.toolsets.core.object.ObjectTools.get_properties",
            {"instance": {"refPath": actor_path}, "properties": suspect_fields},
        )
        parsed = mcp_parse_result(result_text)
        text = mcp_extract_text(parsed, result_text) or ""
        rv = mcp_unwrap_return_value(text)
    except Exception:
        return (actor_prop_names, {}, {})

    if rv is None:
        return (actor_prop_names, {}, {})

    # 分离 component refPath vs 普通属性
    component_refs: dict[str, str] = {}
    direct_props: list[str] = []

    for name in actor_prop_names:
        if name in suspect_fields:
            val = rv.get(name)
            if isinstance(val, dict) and val.get("refPath"):
                component_refs[name] = val["refPath"]
                continue
        direct_props.append(name)

    # 递归获取 component 属性名
    comp_prop_names: dict[str, list[str]] = {}
    for comp_field, comp_refpath in component_refs.items():
        try:
            comp_result = await ue_client.call_tool(
                "toolset_registry.toolsets.core.object.ObjectTools.list_properties",
                {"instance": {"refPath": comp_refpath}},
            )
            comp_parsed = mcp_parse_result(comp_result)
            comp_text = mcp_extract_text(comp_parsed, comp_result)
            comp_text = mcp_unwrap_return_text(comp_text or "")
            comp_names = _extract_property_names(comp_text)
            comp_prop_names[comp_field] = comp_names
        except Exception as e:
            logger.warning(
                "获取 component %s 属性失败: %s", comp_refpath, e,
            )
            comp_prop_names[comp_field] = []

    return (direct_props, component_refs, comp_prop_names)


def _build_property_index(
    actor_type: str,
    actor_name: str,
    actor_prop_names: list[str],
    component_refs: dict[str, str],
    comp_prop_names: dict[str, list[str]],
    start_index: int,
) -> tuple[list[dict], int]:
    """Build a flat property index with full provenance for MiMo classification.

    Each entry records:
      - index: sequential integer (1-based, for MiMo to reference)
      - actor_type: e.g. "DirectionalLight"
      - actor_name: actor refPath
      - refPath: where this property actually lives (actor or component refPath)
      - property: exact UE property name (preserved from list_properties)

    Actor-level props get refPath = actor_name.
    Component pointer fields are NOT emitted — their child properties replace them.
    Component-level props get refPath = component_refs[comp_field].

    Args:
        actor_type: Atmosphere component type name.
        actor_name: Actor refPath string.
        actor_prop_names: All property names from actor-level list_properties.
        component_refs: {field_name: component_refpath} mapping.
        comp_prop_names: {field_name: [prop_names]} from component list_properties.
        start_index: Starting index number (1-based).

    Returns:
        (index_entries, next_index) — list of entry dicts and the next free index.
    """
    entries: list[dict] = []
    idx = start_index

    for prop in actor_prop_names:
        if prop in component_refs:
            comp_path = component_refs[prop]
            for cprop in comp_prop_names.get(prop, []):
                entries.append({
                    "index": idx,
                    "actor_type": actor_type,
                    "actor_name": actor_name,
                    "refPath": comp_path,
                    "property": cprop,
                })
                idx += 1
        else:
            entries.append({
                "index": idx,
                "actor_type": actor_type,
                "actor_name": actor_name,
                "refPath": actor_name,
                "property": prop,
            })
            idx += 1

    return entries, idx


def _build_mimo_prompt(property_index: list[dict]) -> str:
    """Build the MiMo classification prompt using integer property indices.

    MiMo outputs ONLY integer indices, not property names.
    Harness resolves indices back to exact UE property names afterward.
    """
    from collections import defaultdict

    by_actor: dict[str, list[dict]] = defaultdict(list)
    for entry in property_index:
        by_actor[entry["actor_name"]].append(entry)

    prompt_parts = [
        "以下是从 UE 场景中提取的氛围组件属性，每个属性有一个索引编号 [N]。",
        "请筛选与氛围视觉表现相关的属性（排除碰撞、Tick、调试等无关属性）。",
        "对每个相关属性的**索引编号**标注其影响的维度：",
        "brightness / contrast / color_temp / color_cast / saturation "
        "/ haze / shadow_direction / sky。",
        "",
        "## 属性索引",
        "",
    ]

    for actor_name, entries in by_actor.items():
        actor_type = entries[0]["actor_type"]
        prompt_parts.append(f"### {actor_type} ({actor_name})")
        for e in entries:
            if e["refPath"] == e["actor_name"]:
                level_hint = ""
            else:
                comp_tail = e["refPath"].split(".")[-1] if "." in e["refPath"] else ""
                level_hint = f"  (component: {comp_tail})" if comp_tail else ""
            prompt_parts.append(f"  [{e['index']}] {e['property']}{level_hint}")
        prompt_parts.append("")

    prompt_parts.append(
        "输出格式：一个 JSON 对象，key 为维度名，value 为相关属性的**索引编号数组**。"
        "一个索引可出现在多个维度中。不相关的属性不出现在任何维度中。"
    )
    prompt_parts.append("示例：")
    prompt_parts.append(json.dumps({
        "brightness": [3],
        "color_temp": [3, 4],
        "haze": [7, 8],
    }, indent=2, ensure_ascii=False))
    prompt_parts.append("")
    prompt_parts.append("只输出 JSON，不要有其他文字。")

    return "\n".join(prompt_parts)


def _resolve_mimo_indices(
    mimo_output: dict[str, list],
    property_index: list[dict],
) -> dict[str, list[dict]]:
    """Resolve MiMo's integer indices back to full property entries.

    Args:
        mimo_output: {"brightness": [1, 3], "color_temp": [2], ...}
        property_index: List of {index, actor_type, actor_name, refPath, property}

    Returns:
        {"brightness": [{actor_type, actor_name, refPath, property}, ...], ...}
        Dimensions with no valid entries are omitted.
        Invalid indices (out of range, non-integer) are silently dropped.
    """
    lookup: dict[int, dict] = {}
    for entry in property_index:
        lookup[entry["index"]] = entry

    result: dict[str, list[dict]] = {}
    for dim, raw_indices in mimo_output.items():
        if not isinstance(raw_indices, list):
            continue
        resolved: list[dict] = []
        for raw in raw_indices:
            try:
                idx = int(raw)
            except (ValueError, TypeError):
                continue
            entry = lookup.get(idx)
            if entry is not None:
                resolved.append({
                    "actor_type": entry["actor_type"],
                    "actor_name": entry["actor_name"],
                    "refPath": entry["refPath"],
                    "property": entry["property"],
                })
        if resolved:
            result[dim] = resolved

    return result


def _build_trend_summary(
    prev: dict[str, Any], cur: dict[str, Any],
) -> list[str]:
    """生成指标趋势对比行（vs 上一次 match_reference 调用）。

    Args:
        prev: 上一次 match_reference 的 metrics 结果
        cur: 当前 match_reference 的 metrics 结果

    Returns:
        渲染后的趋势行列表（含空行前缀）
    """
    REF_RB = prev.get("color_temperature", {}).get("ref_r_b_ratio")

    def _arrow(prev_val: float, cur_val: float, target: float | None) -> str:
        """判断当前值是否向目标收敛。"""
        if target is None:
            return ""
        prev_dist = abs(prev_val - target)
        cur_dist = abs(cur_val - target)
        if cur_dist < prev_dist * 0.9:
            return " ✓ 向参考值收敛"
        if cur_dist > prev_dist * 1.1:
            return " ✗ 远离参考值"
        return " ≈ 无显著变化"

    lines = [
        "",
        "📊 指标趋势（vs 上一次 match_reference）：",
        f"{'':>14} {'上次':>8} {'本次':>8} {'变化':>10}  {'收敛方向':>12}",
    ]

    # R/B ratio
    prev_rb = prev.get("color_temperature", {}).get("cur_r_b_ratio")
    cur_rb = cur.get("color_temperature", {}).get("cur_r_b_ratio")
    if prev_rb is not None and cur_rb is not None:
        rb_delta = cur_rb - prev_rb
        arrow = _arrow(prev_rb, cur_rb, REF_RB)
        lines.append(
            f"{'R/B 比值':>14} {prev_rb:>8.4f} {cur_rb:>8.4f}"
            f" {rb_delta:>+10.4f} {arrow}"
        )

    # Luminance
    prev_lum = prev.get("luminance", {}).get("cur")
    cur_lum = cur.get("luminance", {}).get("cur")
    ref_lum = prev.get("luminance", {}).get("ref")
    if prev_lum is not None and cur_lum is not None:
        lum_delta = cur_lum - prev_lum
        arrow = _arrow(prev_lum, cur_lum, ref_lum)
        lines.append(
            f"{'亮度':>14} {prev_lum:>8.1f} {cur_lum:>8.1f}"
            f" {lum_delta:>+10.1f} {arrow}"
        )

    # Saturation
    prev_sat = prev.get("saturation", {}).get("cur")
    cur_sat = cur.get("saturation", {}).get("cur")
    ref_sat = prev.get("saturation", {}).get("ref")
    if prev_sat is not None and cur_sat is not None:
        sat_delta = cur_sat - prev_sat
        arrow = _arrow(prev_sat, cur_sat, ref_sat)
        lines.append(
            f"{'饱和度':>14} {prev_sat:>8.1f} {cur_sat:>8.1f}"
            f" {sat_delta:>+10.1f} {arrow}"
        )

    return lines


def _render_mapping_markdown(mapping: dict[str, Any]) -> str:
    """将维度分组映射 dict 转为 Markdown 表格.

    Args:
        mapping: {"brightness": [{actor_type, refPath, property}, ...], ...}

    Returns:
        渲染后的 Markdown 文本（含属性位置列用于标注 actor/component 层级）
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
        lines.append("| 组件 | 属性位置 (refPath) | 属性 |")
        lines.append("|------|-------------------|------|")
        for entry in props:
            if not isinstance(entry, dict):
                continue
            actor_type = entry.get("actor_type", "")
            ref_path = entry.get("refPath", "")
            prop = entry.get("property", "")
            if actor_type and prop:
                short_ref = ref_path.split(":")[-1] if ":" in ref_path else ref_path
                lines.append(f"| {actor_type} | `{short_ref}` | {prop} |")
        lines.append("")

    lines.insert(1, f"共 {total} 个氛围相关属性")
    lines.insert(2, "")
    return "\n".join(lines)
