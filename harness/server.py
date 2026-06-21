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
from harness.interceptor import ToolCallCompleted, ToolCallInterceptor, DebugPreCallInterceptor
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
) -> Server:
    """构建 MCP Server 实例。

    Args:
        config: Harness 配置。
        ue_client: 已连接的 UE MCP Client。
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
    _active_skill: dict | None = None

    # ---- 005 Skill Registry ----
    skill_registry = SkillRegistry()
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
        if _active_skill:
            extra = frozenset(_active_skill.get("tools_allowlist", []))
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
        result.append(Tool(
            name="take_screenshot",
            description=(
                "Harness 截图工具（推荐）。通过 capturer 模块统一获取 UE 截图，"
                "自动 resize 到 1024x768、isError 检测、base64 修复。"
                "返回纯文本描述（不含 base64），视觉分析结果通过 get_context 查看。"
                "支持三种模式：viewport（默认，仅视口画面）、editor（合成编辑器窗口）、"
                "asset（单个资产缩略图）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["viewport", "editor", "asset"],
                        "description": (
                            "截图模式。viewport=仅视口画面（推荐，适合 Vision 分析场景光照/构图）；"
                            "editor=合成所有编辑器窗口（含面板/菜单/工具栏）；"
                            "asset=单个资产缩略图（需配合 asset_path）"
                        ),
                        "default": "viewport",
                    },
                    "asset_path": {
                        "type": "string",
                        "description": "仅 mode=asset 时有效，资产路径如 /Game/Meshes/SM_Cube",
                    },
                    "hide_ui": {
                        "type": "boolean",
                        "description": "隐藏编辑器 UI 覆盖层（gizmo、选中框线），仅 viewport/asset 有效",
                        "default": False,
                    },
                },
            },
        ))

        logger.info("LLM tools/list: 返回 %d 个工具（全量 %d 个，过滤后 + Harness 自有）",
                     len(result), len(_cached_raw_tools))
        return result

    # ---- tools/call ----

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """路由工具调用：Harness 自有工具本地处理，UE 工具透传。

        Harness 自有工具:
          - activate_skill: 激活 Skill → 设置 _active_skill
          - save_skill:     保存 Skill YAML → skill_registry.save_skill()
        """

        nonlocal _active_skill

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
                    _active_skill = _parse_skill_yaml_to_dict(yaml_text)
                    if skill_ref is not None:
                        skill_ref[0] = _active_skill
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
            prompt = assemble_system_prompt(context_providers, world_state, _active_skill)
            return CallToolResult(content=[TextContent(type="text", text=prompt)])

        if name == "deactivate_skill":
            was_active = _active_skill is not None
            _active_skill = None
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

        if name == "take_screenshot":
            """Harness 截图工具 — 通过 capturer.capture() 统一获取截图。"""
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
                log_exception(e, "take_screenshot")
                return CallToolResult(
                    content=[TextContent(type="text",
                        text=f"截图失败: {type(e).__name__}: {e}")],
                    isError=True,
                )

            # 手动触发 post 拦截器链 — take_screenshot 不经过 UE 透传路径，
            # 必须在此处让 VisionInterceptor / SnapshotRecorder 消费截图结果
            duration_ms = (time.monotonic() - t0) * 1000
            event = ToolCallCompleted(
                name="take_screenshot",
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

            return CallToolResult(content=[TextContent(type="text", text=result_text)])

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

        # L3 刷新：load_level handler 标记了 _needs_refresh
        if world_state is not None and world_state._needs_refresh:
            from harness.state.refresher import full_refresh
            try:
                await full_refresh(ue_client, world_state)
                logger.info("load_level 后 L3 刷新完成")
            except Exception as e:
                logger.warning("L3 刷新失败（非致命）: %s", e)

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
        context_providers, state, skill or _active_skill
    )

    return server


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
