"""验证 LevelPersistenceToolset 插件工具是否可用。

直连 UE MCP (:8000)，测试所有 5 个工具。

用法:
  python tests/tool_verify_level_persistence.py
"""

import asyncio
import io
import json
import sys

# Fix Windows GBK terminal — emoji chars crash the encode path
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

UE_URL = "http://127.0.0.1:8000/mcp"
TOOLSET_FULL_NAME = "LevelPersistenceToolset.LevelPersistenceToolset"

EXPECTED_TOOLS = [
    "LevelPersistenceToolset.SaveCurrentLevel",
    "LevelPersistenceToolset.SaveAsset",
    "LevelPersistenceToolset.SaveAll",
    "LevelPersistenceToolset.ListDirtyPackages",
    "LevelPersistenceToolset.GetLevelFingerprint",
]


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    errors = 0

    async with streamablehttp_client(UE_URL, timeout=30, sse_read_timeout=30) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 0. 加载 toolset（UE MCP 默认 deferred 模式，需先 load） ──
            print("0. 加载 LevelPersistenceToolset toolset...")
            try:
                r = await session.call_tool("load_toolset", {"toolset_name": TOOLSET_FULL_NAME})
                text = _extract_text(r)
                print(f"   {text[:200]}")
            except Exception as e:
                print(f"   ❌ 加载失败: {e}")
                return 1

            # ── 1. 确认工具出现在 tools/list 中 ──
            print("1. 检查 tools/list 中的 LevelPersistenceToolset 工具...")
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            lpt_tools = [n for n in tool_names if "LevelPersistence" in n]

            for expected in EXPECTED_TOOLS:
                found = False
                for n in tool_names:
                    if n.endswith(expected):
                        found = True
                        break
                status = "✅" if found else "❌"
                if not found:
                    errors += 1
                print(f"   {status} {expected}")

            all_lpt = [n for n in tool_names if "LevelPersistence" in n]
            if len(all_lpt) != 5:
                print(f"   ⚠  期望 5 个工具，实际找到 {len(all_lpt)}: {all_lpt}")

            # ── 2. ListDirtyPackages（只读，安全） ──
            print("\n2. 调用 ListDirtyPackages...")
            try:
                r = await session.call_tool(
                    "LevelPersistenceToolset.LevelPersistenceToolset.ListDirtyPackages", {}
                )
                text = _extract_text(r)
                print(f"   ✅ 返回: {text}")
                data = json.loads(text)
                if not isinstance(data, list):
                    print(f"   ❌ 期望 JSON 数组，得到: {type(data)}")
                    errors += 1
            except Exception as e:
                print(f"   ❌ 异常: {e}")
                errors += 1

            # ── 3. GetLevelFingerprint（只读） ──
            print("\n3. 调用 GetLevelFingerprint（当前关卡）...")
            try:
                r = await session.call_tool(
                    "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
                    {"LevelPath": ""},
                )
                text = _extract_text(r)
                data = json.loads(text)
                print(f"   ✅ packagePath: {data.get('packagePath', 'N/A')}")
                print(f"      packageGuid: {data.get('packageGuid', 'N/A')}")
                print(f"      fileSizeBytes: {data.get('fileSizeBytes', 'N/A')}")
                print(f"      lastModified: {data.get('lastModified', 'N/A')}")
                print(f"      isLoaded: {data.get('isLoaded', 'N/A')}")
                print(f"      actorCount: {data.get('actorCount', 'N/A')}")
                print(f"      actorNameHash: {data.get('actorNameHash', 'N/A')}")
                if "packagePath" not in data:
                    errors += 1
            except Exception as e:
                print(f"   ❌ 异常: {e}")
                errors += 1

            # ── 4. GetLevelFingerprint（指定路径） ──
            if errors == 0:
                print("\n4. 调用 GetLevelFingerprint（不存在的路径，应返回文件级信息）...")
                try:
                    r = await session.call_tool(
                        "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
                        {"LevelPath": "/Game/NonExistent/Level"},
                    )
                    text = _extract_text(r)
                    data = json.loads(text)
                    print(f"   ✅ isLoaded: {data.get('isLoaded')} (应为 false)")
                    if data.get("isLoaded") is not False:
                        errors += 1
                except Exception as e:
                    print(f"   ❌ 异常: {e}")
                    errors += 1

            # ── 5. SaveCurrentLevel ──
            print("\n5. 调用 SaveCurrentLevel...")
            try:
                r = await session.call_tool(
                    "LevelPersistenceToolset.LevelPersistenceToolset.SaveCurrentLevel", {}
                )
                text = _extract_text(r)
                data = json.loads(text)
                status = data.get("status", "unknown")
                if status == "saved":
                    print(f"   ✅ 保存成功")
                    print(f"      packagePath: {data.get('packagePath', 'N/A')}")
                    print(f"      packageGuid: {data.get('packageGuid', 'N/A')}")
                    print(f"      fileSizeBytes: {data.get('fileSizeBytes', 'N/A')}")
                    print(f"      lastModified: {data.get('lastModified', 'N/A')}")
                    print(f"      actorCount: {data.get('actorCount', 'N/A')}")
                    print(f"      actorNameHash: {data.get('actorNameHash', 'N/A')}")
                else:
                    print(f"   ❌ 保存失败: {text[:200]}")
                    errors += 1
            except Exception as e:
                print(f"   ❌ 异常: {e}")
                errors += 1

            # ── 6. SaveAll ──
            print("\n6. 调用 SaveAll...")
            try:
                r = await session.call_tool(
                    "LevelPersistenceToolset.LevelPersistenceToolset.SaveAll", {}
                )
                text = _extract_text(r)
                data = json.loads(text)
                status = data.get("status", "unknown")
                saved_count = data.get("savedCount", -1)
                packages = data.get("packages", [])
                print(f"   ✅ status={status}, savedCount={saved_count}, packages={len(packages)}")
                if status != "saved":
                    print(f"   ⚠ 状态异常: {text[:200]}")
                    errors += 1
            except Exception as e:
                print(f"   ❌ 异常: {e}")
                errors += 1

            # ── 7. SaveAsset ──
            if errors == 0:
                print("\n7. 调用 SaveAsset（当前关卡路径）...")
                try:
                    # Get current level path first
                    r = await session.call_tool(
                        "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint", {}
                    )
                    text = _extract_text(r)
                    data = json.loads(text)
                    current_path = data.get("packagePath", "")
                    if current_path:
                        r = await session.call_tool(
                            "LevelPersistenceToolset.LevelPersistenceToolset.SaveAsset",
                            {"AssetPath": current_path},
                        )
                        text = _extract_text(r)
                        data = json.loads(text)
                        status = data.get("status", "unknown")
                        if status == "saved":
                            print(f"   ✅ 保存成功: {current_path}")
                        else:
                            print(f"   ❌ 保存失败: {text[:200]}")
                            errors += 1
                    else:
                        print("   ⚠ 跳过 — 无法获取当前关卡路径")
                except Exception as e:
                    print(f"   ❌ 异常: {e}")
                    errors += 1

    print(f"\n{'='*50}")
    print(f"结果: {errors} 个错误" if errors else "结果: ✅ 全部通过")
    return min(errors, 255)


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
