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
    vision_session_manager: Any | None = None,
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
                status = "✅ PASS" if v.get("pass") else "❌ FAIL"
                reason = v.get("reason", "")
                vision_info = f"\n\n[Vision 分析] {status}\n{reason}"
                if v.get("adjustment"):
                    vision_info += f"\n调整建议: {v['adjustment']}"

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
