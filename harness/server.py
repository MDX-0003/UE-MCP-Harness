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

from harness.client import McpClientSession
from harness.config import Config
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
from harness.verification.vision_agent import (
    VISION_SYSTEM_PROMPT,
    VisionSubAgent,
    _call_vision_api,
    _extract_json_object,
)
from harness.verification.interceptor import _unwrap_return_value
from harness.state.models import WorldState

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
    # ---- 参考图会话状态（match_reference / vision_compare 共享） ----
    _session_reference: dict[str, Any] = {}
    _session_mapping_generated: bool = False
    # ---- 005 Skill Registry ----
    _skills_dir = skills_dir if skills_dir is not None else (Path.home() / ".ue-harness" / "skills")
    skill_registry = SkillRegistry(skills_dir=_skills_dir)
    skill_registry.load_skills()

    def _list_skill_names() -> str:
        skills = skill_registry.list_skills()
        if not skills:
            return "(无可用 Skill)"
        return ", ".join(f"{s.name}({s.description[:30]}...)" if len(s.description) > 30
                         else f"{s.name}({s.description})" for s in skills)

    def _parse_skill_yaml_to_dict(yaml_text: str) -> dict:
        """将原始 YAML 文本解析为与 evening-lighting.yaml 格式兼容的 dict。"""
        import yaml
        from harness.context.skill_registry import _normalize_list
        parsed = yaml.safe_load(yaml_text) or {}
        if not isinstance(parsed, dict):
            return {"name": "", "description": "", "triggers": [], "tools_allowlist": [], "steps": ""}
        return {
            "name": str(parsed.get("name", "")),
            "description": str(parsed.get("description", "")),
            "triggers": _normalize_list(parsed.get("triggers", [])),
            "tools_allowlist": _normalize_list(parsed.get("tools_allowlist", [])),
            "steps": str(parsed.get("steps", "")),
        }

    # ---- Context Providers ----

    if context_providers is None:
        context_providers = [
            SystemContextProvider(),
            TaskContextProvider(),  # 005: 当 _active_skill 非 None 时注入 Tier 2
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
        """返回 Context Assembly 过滤后的工具列表。

        004 自由探索模式：仅暴露 allowlist 匹配的工具 + 逃生通道
        005 Skill 模式：额外包含 Skill 的 tools_allowlist
        """
        filtered = await _rebuild_tool_reference()

        result: list[Tool] = []
        for t in filtered:
            result.append(
                Tool(
                    name=t.get("name", ""),
                    description=t.get("description", "") or f"UE 工具: {t.get('name', '')}",
                    inputSchema=t.get("inputSchema", {"type": "object"}),
                )
            )

        # 追加 Harness 自有工具
        result.append(Tool(
            name="activate_skill",
            description="激活一个 Skill（按名称或描述片段匹配）。激活后可用工具限制为 Skill 白名单。",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_desc": {"type": "string", "description": "Skill 名称或描述片段"}
                },
                "required": ["name_or_desc"],
            },
        ))
        result.append(Tool(
            name="save_skill",
            description="保存一个新的 Skill YAML 到 ~/.ue-harness/skills/。",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 名称"},
                    "yaml_content": {"type": "string", "description": "完整的 Skill YAML 内容"},
                },
                "required": ["name", "yaml_content"],
            },
        ))
        result.append(Tool(
            name="get_context",
            description="获取 Harness 组装的完整系统上下文（UE 状态快照 + 活跃 Skill + 可用工具）。",
            inputSchema={"type": "object", "properties": {}},
        ))
        result.append(Tool(
            name="deactivate_skill",
            description="退出当前活跃的 Skill 模式，回到自由探索模式。",
            inputSchema={"type": "object", "properties": {}},
        ))
        # Issue 015: Vision Session 工具
        result.append(Tool(
            name="vision_screenshot",
            description=(
                "获取 UE 编辑器截图，追加到当前 Vision Session，可选附带针对性提问。"
                "如无活跃 Session 则自动创建。系统会自动注入场景上下文（dirty actors、"
                "最近操作记录）。"
            ),
            inputSchema={
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
        ))
        result.append(Tool(
            name="vision_ask",
            description=(
                "在当前 Vision Session 中追问（不截新图）。复用 Session 内所有截图和对话历史。"
                "系统自动附带最新场景上下文。必须先调 vision_screenshot 创建 Session。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "追问的具体问题。Vision 可引用之前的对话上下文。"
                    },
                },
                "required": ["question"],
            },
        ))
        result.append(Tool(
            name="vision_tell",
            description=(
                "向当前 Vision Session 注入 LLM 的意图或预期（系统无法自动推断的信息）。"
                "不触发 API 调用。如需注入系统已知的数据（Actor 状态、修改记录），系统会自动完成。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "info": {
                        "type": "string",
                        "description": "任务级意图或预期。如：'目标是傍晚暖色光照，色温应为 4000K'。"
                    },
                },
                "required": ["info"],
            },
        ))
        result.append(Tool(
            name="vision_reset",
            description="关闭当前 Vision Session（归档到日志），开启新 Session。在新任务开始或场景发生根本变化时调用。",
            inputSchema={"type": "object", "properties": {}},
        ))
        result.append(Tool(
            name="vision_status",
            description="查看当前 Vision Session 摘要：时长、截图数、提问数、上次结论。",
            inputSchema={"type": "object", "properties": {}},
        ))
        # ---- 参考图工具 (Plan 0708) ----
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
                        "description": "复用 Session 内最新截图。",
                    },
                },
                "required": ["component"],
            },
        ))
        result.append(Tool(
            name="match_reference",
            description=(
                "加载参考图，与当前 UE 视口做 8 维度整体对比（亮度/对比度/色温/"
                "色调偏移/饱和度/大气密度/阴影方向/天空表现）。返回结构化方向性差异"
                "+ 5 项量化指标。"
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
        result.append(Tool(
            name="build_atmosphere_mapping",
            description=(
                "扫描场景中 5 类氛围组件（DirectionalLight/SkyAtmosphere/"
                "ExponentialHeightFog/VolumetricCloud/PostProcessVolume），"
                "通过 MiMo 筛选氛围相关属性并按 8 维度分类，生成维度→属性映射。"
                "每会话调用一次即可。"
            ),
            inputSchema={"type": "object", "properties": {}},
        ))

        logger.info("LLM tools/list: 返回 %d 个工具（全量 %d 个，过滤后 + Harness 自有）",
                     len(result), len(_cached_raw_tools))
        return result

    # ---- tools/call ----

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """路由工具调用：Harness 自有工具本地处理，UE 工具透传。

        Harness 自有工具:
          - activate_skill: 激活 Skill → 设置 skill_ref[0]
          - save_skill:     保存 Skill YAML → skill_registry.save_skill()
          - vision_* :      Issue 015 Vision Session 工具
        """

        nonlocal _session_reference, _session_mapping_generated

        # 辅助：为 Harness 自有工具记录日志到 JSONL
        # 未经 UE 透传路径的工具需手动触发 ToolCallLogger
        async def _log_harness_call(tool_name, tool_args, result_text, duration_ms, error=None):
            event = ToolCallCompleted(
                name=tool_name, args=tool_args,
                raw_result={"content": [{"type": "text", "text": result_text}]},
                parsed_text=result_text,
                error=error, duration_ms=duration_ms,
            )
            for ic in interceptors:
                if type(ic).__name__ == "ToolCallLogger":
                    try:
                        await ic.post_call(event)
                    except Exception as e:
                        logger.error("日志写入失败 %s: %s", tool_name, e)
                    break

        from harness.verification.vision_agent import VisionSubAgent

        # ---- Harness 自有工具 ----

        if name == "activate_skill":
            query = arguments.get("name_or_desc", "")

            # 空查询 → 重新扫描目录（感知外部 YAML 变更）
            if not query.strip():
                count = skill_registry.reload()
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"已重新扫描 Skill 目录，发现 {count} 个 Skill。"
                         f"可用: {_list_skill_names()}",
                )])

            matches = skill_registry.match_skill(query)

            if not matches:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"未找到匹配 '{query}' 的 Skill。可用 Skill: {_list_skill_names()}",
                )])

            if len(matches) == 1:
                skill = matches[0]
                yaml_text = skill_registry.load_skill_yaml(skill.name)
                if yaml_text:
                    if skill_ref is not None:
                        skill_ref[0] = _parse_skill_yaml_to_dict(yaml_text)
                    if snapshot_recorder is not None:
                        snapshot_recorder.on_skill_activated(skill.name, yaml_text)
                    logger.info("Skill 已激活: %s", skill.name)
                    return CallToolResult(content=[TextContent(
                        type="text",
                        text=f"Skill '{skill.name}' 已激活。{skill.description}\n"
                             f"步骤 ({skill.steps_count} 步)、"
                             f"工具白名单 ({len(skill.tools_allowlist)} 个): "
                             f"{', '.join(skill.tools_allowlist[:5])}"
                             f"{'...' if len(skill.tools_allowlist) > 5 else ''}",
                    )])

            # 多匹配：列出备选
            lines = [f"找到 {len(matches)} 个匹配 '{query}' 的 Skill，请选择一个："]
            for m in matches:
                lines.append(f"  - {m.name}: {m.description or '(无描述)'}")
            return CallToolResult(content=[TextContent(type="text", text="\n".join(lines))])

        if name == "save_skill":
            skill_name = arguments.get("name", "")
            yaml_content = arguments.get("yaml_content", "")

            if not skill_name or not yaml_content:
                return CallToolResult(
                    content=[TextContent(type="text", text="错误: name 和 yaml_content 均为必填")],
                    isError=True,
                )

            # 重复名检查
            existing = skill_registry.get_skill(skill_name)
            if existing and not arguments.get("overwrite", False):
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Skill '{skill_name}' 已存在。传 overwrite=true 覆盖，或先调 delete 再 save。",
                )])

            try:
                info = skill_registry.save_skill(skill_name, yaml_content)
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"Skill '{info.name}' 已保存。{info.description}\n"
                         f"步骤: {info.steps_count} 步, "
                         f"工具: {', '.join(info.tools_allowlist)}",
                )])
            except ValueError as e:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"保存失败: {e}")],
                    isError=True,
                )

        if name == "get_context":
            prompt = assemble_system_prompt(context_providers, world_state, skill_ref[0] if skill_ref else None)
            return CallToolResult(content=[TextContent(type="text", text=prompt)])

        if name == "deactivate_skill":
            was_active = skill_ref is not None and skill_ref[0] is not None
            if skill_ref is not None:
                skill_ref[0] = None
            if snapshot_recorder is not None:
                snapshot_recorder.on_skill_deactivated()
            if was_active:
                return CallToolResult(content=[TextContent(
                    type="text", text="已退出 Skill 模式，回到自由探索模式。",
                )])
            return CallToolResult(content=[TextContent(
                type="text", text="当前未激活任何 Skill，已在自由探索模式。",
            )])

        # ---- Issue 015: Vision Session 工具 ----

        if name == "vision_ask":
            t0 = time.monotonic()
            if vision_session_manager is None:
                return CallToolResult(content=[TextContent(
                    type="text", text="Vision Session Manager 未初始化。",
                )], isError=True)
            question = arguments.get("question", "")
            if not question.strip():
                return CallToolResult(content=[TextContent(
                    type="text", text="question 参数不能为空。",
                )], isError=True)
            try:
                verdict = await vision_session_manager.ask(question)
            except ValueError as e:
                duration_ms = (time.monotonic() - t0) * 1000
                err_text = str(e)
                await _log_harness_call(name, arguments, err_text, duration_ms, error=ValueError(err_text))
                return CallToolResult(content=[TextContent(
                    type="text", text=err_text,
                )], isError=True)
            duration_ms = (time.monotonic() - t0) * 1000
            warning = vision_session_manager.check_warning()
            result_text = verdict.reason
            if warning:
                result_text = warning + "\n\n" + result_text
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])

        if name == "vision_tell":
            t0 = time.monotonic()
            if vision_session_manager is None:
                return CallToolResult(content=[TextContent(
                    type="text", text="Vision Session Manager 未初始化。",
                )], isError=True)
            info = arguments.get("info", "")
            if not info.strip():
                return CallToolResult(content=[TextContent(
                    type="text", text="info 参数不能为空。",
                )], isError=True)
            vision_session_manager.tell(info)
            duration_ms = (time.monotonic() - t0) * 1000
            result_text = f"已注入上下文到 Vision Session（{len(info)} 字符）。"
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])

        if name == "vision_reset":
            t0 = time.monotonic()
            if vision_session_manager is None:
                return CallToolResult(content=[TextContent(
                    type="text", text="Vision Session Manager 未初始化。",
                )], isError=True)
            old_session = vision_session_manager.get_active()
            vision_session_manager.reset()
            duration_ms = (time.monotonic() - t0) * 1000
            if old_session:
                result_text = f"Vision Session {old_session.id} 已关闭并归档。新 Session 已创建。"
            else:
                result_text = "新 Vision Session 已创建。"
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])

        if name == "vision_status":
            t0 = time.monotonic()
            if vision_session_manager is None:
                return CallToolResult(content=[TextContent(
                    type="text", text="Vision Session Manager 未初始化。",
                )], isError=True)
            result_text = vision_session_manager.status_text()
            duration_ms = (time.monotonic() - t0) * 1000
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(
                type="text", text=result_text,
            )])

        # ---- 参考图工具 (Plan 0708) ----

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
                    text="没有可复用的截图。请先调 vision_screenshot 获取视口截图。",
                )], isError=True)

            component = arguments.get("component", "")
            _valid_components = (
                "DirectionalLight", "SkyAtmosphere", "ExponentialHeightFog",
                "VolumetricCloud", "PostProcessVolume",
            )
            if component not in _valid_components:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text=f"无效的 component: '{component}'。"
                         f"可选: {', '.join(_valid_components)}",
                )], isError=True)

            latest_ss = session.screenshots[-1]
            cur_b64 = latest_ss.b64
            ref_b64 = _session_reference.get("b64") if _session_reference else None
            if ref_b64 is None:
                return CallToolResult(content=[TextContent(
                    type="text",
                    text="未找到参考图。请先调 match_reference(path) 加载。",
                )], isError=True)

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

            _session_reference = {"b64": ref_b64, "path": str(ref_path)}
            # ---- 视角自动对齐 ----
            camera_aligned = False
            ref_view = _session_reference.get("ref_view")
            if ref_view is None:
                ref_view = await _analyze_viewpoint(config, ref_b64)
                if ref_view is not None:
                    _session_reference["ref_view"] = ref_view
                    logger.info(
                        "参考图视角: pitch=%.0f height_offset=%.0f",
                        ref_view["pitch"], ref_view["height_offset"],
                    )

            cur_view = await _analyze_viewpoint(config, cur_b64)
            if ref_view is not None and cur_view is not None:
                pitch_diff = abs(ref_view["pitch"] - cur_view["pitch"])
                logger.info(
                    "视角偏差: ref=%.0f cur=%.0f diff=%.0f",
                    ref_view["pitch"], cur_view["pitch"], pitch_diff,
                )
                if pitch_diff > 15:
                    try:
                        landscape_z = await _get_landscape_z(ue_client)
                    except Exception:
                        landscape_z = None
                    if landscape_z is None:
                        landscape_z = 0.0
                    new_z = landscape_z + ref_view["height_offset"]
                    # 保留当前 x,y,yaw
                    try:
                        current_xform = await ue_client.call_tool(
                            _CAMERA_ALIGN_TOOLS["get_camera"], {},
                        )
                        x_parsed = _parse_raw_result(current_xform)
                        if isinstance(x_parsed, dict) and "returnValue" in x_parsed:
                            x_rv = x_parsed["returnValue"]
                        else:
                            x_rv = x_parsed
                        cur_x = 0.0
                        cur_y = 0.0
                        cur_yaw = 0.0
                        if isinstance(x_rv, dict):
                            loc = x_rv.get("location", {})
                            rot = x_rv.get("rotation", {})
                            if isinstance(loc, dict):
                                cur_x = float(loc.get("x", 0))
                                cur_y = float(loc.get("y", 0))
                            if isinstance(rot, dict):
                                cur_yaw = float(rot.get("yaw", 0))
                    except Exception:
                        cur_x, cur_y, cur_yaw = 0.0, 0.0, 0.0

                    await ue_client.call_tool(
                        _CAMERA_ALIGN_TOOLS["set_camera"],
                        {
                            "transform": {
                                "location": {"x": cur_x, "y": cur_y, "z": new_z},
                                "rotation": {
                                    "pitch": ref_view["pitch"],
                                    "yaw": cur_yaw,
                                    "roll": 0,
                                },
                            }
                        },
                    )
                    # 重截视口
                    try:
                        from harness.verification.capturer import (
                            capture as _re_capture,
                        )
                        max_w2, max_h2 = config.vision_max_size
                        new_shot = await _re_capture(
                            ue_client, max_w2, max_h2, mode="viewport",
                        )
                        cur_b64 = new_shot.data_b64
                        camera_aligned = True
                    except Exception as e:
                        logger.warning("视角修正后重截失败: %s", e)
            # ---- 视角对齐结束 ----


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
            ]
            if trend_lines:
                lines.extend(trend_lines)
            if camera_aligned:
                align_note = (
                    f"📷 视角已自动修正: 原 pitch={cur_view["pitch"]:.0f}° → {ref_view["pitch"]:.0f}°,"
                    f" 高度 offset={ref_view["height_offset"]:.0f}"
                )
                lines.insert(0, align_note)
                lines.insert(1, "")
            lines.append("")

            lines.append("MiMo 8 维度差异：")
            lines.append(verdict.answer)

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

            lines.append("")
            if not _session_mapping_generated:
                lines.append(
                    "下一步：请先调 build_atmosphere_mapping() 生成参数映射，"
                    "再对照映射和差异调整各组件。"
                )
            else:
                lines.append(
                    "对照映射和差异调整各组件。交叉参考 MiMo 分析和量化指标——"
                )
                lines.append(
                    "两者一致则高置信，不一致则以 MiMo 为主、量化指标为参考修正。"
                )

            result_text = "\n".join(lines)
            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])

        if name == "build_atmosphere_mapping":
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
                        props_parsed = _parse_raw_result(props_result)
                        props_text = _extract_parsed_text(
                            props_parsed, props_result,
                        )
                        # 解包可能的 returnValue 包装
                        props_text = _unwrap_return_value_text(props_text or "")
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
                await _log_harness_call(
                    name, arguments,
                    f"MiMo 失败，返回原始属性索引列表 ({err_text})",
                    duration_ms, error=e,
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

            await _log_harness_call(name, arguments, result_text, duration_ms)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])

        if name == "vision_screenshot":
            """Harness 截图工具 — 通过 capturer.capture() 统一获取截图。

            Issue 015: 追加截图到当前 Vision Session + 可选针对性提问。
            """
            from harness.verification.capturer import capture as capturer_capture
            t0 = time.monotonic()
            try:
                mode = arguments.get("mode", "viewport")
                asset_path = arguments.get("asset_path", "")
                hide_ui = arguments.get("hide_ui", False)
                max_w, max_h = config.vision_max_size
                screenshot = await capturer_capture(
                    ue_client, max_w, max_h,
                    mode=mode, asset_path=asset_path, hide_ui=hide_ui,
                )
                if pending_screenshot_ref is not None:
                    pending_screenshot_ref[0] = screenshot
                result_text = (
                    f"Screenshot 已获取: {screenshot.width}x{screenshot.height}"
                    f" {screenshot.mime_type} (mode={mode})"
                )
            except Exception as e:
                if pending_screenshot_ref is not None:
                    pending_screenshot_ref[0] = None
                from harness.verification.debug import log_exception
                log_exception(e, name)
                return CallToolResult(
                    content=[TextContent(type="text",
                        text=f"截图失败: {type(e).__name__}: {e}")],
                    isError=True,
                )

            # 手动触发 post 拦截器链 — 不经过 UE 透传路径，
            # 必须在此处让 VisionInterceptor / SnapshotRecorder 消费截图结果
            duration_ms = (time.monotonic() - t0) * 1000
            event = ToolCallCompleted(
                name=name,  # 保留原始工具名供 interceptor 路由
                args=arguments,
                raw_result={"content": [{"type": "text", "text": result_text}]},
                parsed_text=result_text,
                error=None,
                duration_ms=duration_ms,
            )
            for ic in interceptors:
                try:
                    await ic.post_call(event)
                except Exception as e:
                    logger.error("后拦截 %s 失败: %s", type(ic).__name__, e)

            # 008 / 007 / 015: 将 Vision 分析结果 + Session 状态追加到返回值
            vision_info = ""
            if world_state is not None and world_state.last_vision_verdict:
                v = world_state.last_vision_verdict
                badges = {"high": "🟢", "medium": "🟡", "low": "🔴"}
                confidence = v.get("confidence", "medium")
                badge = badges.get(confidence, "🟡")
                answer = v.get("answer", "")
                caveats = v.get("caveats", [])
                observations = v.get("observations", [])

                parts = ["\n\n[Vision 分析]"]
                parts.append(f"置信度: {badge} {confidence}")
                parts.append(f"回答: {answer if answer else '（Vision 返回为空或格式异常）'}")
                if caveats:
                    parts.append(f"限制: {'; '.join(caveats[:3])}")
                if observations:
                    obs_lines = []
                    obs_badges = {"high": "✓", "medium": "~", "low": "?"}
                    for o in observations[:8]:
                        ob = obs_badges.get(o.get("confidence", ""), "~")
                        obs_lines.append(
                            f"  {ob} {o.get('what', '')}: {o.get('finding', '')}"
                        )
                    parts.append("分项观察:\n" + "\n".join(obs_lines))
                vision_info = "\n".join(parts)

            # Issue 015: 注入 Session 状态和过期警告
            if vision_session_manager is not None:
                session = vision_session_manager.get_active()
                if session is not None:
                    session_info = (
                        f"\n\nSession: {session.id} "
                        f"(截图 #{session.screenshot_count}，"
                        f"累计 {session.question_count} 次提问)"
                    )
                    result_text += session_info
                    warning = vision_session_manager.check_warning()
                    if warning:
                        vision_info = warning + "\n" + vision_info if vision_info else warning

            return CallToolResult(
                content=[TextContent(type="text", text=result_text + vision_info)]
            )

        # ---- UE 工具透传 ----
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
        parsed_raw = _parse_raw_result(result_text)
        parsed_text = _extract_parsed_text(parsed_raw, result_text)

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
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {error}")],
                isError=True,
            )

        logger.info("LLM tools/call: %s 完成 (%.0fms)", name, duration_ms)
        return CallToolResult(
            content=[TextContent(type="text", text=result_text)]
        )

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


