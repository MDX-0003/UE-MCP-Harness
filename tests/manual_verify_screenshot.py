"""手动验证：通过 Harness MCP 调用 CaptureEditorImage，确认截图链路 + VisionInterceptor 触发。

用法（需 Harness 已启动）：
  python tests/manual_verify_screenshot.py
"""

import asyncio
import json
import sys
from mcp import ClientSession
from mcp.client.sse import sse_client

HARNESS_URL = "http://127.0.0.1:9000/mcp"
USE_STREAMABLE_HTTP = True  # 新版 Streamable HTTP；False = 旧 SSE


async def main() -> int:
    print(f"连接到 Harness: {HARNESS_URL}")

    if USE_STREAMABLE_HTTP:
        from mcp.client.streamable_http import streamablehttp_client
        transport = streamablehttp_client(HARNESS_URL, timeout=120, sse_read_timeout=120)
    else:
        transport = sse_client("http://127.0.0.1:9000/sse", timeout=30)

    async with transport as streams:
        read, write = streams[0], streams[1]  # 第3个是 get_session_id callback
        async with ClientSession(read, write) as session:
            # 1. 握手
            print("\n1. initialize...")
            init_result = await session.initialize()
            print(f"   服务端: {init_result.serverInfo.name} v{init_result.serverInfo.version}")

            # 2. 尝试截图
            print("\n2. 调用 CaptureEditorImage...")
            try:
                result = await session.call_tool(
                    "ToolsetRegistry.EditorAppToolset.CaptureEditorImage",
                    {},
                )
                text = _extract_text(result)
                is_error = result.isError

                print(f"   isError: {is_error}")
                print(f"   返回长度: {len(text)} 字符")

                if is_error:
                    print(f"   ❌ 截图失败: {text[:500]}")
                    print("\n   可能原因:")
                    print("   - UE 编辑器窗口最小化或不可见")
                    print("   - 远程桌面/headless 环境")
                    print("   - 没有可捕获的编辑器视口")
                elif "Failed to capture" in text:
                    print(f"   ⚠ UE 返回错误: {text[:500]}")
                    print("   截图工具本身正常，但未找到可捕获的窗口")
                elif "image" in text.lower() or "base64" in text.lower() or len(text) > 1000:
                    print(f"   ✅ 疑似成功！包含 {len(text)} 字符数据")
                    print(f"   前 200 字符: {text[:200]}")
                else:
                    print(f"   ⚠ 未知格式: {text[:500]}")

            except Exception as e:
                print(f"   ❌ 工具调用异常: {e}")

            # 3. 检查 get_context 中的视觉验证段落（VisionInterceptor 产物）
            print("\n3. 检查 get_context（确认 VisionInterceptor 是否写入 verdict）...")
            try:
                result = await session.call_tool("get_context", {})
                text = _extract_text(result)
                if "视觉验证" in text:
                    print("   ✅ get_context 包含视觉验证段落！VisionInterceptor 已触发")
                    # 提取相关行
                    for line in text.splitlines():
                        if "视觉验证" in line or "验证时间" in line:
                            print(f"     {line.strip()}")
                else:
                    print("   ⚠ get_context 中无视觉验证段落 — VisionInterceptor 未触发或截图失败")
            except Exception as e:
                print(f"   ❌ get_context 失败: {e}")

    print("\n完成。")
    return 0


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
