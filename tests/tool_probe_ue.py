"""探测 UE MCP Server 中可用的截图工具。

用于开发/调试时快速确认 UE 端注册了哪些截图工具、工具名是否正确、
调用参数是什么。直连 UE MCP（不经过 Harness），不依赖 Vision API。

前置条件:
  - UE Editor 运行中，MCP Server 在 8000 端口
  - `pip install mcp httpx`

用法:
  python tests/tool_probe_ue.py

输出解读:
  - 列出所有已加载工具集，标记截图相关工具
  - 尝试加载可能未启用的工具集（如 SlateInspectorToolset）
  - 逐个调用截图工具，报告成功/失败及返回数据量
  - 如果所有截图工具都报 isError，可能是 UE 窗口不可见
"""

import asyncio
import sys

UE_URL = "http://127.0.0.1:8000/mcp"

# 已确认存在的截图工具
SCREENSHOT_TOOLS = [
    ("ToolsetRegistry.EditorAppToolset.CaptureEditorImage", {}),
    ("ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
     {"AssetPath": "", "bShowUI": False}),
]


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(UE_URL, timeout=120, sse_read_timeout=120) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            print("1. initialize UE...")
            init_result = await session.initialize()
            print(f"   服务端: {init_result.serverInfo.name} v{init_result.serverInfo.version}")

            # 列出所有工具集
            print("\n2. list_toolsets...")
            r = await session.call_tool("list_toolsets", {})
            text = _extract_text(r)
            for line in text.splitlines():
                clean = line.strip()
                if clean.startswith("- "):
                    print(f"   {clean}")

            # 检查已知截图工具
            print("\n3. 逐个调用截图工具:")
            for tool_name, args in SCREENSHOT_TOOLS:
                print(f"\n   调用: {tool_name}")
                try:
                    r = await session.call_tool(tool_name, args)
                    text = _extract_text(r)
                    if r.isError:
                        print(f"   ❌ isError: True → {text[:200]}")
                    elif len(text) > 500:
                        print(f"   ✅ 成功 — {len(text)} 字符 base64")
                    else:
                        print(f"   ⚠ 返回短文本: {text[:200]}")
                except Exception as e:
                    print(f"   ❌ 调用异常: {e}")

    print("\n完成。")
    return 0


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