def _item_to_name(item: Any) -> str:
    """从 find_actors 返回值元素中提取名称字符串."""
    if isinstance(item, dict):
        ref = item.get("refPath", "") or item.get("name", "") or item.get("Name", "")
        if ref:
            return str(ref)
    return str(item)


def _extract_actor_names(parsed: Any) -> list[str]:
    """从 find_actors 返回值中提取 actor 名称列表.

    处理两种格式：
      1. MCP content 包裹（ue_client.call_tool 真实返回）:
         {"content": [{"type": "text", "text": "{\\"returnValue\\": [...]}"}]}
         其中 text 内层是 JSON 字符串，需二次解析。
      2. 直接 dict（向后兼容旧测试 mock）:
         {"returnValue": [...]}
    """
    # ---- 解包 MCP content 数组 ----
    if isinstance(parsed, dict):
        content = parsed.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        try:
                            inner = json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(inner, dict) and "returnValue" in inner:
                            rv = inner["returnValue"]
                            if isinstance(rv, list):
                                return [_item_to_name(item) for item in rv if item]
                        if isinstance(inner, list):
                            return [_item_to_name(item) for item in inner if item]
    # ---- 向后兼容：直接 dict / list 格式 ----
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    if isinstance(parsed, dict):
        for key in ("actors", "result", "data"):
            val = parsed.get(key)
            if isinstance(val, list):
                return [_item_to_name(item) for item in val if item]
        rv = parsed.get("returnValue")
        if isinstance(rv, list):
            return [_item_to_name(item) for item in rv if item]
    # ---- 最终 fallback ----
    text = str(parsed)
    lines = text.strip().split("\n")
    return [line.strip() for line in lines if line.strip()
            and not line.startswith("{")]


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
        parsed = _parse_raw_result(result_text)
        text = _extract_parsed_text(parsed, result_text) or ""
        rv = _try_unwrap_return_value(text)
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
            comp_parsed = _parse_raw_result(comp_result)
            comp_text = _extract_parsed_text(comp_parsed, comp_result)
            comp_text = _unwrap_return_value_text(comp_text or "")
            comp_names = _extract_property_names(comp_text)
            comp_prop_names[comp_field] = comp_names
        except Exception as e:
            logger.warning(
                "获取 component %s 属性失败: %s", comp_refpath, e,
            )
            comp_prop_names[comp_field] = []

    return (direct_props, component_refs, comp_prop_names)


