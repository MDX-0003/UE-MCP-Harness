"""Harness CLI 入口点。

用法:
    harness start [--ue-port PORT] [--listen-port PORT]
    harness start --ue-port 8000 --listen-port 9000
    harness version
    harness skill create|list|delete|update <name>
    harness safety rules list|add|remove
    harness stats
    harness replay <log_file>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path


def _setup_logging(level: str = "INFO") -> None:
    """配置标准 logging。P3 时迁移到 structlog。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


EXPECTED_LEVEL_TOOLS = [
    "LevelPersistenceToolset.LevelPersistenceToolset.SaveCurrentLevel",
    "LevelPersistenceToolset.LevelPersistenceToolset.SaveAsset",
    "LevelPersistenceToolset.LevelPersistenceToolset.SaveAll",
    "LevelPersistenceToolset.LevelPersistenceToolset.ListDirtyPackages",
    "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
]

LEVEL_TOOLSET_FULL_NAME = "LevelPersistenceToolset.LevelPersistenceToolset"


async def _verify_level_persistence_tools(ue_client) -> list[str]:
    """验证 LevelPersistenceToolset 五工具可用。返回缺失的工具名列表。

    Harness 启动时调用：load_toolset → list_tools 比对 → ListDirtyPackages smoke call。
    缺失时只记录警告，不阻断启动——指纹功能降级运行。
    """
    logger = logging.getLogger("harness.cli")

    # 1. 加载 toolset（UE MCP 默认 deferred 模式）
    try:
        await ue_client.call_tool("load_toolset", {
            "toolset_name": LEVEL_TOOLSET_FULL_NAME,
        })
        logger.debug("load_toolset: %s", LEVEL_TOOLSET_FULL_NAME)
    except Exception as e:
        logger.warning("load_toolset 失败: %s", e)
        return [f"load_toolset failed: {e}"]

    # 2. 检查 tools/list
    try:
        tools = await ue_client.list_tools()
    except Exception as e:
        return [f"tools/list failed: {e}"]

    tool_names = {t["name"] for t in tools}  # list_tools 返回 list[dict]
    missing = [t for t in EXPECTED_LEVEL_TOOLS if t not in tool_names]

    # 3. Smoke test —— 调一个只读工具确认调用链路通畅
    if not missing:
        try:
            await ue_client.call_tool(
                "LevelPersistenceToolset.LevelPersistenceToolset.ListDirtyPackages", {}
            )
        except Exception as e:
            logger.debug("ListDirtyPackages smoke test failed: %s", e)
            missing.append(f"ListDirtyPackages smoke test failed: {e}")

    return missing


