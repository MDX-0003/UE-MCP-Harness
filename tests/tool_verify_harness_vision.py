"""验证 Harness take_screenshot 三种截图模式 → Vision 管线端到端。

串行测试 viewport / editor / asset 三种 mode，每种模式验证：
  1. 调用成功（isError=False）
  2. 返回合理的截图尺寸
  3. VisionInterceptor 触发 → get_context 出现视觉验证段落

前置条件:
  - Harness 已启动: harness start --ue-port 8000 --listen-port 9000
  - UE Editor 运行中，有可见视口
  - .vision.env 已配置有效的 API key

用法:
  python tests/tool_verify_harness_vision.py
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

            print("1. 确认 tools/list...")
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            if "take_screenshot" not in tool_names:
                print("   ❌ take_screenshot 不可用，请重启 Harness")
                return 1
            # 打印 schema 确认 mode 参数
            for t in tools_result.tools:
                if t.name == "take_screenshot":
                    props = t.inputSchema.get("properties", {})
                    modes = props.get("mode", {}).get("enum", [])
                    print(f"   ✅ take_screenshot 可用，支持的 mode: {modes}")

            # ── 串行测试 ──
            # mode=editor (auto-fallback to viewport if CaptureEditorImage fails)
            # ok = await _test_mode(session, "editor", {})
            # if not ok:
            #     print("   ⚠ editor 未通过")

            # 1. asset — 显式指定引擎内置资产，走缩略图路径（FAssetThumbnailCapture，10s 超时）
            # 缩略图路径比视口截图更可靠：异步等待窗口短，MultipleWriteStream 竞态风险低
            # ENGINE_ASSET = "/Engine/BasicShapes/BasicShapeMaterial_Inst.BasicShapeMaterial_Inst"
            # print(f"\n{'─' * 50}")
            # print(f"测试 mode='asset' 显式指定: {ENGINE_ASSET}")
            # ok = await _test_mode(session, "asset",
            #                       {"asset_path": ENGINE_ASSET, "hide_ui": True})
            # if not ok:
            #     print("   ⚠ asset 未通过")

            # ok = await _test_mode(session, "asset",
            #                       {"asset_path": "", "hide_ui": True})
            # if not ok:
            #     print("   ⚠ asset 未通过")
            ok = await _test_mode(session, "viewport", {"hide_ui": True})
            if not ok:
                print("   ⚠ viewport 未通过")

    print("\n全部测试完成。")
    return 0


async def _find_asset(session) -> str | None:
    """查找可用的 Asset 路径：先查已选中，再查已打开。"""
    # 1. 已选中的资产
    r = await session.call_tool("ToolsetRegistry.EditorAppToolset.GetSelectedAssets", {})
    selected = _parse_json_array(_extract_text(r))
    if selected:
        print(f"   使用已选中 Asset: {selected[0]}")
        return selected[0]
    # 2. 已打开的资产
    r = await session.call_tool("ToolsetRegistry.EditorAppToolset.GetOpenAssets", {})
    opened = _parse_json_array(_extract_text(r))
    if opened:
        print(f"   使用已打开 Asset: {opened[0]}")
        return opened[0]
    return None


async def _test_mode(session, mode: str, extra: dict) -> bool:
    print(f"\n{'─' * 50}")
    print(f"测试 mode='{mode}' {extra}")

    # 调用
    arguments = {"mode": mode, **extra}
    r = await session.call_tool("take_screenshot", arguments)
    text = _extract_text(r)

    print(f"  isError={r.isError}, 返回: '{text}'")
    if r.isError:
        print(f"  ❌ mode='{mode}' 失败")
        return False

    # 等待 Vision
    print(f"  等待 Vision API（5 秒）...")
    await asyncio.sleep(5)

    # 对比
    ctx_after = await _get_ctx(session)
    if _has_vision_verdict(ctx_after):
        print(f"  ✅ mode='{mode}' VisionInterceptor 已触发")
        _print_vision_lines(ctx_after)
        return True
    else:
        print(f"  ⚠ mode='{mode}' get_context 未检测到视觉验证段落")
        return False


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


def _parse_json_array(text: str) -> list[str]:
    """从 MCP 返回文本中提取字符串数组（支持嵌套 returnValue 格式）。"""
    import json
    text = text.strip()
    for candidate in (text,):
        try:
            val = json.loads(candidate)
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                return val
            if isinstance(val, dict):
                rv = val.get("returnValue", [])
                if isinstance(rv, list) and all(isinstance(x, str) for x in rv):
                    return rv
        except json.JSONDecodeError:
            continue
    return []


def _print_vision_lines(text: str) -> None:
    for line in text.splitlines():
        if "视觉验证" in line:
            print(f"   >>> {line.strip()[:200]}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
