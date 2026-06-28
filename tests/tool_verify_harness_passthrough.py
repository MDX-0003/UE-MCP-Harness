"""Harness 透传模式测试——直接调 CaptureAssetImage，不经过 take_screenshot。

验证 MultipleWriteStream bug 是否在"Harness 透传原生 UE 工具"场景下触发。

用法:
  1. harness start --ue-port 8000 --listen-port 9000
  2. python tests/tool_verify_harness_passthrough.py
"""

import asyncio
import sys

HARNESS_URL = "http://127.0.0.1:9000/mcp"


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(HARNESS_URL, timeout=120, sse_read_timeout=120) as (
        read, write, _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 确认工具可用
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            target = "ToolsetRegistry.EditorAppToolset.CaptureAssetImage"
            print(f"tools/list: {len(names)} tools, '{target}' in list: {target in names}")

            if target not in names:
                print("❌ CaptureAssetImage 不在工具列表中（denylist 可能未放开）")
                return 1

            print(f"\n── 透传调用 {target}（不经过 take_screenshot）──")
            r = await session.call_tool(target, {"assetPath": "", "bShowUI": False})

            text = ""
            for item in r.content:
                if hasattr(item, "text"):
                    text += item.text
            print(f"isError={r.isError}, len={len(text)}")
            if len(text) < 300:
                print(f"body: {text}")
            else:
                print(f"first 200: {text[:200]}")

            return 0 if not r.isError else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