def cmd_start(args: argparse.Namespace) -> int:
    """启动 Harness MCP Server，连接 UE MCP Server。

    步骤:
      1. 加载配置（环境变量 + CLI 覆盖）
      2. 创建 McpClientSession，连接到 UE MCP Server
      3. 预加载所有工具集（optional，由配置控制）
      4. 初始化拦截器链（003 Logger + DebugPreCall）
      5. 构建 mcp Server，注册 list_tools / call_tool
      6. 通过 SSE transport 启动 HTTP Server
    """
    import uuid

    from harness.config import Config
    from harness.client import McpClientSession
    from harness.interceptor import DebugPreCallInterceptor, ToolCallInterceptor
    from harness.observability.logger import ToolCallLogger
    from harness.server import build_server
    from harness.context.skill_registry import SkillRegistry
    from harness.state.interceptor import StateCacheInterceptor
    from harness.state.models import WorldState
    from harness.state.hard_boundary import execute_hard_boundary
    from harness.transport import serve
    from harness.verification.config import load_vision_env
    from harness.verification.vision_agent import VisionSubAgent
    from harness.verification.interceptor import ReadbackInterceptor, VisionInterceptor
    from harness.stop_limit import StopLimitInterceptor   # 0714: match_reference 硬终止兜底
    from harness.verification.drift_alert import DriftAlertInterceptor
    from harness.observability.snapshotter import SnapshotRecorder
    from harness.verification.capturer import init_shot_session, close_shot_session

    # 项目 skills 目录（优先项目目录，回退到用户目录）
    _SKILLS_PATH = (Path(__file__).parent.parent / "skills").resolve()

    # 加载 Vision 配置（.vision.env）
    load_vision_env()

    config = Config.from_env().merge_cli_overrides(
        ue_port=args.ue_port,
        listen_port=args.listen_port,
        ue_host=args.ue_host,
        ue_project_root=(
            Path(args.ue_project_root) if args.ue_project_root else None
        ),
        ue_screenshot_dir=(
            Path(args.ue_screenshot_dir) if args.ue_screenshot_dir else None
        ),
    )
    from harness.verification.debug import init as debug_init
    debug_init(config)
    _setup_logging(config.log_level)
    logger = logging.getLogger("harness")

    logger.info("UE Agent Harness v0.1.0 正在启动...")
    logger.info("UE MCP Server: %s", config.ue_base_url)
    logger.info("Harness 监听: http://%s:%d/sse", config.listen_host, config.listen_port)

    # 创建 UE 客户端
    ue_client = McpClientSession(config)

    # 会话 ID：优先用 UE session_id（连通后），否则生成 UUID
    session_id = str(uuid.uuid4())[:8]

    # 008 State Cache — 全局共享的 WorldState 实例
    _cache = WorldState()

    # 007 验证闭环 — 活跃 Skill 引用（build_server 内 update，VisionInterceptor 读取）
    _active_skill_ref: list[dict | None] = [None]
    # vision_screenshot 工具写入，VisionInterceptor / SnapshotRecorder 读取
    _pending_screenshot_ref: list = [None]  # list[harness.verification.capturer.Screenshot | None]

    # 012 重连钩子 — 主 session 重连后自动恢复截图 session + State Cache
    async def _rebuild_shot_session() -> None:
        await init_shot_session(config)
        logger.info("截图 session 已随主 session 重建")

    async def _refresh_cache_on_reconnect() -> None:
        result = await execute_hard_boundary(
            ue_client, _cache, reason="reconnect",
            expected_fingerprint=_cache.last_fingerprint,
        )
        _cache.last_fingerprint = result.fingerprint
        _cache.drift_detected = result.drift_detected
        logger.info("State Cache 已随重连刷新")

    ue_client.add_reconnect_hook(_rebuild_shot_session)
    ue_client.add_reconnect_hook(_refresh_cache_on_reconnect)

    async def run() -> None:
        nonlocal session_id

        tool_logger = None
        snapshot_recorder = None

        # 008 缓存拦截器（无 session 依赖，可提前创建）
        cache_interceptor = StateCacheInterceptor(_cache)

        # 007 视觉验证拦截器（无 session 依赖）
        vision_agent = VisionSubAgent(config)

        # Issue 015: Vision Session Manager
        from harness.verification.session import VisionSessionManager
        _vision_session_mgr = VisionSessionManager(
            config, world_state=_cache,
            log_dir=config.log_dir,
        )

        vision_interceptor = VisionInterceptor(
            vision_agent, _cache,
            get_active_skill=lambda: _active_skill_ref[0],
            get_pending_screenshot=lambda: _pending_screenshot_ref[0],
            session_manager=_vision_session_mgr,
        )

        try:
            # 1. 连接 UE → 获取真实 session_id，失败则回退到短 UUID
            await ue_client.connect()
            logger.info("✓ 已连接到 UE MCP Server (session: %s)", ue_client.session_id or "无")
            session_id = ue_client.session_id or session_id

            # 2. 用真实 session_id 创建日志和快照记录器
            from harness.observability.snapshotter import _last_saved_screenshot_path as _ss_path_ref
            tool_logger = ToolCallLogger(
                config.log_dir, session_id,
                get_verdict=lambda: _cache.last_vision_verdict,
                get_screenshot_path=lambda: _ss_path_ref,
            )
            await tool_logger.start()

            # 使用 logger 的 session_dir 确保截图和 JSONL 在同一目录
            snapshot_dir = tool_logger.session_dir or config.log_dir / session_id
            # Issue 015: Vision Session 归档也写入同一目录
            if tool_logger.session_dir:
                _vision_session_mgr.set_log_dir(tool_logger.session_dir)
            snapshot_recorder = SnapshotRecorder(
                snapshot_dir, _cache,
                get_pending_screenshot=lambda: _pending_screenshot_ref[0],
            )

            _stop_limit = StopLimitInterceptor()              # 0714: match_reference 硬终止兜底（Phase 3）

            interceptors: list[ToolCallInterceptor] = [
                DebugPreCallInterceptor(),
                ReadbackInterceptor(ue_client, _cache),   # 016: L2 读回验证（injects badge before logger）
                _stop_limit,
                tool_logger,
                cache_interceptor,
                DriftAlertInterceptor(_cache),   # 漂移时注入警告到 tool call 返回值
                vision_interceptor,
                snapshot_recorder,
            ]

            # 2. 预加载工具集（可配置跳过，用于快速调试）
            if config.preload_all_toolsets:
                tool_count = await ue_client.preload_all_toolsets()
                logger.info("✓ 已预加载 %d 个工具", tool_count)
            else:
                tools = await ue_client.list_tools()
                logger.info("✓ 已获取 %d 个工具（跳过预加载）", len(tools))

            # 3. 创建截图专用持久 session（避免频繁 connect/close/DELETE）
            from harness.verification.capturer import init_shot_session
            await init_shot_session(config)
            logger.info("✓ 截图专用 session 已就绪")

            # 4. Hard Boundary: 首次连接 → L3 刷新 + 指纹基线 + dirty-diff
            hb_result = await execute_hard_boundary(
                ue_client, _cache, reason="startup",
            )
            _cache.last_fingerprint = hb_result.fingerprint
            _cache.drift_detected = hb_result.drift_detected
            if hb_result.refreshed:
                logger.info("✓ State Cache 已就绪")
            else:
                logger.warning("L3 刷新失败（非致命），State Cache 可能为空")

            # 5. 验证 LevelPersistenceToolset 工具可用性
            missing_tools = await _verify_level_persistence_tools(ue_client)
            if missing_tools:
                logger.warning(
                    "LevelPersistenceToolset 部分工具不可用: %s",
                    ", ".join(missing_tools),
                )
                logger.warning(
                    "指纹校验和 Hard Boundary 漂移检测将降级运行。"
                    "请确认 LevelPersistenceToolset 插件已编译并启用于 UE 项目。"
                )
            else:
                logger.info("✓ LevelPersistenceToolset 5 工具全部就绪")

            server = build_server(config, ue_client, interceptors,
                                  world_state=_cache, skill_ref=_active_skill_ref,
                                  snapshot_recorder=snapshot_recorder,
                                  pending_screenshot_ref=_pending_screenshot_ref,
                                  vision_session_manager=_vision_session_mgr,
                                  skills_dir=_SKILLS_PATH,
                                  stop_limit=_stop_limit)

            # 初始化 instructions：列出可用 Skill（含触发词，供 LLM 匹配用户意图）
            skill_registry = SkillRegistry(skills_dir=_SKILLS_PATH)
            skill_registry.load_skills()
            skills = skill_registry.list_skills()
            if skills:
                lines: list[str] = ["可用 Skill（用户提及触发词时自动激活对应 Skill）："]
                for s in skills:
                    lines.append(f"  - {s.name}: {s.description}")
                    triggers_str = ", ".join(s.triggers)
                    lines.append(f"    触发词: {triggers_str}")
                skill_list = "\n".join(lines)
            else:
                skill_list = "  (无已安装 Skill)"
            instructions = (
                "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n"
                "自由探索模式下可用约 20 个核心工具。\n"
                "\n"
                "## 场景修改后的标准验证流程\n"
                "修改任何 Actor 后，请按以下步骤验证效果：\n"
                "1. L2 读回 — 调 get_actor_transform / get_properties 确认写入值\n"
                "2. 相机定位 — 截图前确保视口对准了观察目标：\n"
                "   FocusOnActors 只能调距离，不改变旋转。如果 Actor 被遮挡或视角太偏，\n"
                "   先用以下预设角度轮换尝试：\n"
                "     pitch=-25, yaw=45  →  经典斜前侧（推荐首选）\n"
                "     pitch=-20, yaw=90  →  纯侧面\n"
                "     pitch=-55, yaw=0   →  俯瞰（适合检查位置关系）\n"
                "     pitch=-15, yaw=0   →  正面平视\n"
                "   每次：SetCameraTransform(rotation=(pitch, yaw)) → FocusOnActors()\n"
                "   试一个角度截一次图，确认目标可见后再做正式的 vision_screenshot 验证。\n"
                "3. 视觉验证 — 调 vision_screenshot(question=\"具体要验证什么？\")\n"
                "   系统会自动注入场景中的相关 Actor 信息和最近操作记录。\n"
                "4. 追问 — 如需要，调 vision_ask 在同一 Session 内深入分析\n"
                "5. 闭环 — 验证通过后调 vision_reset 关闭 Session\n"
                "\n"
                "## ⚠ 灯光修改专项 SOP（必须遵守）\n"
                "修改灯光属性（颜色/强度/旋转）前，必须先完成空间检查。\n"
                "灯光的视觉效果需要在被照亮的表面上判断，不能看编辑器图标/gizmo 颜色。\n"
                "\n"
                "前置检查步骤：\n"
                "a. 确认灯光附近有可见几何体：\n"
                "   调 get_actor_transform 获取灯光位置，\n"
                "   调 get_actor_transform 获取场景中 StaticMeshActor 的位置，\n"
                "   计算距离：PointLight/SpotLight 默认有效半径约 1000 UE 单位，\n"
                "   RectLight 约 2000 单位。如果所有几何体距离都超过有效半径，\n"
                "   先调 set_actor_transform 把灯移到目标物体附近。\n"
                "b. 确认灯光朝向目标：\n"
                "   SpotLight：调 rotation 使光锥对准目标物体。\n"
                "   可从相机对准目标物体来判断——不会被自身遮挡的视角 = 灯光应该来的方向。\n"
                "c. 确认后再改属性：\n"
                "   上面两步全部确认后，再调 set_properties 改 LightColor/Intensity。\n"
                "d. 验证时关注被照亮的表面，不是图标：\n"
                "   vision_screenshot 的问题要包含\"被照亮的表面呈现什么颜色\"，\n"
                "   不要问\"图标是否可见\"或\"图标是什么颜色\"。\n"
                "\n"
                "⚠ 如果 Vision 返回\"场景为空\"、\"无被照表面\"、\"无法观察光照效果\"：\n"
                "→ 不要继续调整灯光属性！回到前置检查步骤 a，先把灯移到几何体附近。\n"
                "\n"
                f"{skill_list}\n"
                "调 activate_skill <名称> 激活 Skill（如 activate_skill(\"验证\") 进入完整验证引导），"
                "调 deactivate_skill 退出。\n"
                "调 get_context 获取最新 UE 状态快照和活跃 Skill 进度。"
            )
            server.instructions = instructions
            # 将 instructions 写入 session 目录，供复盘分析
            if tool_logger is not None and tool_logger.session_dir is not None:
                inst_path = tool_logger.session_dir / "instructions.md"
                inst_path.write_text(instructions, encoding="utf-8")
                logger.debug("Instructions 已写入: %s", inst_path)

            # SDK 层校验错误日志路径（与 tool_calls.jsonl 同目录）
            _error_log = None
            if tool_logger is not None and tool_logger.session_dir is not None:
                _error_log = str(tool_logger.session_dir / "tool_errors.jsonl")

            await serve(
                server, host=config.listen_host, port=config.listen_port,
                error_log_path=_error_log,
            )

        except Exception as e:
            logger.error("启动失败: %s", e)
            raise
        finally:
            # 主 Session 关闭前自动归档未关闭的 Vision Session
            if _vision_session_mgr is not None:
                _vision_session_mgr.close_active()
            if snapshot_recorder is not None:
                snapshot_recorder.write_session_json()
            if tool_logger is not None:
                await tool_logger.stop()

    # 信号处理：SIGINT/SIGTERM 优雅关闭
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def shutdown(sig: signal.Signals) -> None:
        logger.info("收到信号 %s，正在关闭...", sig.name)
        from harness.verification.capturer import close_shot_session
        await close_shot_session()
        await ue_client.close()
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()
    
    # 当收到 SIGINT/SIGTERM 时，把 shutdown() 协程加入循环的待办队列
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(shutdown(s)),
            )
        except NotImplementedError:
            # Windows 不支持 add_signal_handler，使用 signal.signal
            pass

    try:# 阻塞在这里，直到 run() 完成或循环被 stop()
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        logger.info("用户中断。")
    except Exception as e:
        logger.error("致命错误: %s", e)
        return 1
    finally:
        try:
            from harness.verification.capturer import close_shot_session
            loop.run_until_complete(close_shot_session())
        except Exception:
            pass
        try:
            loop.run_until_complete(ue_client.close())
        except Exception:
            pass
        loop.close()
        logger.info("Harness 已停止。")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="UE Agent Harness — LLM 与 Unreal Engine 5.8 之间的 MCP 中间层",
    )
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="启动 Harness")
    p_start.add_argument(
        "--ue-port", type=int, default=None,
        help="UE MCP Server 端口 (默认: 8000)"
    )
    p_start.add_argument(
        "--listen-port", type=int, default=None,
        help="Harness 监听端口 (默认: 9000)"
    )
    p_start.add_argument(
        "--ue-host", type=str, default=None,
        help="UE MCP Server 地址 (默认: 127.0.0.1)"
    )
    p_start.add_argument(
        "--ue-project-root", type=str, default=None,
        help="UE 项目根目录（自动发现失败时的救援路径）"
    )
    p_start.add_argument(
        "--ue-screenshot-dir", type=str, default=None,
        help="UE 截图保存目录（硬覆盖，直接使用此路径）"
    )
    p_start.add_argument(
        "--no-preload", action="store_true",
        help="跳过工具集预加载（调试用）"
    )

    # version
    sub.add_parser("version", help="输出版本号")

    # skill
    p_skill = sub.add_parser("skill", help="Skill 管理")
    p_skill.add_argument("action", choices=["create", "list", "delete", "update"])
    p_skill.add_argument("name", nargs="?", help="Skill 名称")

    # safety
    p_safety = sub.add_parser("safety", help="安全护栏管理")
    p_safety.add_argument("action", choices=["rules"])
    p_safety.add_argument("sub_action", choices=["list", "add", "remove"], nargs="?")

    # stats
    sub.add_parser("stats", help="显示工具调用统计")

    # replay
    p_replay = sub.add_parser("replay", help="回放日志")
    p_replay.add_argument("log_file", help="日志文件路径")

    # vision
    p_vision = sub.add_parser("vision", help="视觉验证")
    p_vision_sub = p_vision.add_subparsers(dest="vision_action")
    p_vision_check = p_vision_sub.add_parser("check", help="单次视觉验证")
    p_vision_check.add_argument("--image", default=None, help="截图文件路径或 base64 数据")
    p_vision_check.add_argument("--from-ue", action="store_true", help="从 UE 编辑器实时截图（需 UE 运行）")
    p_vision_check.add_argument("--ue-port", type=int, default=8000, help="UE MCP Server 端口 (默认: 8000)")
    p_vision_check.add_argument("--expected", default="描述截图内容", help="预期场景描述（可选，留空则自由描述）")
    p_vision_check.add_argument("--tolerance", type=float, default=None, help="容忍度阈值 0-1（仅在提供 --expected 时有效）")

    args = parser.parse_args()

    if args.command == "start":
        if args.no_preload:
            import os
            os.environ["HARNESS_PRELOAD_TOOLSETS"] = "false"
        return cmd_start(args)
    elif args.command == "version":
        from harness import __version__
        print(f"ue-agent-harness v{__version__}")
        return 0
    elif args.command == "stats":
        return _cmd_stats(args)
    elif args.command == "replay":
        return _cmd_replay(args)
    elif args.command == "vision":
        return _cmd_vision(args)
    elif args.command == "skill":
        return _cmd_skill(args)
    elif args.command is None:
        parser.print_help()
        return 0
    else:
        print(f"命令 '{args.command}' 尚未实现。")
        return 1