def _unwrap_return_value_text(text: str) -> str:
    """If text is a ``{"returnValue": "..."}`` JSON wrapper, extract the inner value.

    list_properties 返回值有两种格式：
      1. 直接文本: ``"[75 fields] a, b, c"``
      2. returnValue 包装: ``{"returnValue": "[75 fields] a, b, c"}``

    此函数统一解包，返回内层字符串。
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, dict) and "returnValue" in parsed:
        rv = parsed["returnValue"]
        if isinstance(rv, str):
            return rv
        return json.dumps(rv) if isinstance(rv, dict) else str(rv)
    return text


def _try_unwrap_return_value(text: str) -> dict | None:
    """尝试解包 returnValue JSON 包装.

    格式: {"returnValue": "<json_string>"}
    返回内层 JSON dict，或 None。
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and "returnValue" in parsed:
        rv = parsed["returnValue"]
        if isinstance(rv, str):
            try:
                inner = json.loads(rv)
                return inner if isinstance(inner, dict) else None
            except (json.JSONDecodeError, TypeError):
                return None
        if isinstance(rv, dict):
            return rv
    return None


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



_VIEWPOINT_PROMPT = (
    "评估这张截图的拍摄视角。"
    "UE 坐标系：pitch=0 为水平向前，pitch=-90 为垂直向下看地面。\n"
    "\n"
    "pitch 数值参考：\n"
    "  地平线在画面中间，相机几乎水平 → pitch 在 -5 到 0\n"
    "  能看到天空，地面占下半部分 → pitch 在 -15 到 -30\n"
    "  几乎看不到天空，全部是地面/物体 → pitch 在 -50 到 -70\n"
    "  不确定时取中间值，粒度 5°\n"
    "\n"
    "相机离地表高度（UE 单位，1 人身高≈170）：\n"
    "  贴近地面 → 50\n"
    "  人眼或略高 → 170\n"
    "  几层楼 → 800\n"
    "  更大高度 → 2000~5000，根据画面推断\n"
    "\n"
    "只输出 JSON，不要其他文字：\n"
    '{"pitch": <推测数字>, "height_offset": <推测数字>}'
)


