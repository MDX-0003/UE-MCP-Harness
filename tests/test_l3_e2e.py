"""L3 端到端测试：验证 003 日志落地。

用法（需要 UE 和 Harness 先启动）：
  1. 启动 UE Editor（MCP Server 在 8000 端口）
  2. 启动 Harness：harness start --ue-port 8000 --listen-port 9000
  3. 运行此脚本：python tests/test_l3_e2e.py
  4. 检查日志：harness stats
  5. 回放日志：harness replay <日志文件>

此脚本使用 MCP SDK 的 sse_client 连接 Harness SSE 端点，
完成 MCP 握手后调用 2 个工具，触发 003 ToolCallLogger 写日志。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client

HARNESS_URL = "http://127.0.0.1:9000/sse"


async def main() -> int:
    errors = 0

    print(f"连接到 Harness: {HARNESS_URL}")

    try:
        async with sse_client(HARNESS_URL, timeout=10) as (read, write):
            async with ClientSession(read, write) as session:
                # 1. MCP 握手
                print("1. MCP 握手 (initialize)...")
                init_result = await session.initialize()
                print(f"   协议版本: {init_result.protocolVersion}")
                print(f"   服务端: {init_result.serverInfo.name} v{init_result.serverInfo.version}")

                # 2. 获取工具列表
                print("2. 获取工具列表 (tools/list)...")
                tools_result = await session.list_tools()
                tool_count = len(tools_result.tools)
                print(f"   发现 {tool_count} 个工具")

                if tool_count == 0:
                    print("   ⚠ 工具列表为空，Harness 可能未正确连接 UE")
                    errors += 1
                    return errors

                # 3. 调用只读工具 1: 获取选中 Actor（无参）
                GET_ACTORS = "ToolsetRegistry.EditorAppToolset.GetSelectedActors"
                print(f"3. 调用: {GET_ACTORS}()...")
                try:
                    result = await session.call_tool(GET_ACTORS, {})
                    text = _extract_text(result)
                    print(f"   返回 ({'ERROR' if result.isError else 'OK'}): {text[:200]}...")
                except Exception as e:
                    print(f"   ⚠ 调用失败: {e}")
                    errors += 1

                # 4. 调用只读工具 2: 获取当前关卡（无参）
                GET_LEVEL = "toolset_registry.toolsets.core.scene.SceneTools.get_current_level"
                print(f"4. 调用: {GET_LEVEL}()...")
                try:
                    result = await session.call_tool(GET_LEVEL, {})
                    text = _extract_text(result)
                    print(f"   返回 ({'ERROR' if result.isError else 'OK'}): {text[:200]}...")
                except Exception as e:
                    print(f"   ⚠ 调用失败: {e}")
                    errors += 1

    except Exception as e:
        print(f"\n❌ 连接 Harness 失败: {e}")
        print("   请确认:")
        print("   1. UE Editor 已启动，MCP Server 在 :8000")
        print("   2. Harness 已启动: harness start --ue-port 8000 --listen-port 9000")
        print(f"   3. {HARNESS_URL} 可访问")
        return 1

    if errors == 0:
        print("\n✅ L3 基础链路通过: Harness 成功代理工具调用")
        print("\n下一步验证:")
        log_dir = Path.home() / ".ue-harness" / "logs"
        print(f"  1. 检查日志目录: {log_dir}")
        print( "  2. 运行: harness stats")
        print( "  3. 日志文件应该有 2 条 tool call 记录")
    else:
        print(f"\n❌ L3 测试有 {errors} 个错误")

    return errors


def _extract_text(result) -> str:
    """从 CallToolResult 中提取文本内容。"""
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
