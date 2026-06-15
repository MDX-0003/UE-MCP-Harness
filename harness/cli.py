"""Harness CLI 入口点。

用法:
    harness start [--ue-port PORT] [--listen-port PORT]
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


def _setup_logging(level: str = "INFO") -> None:
    """配置标准 logging。P3 时迁移到 structlog。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


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
    from harness.state.refresher import full_refresh
    from harness.transport import serve

    config = Config.from_env().merge_cli_overrides(
        ue_port=args.ue_port,
        listen_port=args.listen_port,
    )
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

    async def run() -> None:
        nonlocal session_id

        # 003 日志拦截器（在连接前创建，连接后启动以获得真正的 session_id）
        tool_logger = ToolCallLogger(config.log_dir, session_id)
        await tool_logger.start()

        # 008 缓存拦截器
        cache_interceptor = StateCacheInterceptor(_cache)

        interceptors: list[ToolCallInterceptor] = [
            DebugPreCallInterceptor(),
            tool_logger,
            cache_interceptor,
        ]

        try:
            # 1. 连接 UE
            await ue_client.connect()
            logger.info("✓ 已连接到 UE MCP Server (session: %s)", ue_client.session_id or "无")
            if ue_client.session_id:
                tool_logger._session_id = ue_client.session_id

            # 2. 预加载工具集（可配置跳过，用于快速调试）
            if config.preload_all_toolsets:
                tool_count = await ue_client.preload_all_toolsets()
                logger.info("✓ 已预加载 %d 个工具", tool_count)
            else:
                tools = await ue_client.list_tools()
                logger.info("✓ 已获取 %d 个工具（跳过预加载）", len(tools))

            # 3. L3 全量刷新 State Cache（首次连接 → Hard Boundary）
            try:
                await full_refresh(ue_client, _cache)
                logger.info("✓ State Cache 已就绪")
            except Exception as e:
                logger.warning("L3 刷新失败（非致命）: %s", e)

            # 4. 构建并启动 MCP Server
            server = build_server(config, ue_client, interceptors, world_state=_cache)

            # 初始化 instructions：列出可用 Skill + 提示核心工具
            skill_registry = SkillRegistry()
            skill_registry.load_skills()
            skills = skill_registry.list_skills()
            skill_list = "\n".join(
                f"  - {s.name}: {s.description}" for s in skills
            ) if skills else "  (无已安装 Skill)"
            instructions = (
                "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n"
                "自由探索模式下可用约 20 个核心工具。\n"
                f"可用 Skill:\n{skill_list}\n"
                "调 activate_skill <名称> 激活 Skill，调 deactivate_skill 退出。\n"
                "调 get_context 获取最新 UE 状态快照和活跃 Skill 进度。"
            )

            await serve(server, host=config.listen_host, port=config.listen_port, instructions=instructions)

        except Exception as e:
            logger.error("启动失败: %s", e)
            raise
        finally:
            await tool_logger.stop()

    # 信号处理：SIGINT/SIGTERM 优雅关闭
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def shutdown(sig: signal.Signals) -> None:
        logger.info("收到信号 %s，正在关闭...", sig.name)
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
