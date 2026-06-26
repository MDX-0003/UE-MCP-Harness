"""直连 UE MCP Server 调用截图工具，与 tool_verify_harness_vision.py 同序列。

绕过 Harness 中间层，直接验证 UE 工具的原始行为：
  1. CaptureAssetImage(AssetPath="")  ← 对应 Harness viewport mode
  2. CaptureEditorImage()              ← 对应 Harness editor mode
  3. CaptureAssetImage(AssetPath=...)  ← 对应 Harness asset mode

自包含：如果 UE 为延迟工具加载模式（默认），自动调用 load_toolset
加载 EditorAppToolset，无需依赖 Harness 预加载。

用法（UE 必须运行，Harness 可关）:
  python tests/tool_verify_ue_vision.py
"""

import asyncio
import json
import sys

UE_URL = "http://127.0.0.1:8000/mcp"

# 截图工具所属的工具集名称
REQUIRED_TOOLSET = "ToolsetRegistry.EditorAppToolset"


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(UE_URL, timeout=120, sse_read_timeout=120) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            print("1. initialize UE...")
            init = await session.initialize()
            print(f"   {init.serverInfo.name} v{init.serverInfo.version}")

            # ── 0. 确保所需工具集已加载 ──
            await _ensure_toolset_loaded(session)

            # ── 1. CaptureAssetImage（无路径 = viewport）──
            print("\n2. test 1: CaptureAssetImage(AssetPath='')  ← viewport")
            r = await session.call_tool(
                "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
                {"AssetPath": "", "bShowUI": False},
            )
            text = _extract_text(r)
            if r.isError:
                print(f"   ❌ isError=True, 返回: '{text[:200]}'")
            elif _has_image(text):
                print(f"   ✅ 成功, 返回 {len(text)} 字符 (含 base64 图片)")
            else:
                print(f"   ⚠ 返回 {len(text)} 字符 (格式未知): '{text[:200]}'")

            # ── 2. CaptureEditorImage ──
            print("\n3. test 2: CaptureEditorImage()  ← editor")
            r = await session.call_tool(
                "ToolsetRegistry.EditorAppToolset.CaptureEditorImage", {}
            )
            text = _extract_text(r)
            if r.isError:
                print(f"   ❌ isError=True, 返回: '{text[:200]}'")
            elif _has_image(text):
                print(f"   ✅ 成功, 返回 {len(text)} 字符 (含 base64 图片)")
            else:
                print(f"   ⚠ 返回 {len(text)} 字符 (格式未知): '{text[:200]}'")

            # ── 3. 找 Asset 路径 ──
            asset_path = None
            print("\n4. 查找 Asset 路径...")
            try:
                r = await session.call_tool(
                    "ToolsetRegistry.EditorAppToolset.GetSelectedAssets", {}
                )
                assets = _parse_return_array(_extract_text(r))
                if assets:
                    asset_path = assets[0]
                    print(f"   → GetSelectedAssets: {asset_path}")
            except Exception as e:
                print(f"   GetSelectedAssets 失败: {e}")

            if asset_path:
                print(f"\n5. test 3: CaptureAssetImage(AssetPath='{asset_path}')  ← asset")
                r = await session.call_tool(
                    "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
                    {"AssetPath": asset_path, "bShowUI": True},
                )
                text = _extract_text(r)
                if r.isError:
                    print(f"   ❌ isError=True, 返回: '{text[:200]}'")
                elif _has_image(text):
                    print(f"   ✅ 成功, 返回 {len(text)} 字符 (含 base64 图片)")
                else:
                    print(f"   ⚠ 返回 {len(text)} 字符 (格式未知): '{text[:200]}'")
            else:
                print("   ⚠ 跳过 — 无选中 Asset")

    print("\n完成。")
    return 0


async def _ensure_toolset_loaded(session) -> None:
    """确保 EditorAppToolset 已加载。

    UE MCP Server 默认使用延迟工具加载（DeferredToolLoading）——
    tools/list 只返回 list_toolsets / describe_toolset / load_toolset。
    需要显式调 load_toolset 注册工具集中的工具。
    load_toolset 对已加载的工具集是安全的 no-op。
    """
    tools_result = await session.list_tools()
    tool_names = [t.name for t in tools_result.tools]

    # 检查关键工具是否已可用
    if "ToolsetRegistry.EditorAppToolset.CaptureAssetImage" in tool_names:
        print("   ✅ EditorAppToolset 已加载，跳过")
        return

    # 延迟加载模式 — 需要手动加载工具集
    if "load_toolset" not in tool_names:
        print("   ⚠ 非延迟加载模式且未找到截图工具，尝试强制加载")
    else:
        print(f"   延迟加载模式，正在加载 {REQUIRED_TOOLSET}...")

    r = await session.call_tool("load_toolset",
                                {"toolset_name": REQUIRED_TOOLSET})
    result_text = _extract_text(r)
    print(f"   load_toolset 返回: {result_text[:120]}")


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


def _has_image(text: str) -> bool:
    """判断返回文本是否包含图片数据。"""
    return "returnValue" in text or "base64" in text or "image" in text.lower() or len(text) > 500


def _parse_return_array(text: str) -> list[str]:
    """从 UE MCP 返回提取 returnValue 数组。"""
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            rv = val.get("returnValue", [])
            if isinstance(rv, list):
                return [str(x) for x in rv]
    except json.JSONDecodeError:
        pass
    return []


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