async def _analyze_viewpoint(
    config: Config, image_b64: str,
) -> "dict[str, float] | None":
    """MiMo 单图视角分析，返回 {pitch, height_offset} 或 None.

    直接调 _call_vision_api（不经 VisionSubAgent.check），
    避免 _VISION_FORMAT_REMINDER 的标准 schema 与 _VIEWPOINT_PROMPT 的自定义
    schema 冲突。_VIEWPOINT_PROMPT 末尾已自带 JSON 格式定义。
    """
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": _VIEWPOINT_PROMPT},
        ],
    }]

    try:
        response = await _call_vision_api(
            config, messages,
            system=VISION_SYSTEM_PROMPT, temperature=0.7,
        )
    except Exception:
        return None

    json_str = _extract_json_object(response)
    if json_str is None:
        logger.warning("视角分析未找到 JSON: %.100s", response[:200])
        return None
    try:
        result = json.loads(json_str)
        pitch = float(result.get("pitch", 0))
        height_offset = float(result.get("height_offset", 170))
        return {"pitch": pitch, "height_offset": height_offset}
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("视角分析 JSON 解析失败: %s", e)
        return None


_CAMERA_ALIGN_TOOLS: dict[str, str] = {
    "find_actors": "toolset_registry.toolsets.core.scene.SceneTools.find_actors",
    "get_actor_bounds": "toolset_registry.toolsets.core.actor.ActorTools.get_actor_bounds",
    "get_camera": "ToolsetRegistry.EditorAppToolset.GetCameraTransform",
    "set_camera": "ToolsetRegistry.EditorAppToolset.SetCameraTransform",
}