def _cmd_stats(args: argparse.Namespace) -> int:
    """harness stats 命令 — 读取日志统计。"""
    from harness.config import Config
    from harness.observability.stats import cmd_stats

    config = Config.from_env()
    _setup_logging(config.log_level)

    return cmd_stats(config.log_dir)


def _cmd_replay(args: argparse.Namespace) -> int:
    """harness replay 命令 — 从日志回放工具调用。"""
    from pathlib import Path
    from harness.config import Config
    from harness.observability.replay import cmd_replay

    config = Config.from_env()
    _setup_logging(config.log_level)

    log_file = Path(args.log_file)
    return cmd_replay(log_file, ue_port=config.ue_port)


def _cmd_vision(args: argparse.Namespace) -> int:
    """harness vision check — 单次视觉验证。

    截图来源支持三种：
      --image <file>   本地文件（PNG）
      --from-ue        从 UE 编辑器实时截图（需 UE MCP Server 运行）
      缺省             将 --image 的值视为 base64 字符串
    """
    import asyncio
    import json
    from pathlib import Path

    from harness.verification.config import load_vision_env
    from harness.config import Config
    from harness.verification.capturer import capture, capture_from_file
    from harness.verification.vision_agent import VisionSubAgent

    # 校验参数
    if not args.from_ue and not args.image:
        print("错误: 请提供 --image <文件> 或 --from-ue 从 UE 截图")
        return 1

    # 先加载 .vision.env，再创建 Config
    load_vision_env()

    config = Config.from_env()
    _setup_logging(config.log_level)

    agent = VisionSubAgent(config)

    async def run() -> int:
        screenshot = None

        # ---- 路径 A: UE 实时截图 ----
        if args.from_ue:
            from harness.client import McpClientSession

            client = McpClientSession(Config(ue_port=args.ue_port))
            try:
                print(f"正在连接 UE MCP Server (: {args.ue_port})...")
                await client.connect()
                # 预加载工具集——延迟加载模式下，工具集未加载前截图工具不可用
                tool_count = await client.preload_all_toolsets()
                print(f"已加载 {tool_count} 个工具，正在截图...")
                screenshot = await capture(
                    client,
                    config.vision_max_size[0],
                    config.vision_max_size[1],
                )
                print(f"截图完成 ({screenshot.width}x{screenshot.height})")
            except Exception as e:
                print(f"UE 截图失败: {e}")
                return 1
            finally:
                await client.close()

        # ---- 路径 B: 本地文件 ----
        elif args.image and Path(args.image).is_file():
            screenshot = capture_from_file(
                Path(args.image),
                config.vision_max_size[0],
                config.vision_max_size[1],
            )

        # ---- 路径 C: base64 数据 ----
        elif args.image:
            from dataclasses import dataclass
            @dataclass
            class _Screenshot:
                data_b64: str
                width: int = 0
                height: int = 0
            screenshot = _Screenshot(data_b64=args.image)

        if screenshot is None:
            print("错误: 无法获取截图")
            return 1

        # 调用 Vision Sub-Agent
        verdict = await agent.check(
            screenshot.data_b64,
            args.expected,
            tolerance=args.tolerance or 0.7,
        )

        print(json.dumps({
            "pass": verdict.pass_,
            "reason": verdict.reason,
            "adjustment": verdict.adjustment,
            "need_more_info": verdict.need_more_info,
            "question": verdict.question,
        }, ensure_ascii=False, indent=2))
        return 0 if verdict.pass_ else 1

    return asyncio.run(run())


