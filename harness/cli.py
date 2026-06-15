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
      4. 构建 mcp Server，注册 list_tools / call_tool
      5. 通过 SSE transport 启动 HTTP Server
    """
    from harness.config import Config
    from harness.client import McpClientSession
    from harness.server import build_server
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

    async def run() -> None:
        try:
            # 1. 连接 UE
            await ue_client.connect()
            logger.info("✓ 已连接到 UE MCP Server (session: %s)", ue_client.session_id or "无")

            # 2. 预加载工具集（可配置跳过，用于快速调试）
            if config.preload_all_toolsets:
                tool_count = await ue_client.preload_all_toolsets()
                logger.info("✓ 已预加载 %d 个工具", tool_count)
            else:
                tools = await ue_client.list_tools()
                logger.info("✓ 已获取 %d 个工具（跳过预加载）", len(tools))

            # 3. 构建并启动 MCP Server
            server = build_server(config, ue_client)
            await serve(server, host=config.listen_host, port=config.listen_port)

        except Exception as e:
            logger.error("启动失败: %s", e)
            raise

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

    args = parser.parse_args()

    if args.command == "start":
        # --no-preload 覆盖预加载配置
        if args.no_preload:
            import os
            os.environ["HARNESS_PRELOAD_TOOLSETS"] = "false"
        return cmd_start(args)
    elif args.command == "version":
        from harness import __version__
        print(f"ue-agent-harness v{__version__}")
        return 0
    elif args.command is None:
        parser.print_help()
        return 0
    else:
        print(f"命令 '{args.command}' 尚未实现。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
