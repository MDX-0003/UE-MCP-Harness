"""验证 vision_screenshot mode="asset" — 显式传入 AssetPath 截图。

流程:
  1. GetSelectedAssets → 取第一个已选中资产
  2. 若无选中，GetOpenAssets → 取第一个已打开的资产
  3. 调用 vision_screenshot(mode="asset", asset_path=..., hide_ui=True)
  4. 确认截图成功 + Vision 分析触发

用法（Harness 必须运行）:
  python tests/tool_verify_asset_screenshot.py
"""

import asyncio
import sys

HARNESS_URL = "http://127.0.0.1:9000/mcp"


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(HARNESS_URL, timeout=120, sse_read_timeout=120) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 1. 找 Asset ──
            asset_path = None

            # 1a. 已选中的资产
            print("1. 查找可用 Asset...")
            r = await session.call_tool("ToolsetRegistry.EditorAppToolset.GetSelectedAssets", {})
            selected = _parse_json_array(_extract_text(r))
            print(f"   GetSelectedAssets: {selected}")
            if selected:
                asset_path = selected[0]
                print(f"   → 使用已选中资产: {asset_path}")

            # 1b. 已打开的资产
            if not asset_path:
                r = await session.call_tool("ToolsetRegistry.EditorAppToolset.GetOpenAssets", {})
                opened = _parse_json_array(_extract_text(r))
                print(f"   GetOpenAssets: {opened}")
                if opened:
                    asset_path = opened[0]
                    print(f"   → 使用已打开资产: {asset_path}")

            if not asset_path:
                print("   ❌ 无可用 Asset。请在 Content Browser 中选中一个资产后重试")
                return 1

            # ── 2. 截图 ──
            print(f"\n2. vision_screenshot mode='asset' asset_path='{asset_path}'...")
            ctx_before = await _get_ctx(session)
            r = await session.call_tool("vision_screenshot", {
                "mode": "asset",
                "asset_path": asset_path,
                "hide_ui": True,
            })
            text = _extract_text(r)
            print(f"   isError={r.isError}, 返回: '{text}'")
            if r.isError:
                print(f"   ❌ 截图失败")
                return 1

            # ── 3. 等待 Vision ──
            print("\n3. 等待 Vision API（5 秒）...")
            await asyncio.sleep(5)

            ctx_after = await _get_ctx(session)
            if _has_vision_verdict(ctx_after) and not _has_vision_verdict(ctx_before):
                print("   ✅ VisionInterceptor 已触发")
                _print_vision_lines(ctx_after)
            else:
                print("   ⚠ 未检测到视觉验证段落")

    print("\n完成。")
    return 0


async def _get_ctx(session) -> str:
    r = await session.call_tool("get_context", {})
    return _extract_text(r)


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


def _has_vision_verdict(text: str) -> bool:
    return "上次视觉验证" in text


def _print_vision_lines(text: str) -> None:
    for line in text.splitlines():
        if "视觉验证" in line:
            print(f"   >>> {line.strip()[:200]}")


def _parse_json_array(text: str) -> list[str]:
    """尝试从 MCP 返回文本中提取 JSON 数组。"""
    import json
    text = text.strip()
    # 可能是纯 JSON 数组
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except json.JSONDecodeError:
        pass
    # 可能是 {"returnValue": [...]} 嵌套
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            rv = val.get("returnValue", [])
            if isinstance(rv, list):
                return rv
    except json.JSONDecodeError:
        pass
    return []


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