def _cmd_skill(args: argparse.Namespace) -> int:
    """harness skill create|list|delete|update — Skill 生命周期管理。"""
    from harness.context.skill_registry import SkillRegistry

    registry = SkillRegistry()
    _setup_logging("INFO")
    action = args.action
    name = args.name or ""

    if action == "list":
        skills = registry.list_skills()
        if not skills:
            print("(无已安装的 Skill)")
            return 0
        print(f"{'名称':<30} {'步骤':>5} {'触发器':<40} 描述")
        print("-" * 90)
        for s in skills:
            triggers = ", ".join(s.triggers[:3])
            if len(s.triggers) > 3:
                triggers += "..."
            print(f"{s.name:<30} {s.steps_count:>5} {triggers:<40} {s.description}")

    elif action == "create":
        if not name:
            print("用法: harness skill create <name>")
            return 1
        file_path = registry.create_template(name)
        print(f"Skill 模板已创建: {file_path}")
        print("请编辑此文件填写 triggers / steps / tools_allowlist / verification。")
        # 尝试打开编辑器
        import os
        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "notepad"))
        try:
            os.startfile(str(file_path))
        except Exception:
            print(f"请手动编辑: {file_path}")

    elif action == "delete":
        if not name:
            print("用法: harness skill delete <name>")
            return 1
        if registry.delete_skill(name):
            print(f"Skill '{name}' 已删除。")
        else:
            print(f"Skill '{name}' 不存在。")
            return 1

    elif action == "update":
        if not name:
            print("用法: harness skill update <name>")
            return 1
        info = registry.get_skill(name)
        if info is None or info.file_path is None:
            print(f"Skill '{name}' 不存在。")
            return 1
        import os
        try:
            os.startfile(str(info.file_path))
        except Exception:
            print(f"请手动编辑: {info.file_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