async def _get_landscape_z(ue_client: "McpClientSession") -> "float | None":
    """通过 find_actors + get_actor_bounds 获取 Landscape 表面 Z 高度."""
    try:
        raw = await ue_client.call_tool(
            _CAMERA_ALIGN_TOOLS["find_actors"],
            {"glob": "*Landscape*", "tag": ""},
        )
        parsed = _parse_raw_result(raw)
        names = _extract_actor_names(parsed)
        if not names:
            return None
        first = names[0]
        if isinstance(first, dict):
            ref_path = first.get("refPath", "")
        else:
            ref_path = str(first)
        if not ref_path:
            return None
        raw2 = await ue_client.call_tool(
            _CAMERA_ALIGN_TOOLS["get_actor_bounds"],
            {"actor": {"refPath": ref_path}},
        )
        parsed2 = _parse_raw_result(raw2)
        # _unwrap_return_value 接受 string 而非 dict——直接提取 returnValue
        if isinstance(parsed2, dict) and "returnValue" in parsed2:
            rv = parsed2["returnValue"]
        else:
            rv = parsed2
        if isinstance(rv, dict):
            origin = rv.get("origin", {})
            extent = rv.get("boxExtent", {})
            if isinstance(origin, dict) and isinstance(extent, dict):
                z_center = float(origin.get("z", 0))
                z_half = float(extent.get("z", 0))
                return z_center + z_half
        return None
    except Exception:
        return None


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


def _parse_raw_result(result_text: str | None) -> Any:
    """解析 JSON-RPC result 文本为 Python 对象。"""
    if result_text is None:
        return None
    if isinstance(result_text, str):
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            return result_text
    return result_text


def _extract_parsed_text(parsed_raw: Any, fallback: str | None) -> str | None:
    """从 MCP content array 格式中提取纯文本。

    MCP 规范的工具结果格式：
      {"content": [{"type": "text", "text": "..."}]}
      {"content": [{"type": "image", "data": "base64...", "mimeType": "image/png"}]}

    对于 image 类型，保留 raw_result 但 parsed_text 返回标记字符串。
    """
    if parsed_raw is None:
        return fallback
    if isinstance(parsed_raw, dict):
        content = parsed_raw.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
            if isinstance(item, dict) and item.get("type") == "image":
                return f"[image: {item.get('mimeType', 'unknown')}]"
    # 回退到原始文本
    if fallback is not None:
        return fallback
    return str(parsed_raw)
