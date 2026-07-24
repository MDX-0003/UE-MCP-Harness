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

from harness.client import McpClientSession, mcp_extract_text, mcp_parse_result
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
from harness.verification.reference import handle_match_reference, ReferenceImageSession
from harness.verification.atmosphere import handle_build_atmosphere_mapping

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
    # ---- 005 Skill Registry ----
    _skills_dir = skills_dir if skills_dir is not None else (Path.home() / ".ue-harness" / "skills")
    skill_registry = SkillRegistry(skills_dir=_skills_dir)
    skill_registry.load_skills()

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
        ref_session=ReferenceImageSession(),
        post_interceptors=interceptors,
    )

    # 查找 ToolCallLogger 并挂到 _ctx（Issue 021 将改为 is 类型检查）
    for ic in interceptors:
        if ic.__class__.__name__ == "ToolCallLogger":
            _ctx.tool_logger = ic
            break

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
            handler=handle_match_reference,
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
            handler=handle_build_atmosphere_mapping,
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

