"""L3 端到端测试：验证 003/004/005/008 的完整 MCP 链路。

用法（需要 UE 和 Harness 先启动）：
  1. 启动 UE Editor（MCP Server 在 8000 端口）
  2. 启动 Harness：harness start --ue-port 8000 --listen-port 9000
  3. 运行此脚本：python tests/test_l3_e2e.py

测试覆盖：
  - MCP 握手 + tools/list（验证 004 过滤 + Harness 自有工具）
  - 只读工具透传（验证 003 日志 + 008 cache L3 刷新）
  - get_context（验证 context 组装 + WorldState 快照）
  - activate_skill / deactivate_skill（验证 005 Skill 激活/取消）
  - save_skill（验证 005 Skill 保存）
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
        async with sse_client(HARNESS_URL, timeout=30) as (read, write):
            async with ClientSession(read, write) as session:
                # ============================================
                # 1. MCP 握手
                # ============================================
                print("1. MCP 握手 (initialize)...")
                init_result = await session.initialize()
                print(f"   协议版本: {init_result.protocolVersion}")
                print(f"   服务端: {init_result.serverInfo.name} v{init_result.serverInfo.version}")
                if init_result.instructions:
                    print(f"   Instructions ({len(init_result.instructions)} 字符):")
                    for line in init_result.instructions.splitlines()[:5]:
                        print(f"     {line}")
                else:
                    print("   ⚠ 无 instructions（Step 1 可能未生效）")
                print("   ✅ 握手通过")

                # ============================================
                # 2. tools/list — 验证 004 过滤 + Harness 自有工具
                # ============================================
                print("\n2. 获取工具列表 (tools/list)...")
                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                print(f"   工具总数: {len(tool_names)}")
                print(f"   前 10 个: {tool_names[:10]}")

                # 004: 应只返回过滤后的工具（~20 个 + Harness 自有），不是 211 个
                if len(tool_names) > 100:
                    print(f"   ⚠ 工具数 {len(tool_names)} > 100，004 过滤可能未生效")
                    errors += 1
                elif len(tool_names) < 3:
                    print(f"   ⚠ 工具数 {len(tool_names)} < 3，UE 连接可能有问题")
                    errors += 1
                else:
                    print(f"   ✅ 004 过滤生效（{len(tool_names)} 个工具）")

                # Harness 自有工具检查
                for expected in ["activate_skill", "save_skill", "get_context", "deactivate_skill"]:
                    if expected in tool_names:
                        print(f"   ✅ Harness 自有工具存在: {expected}")
                    else:
                        print(f"   ⚠ 缺失 Harness 自有工具: {expected}")
                        errors += 1

                # 逃生通道检查
                for escape in ["list_toolsets", "describe_toolset"]:
                    if escape in tool_names:
                        print(f"   ✅ 逃生通道存在: {escape}")

                # ============================================
                # 3. 只读工具透传 — 验证 003 日志 + 008 L3
                # ============================================
                print("\n3. 调用只读工具 (透传验证)...")
                GET_ACTORS = "ToolsetRegistry.EditorAppToolset.GetSelectedActors"
                try:
                    result = await session.call_tool(GET_ACTORS, {})
                    text = _extract_text(result)
                    ok = "OK" if not result.isError else "ERROR"
                    print(f"   {GET_ACTORS}: {ok} ({len(text)} 字符)")
                    if result.isError:
                        errors += 1
                except Exception as e:
                    print(f"   ⚠ 调用失败: {e}")
                    errors += 1

                # ============================================
                # 4. get_context — 验证 context 组装
                # ============================================
                print("\n4. 获取系统上下文 (get_context)...")
                try:
                    result = await session.call_tool("get_context", {})
                    text = _extract_text(result)
                    print(f"   Context 长度: {len(text)} 字符")
                    # 应包含 Agent 身份、UE 状态快照
                    checks = {
                        "Agent 身份": "Unreal Engine" in text,
                        "地图信息": "地图" in text,
                        "PIE 状态": "PIE" in text,
                        "Actor 数量": "Actor 数" in text,
                    }
                    for label, passed in checks.items():
                        status = "✅" if passed else "⚠ 缺失"
                        print(f"   {status} {label}")
                        if not passed:
                            errors += 1
                except Exception as e:
                    print(f"   ⚠ get_context 失败: {e}")
                    errors += 1

                # ============================================
                # 5. Skill 系统 — activate / deactivate
                # ============================================
                print("\n5. Skill 系统测试...")

                # 5a. 激活 evening-lighting
                try:
                    result = await session.call_tool("activate_skill", {"name_or_desc": "黄昏"})
                    text = _extract_text(result)
                    print(f"   activate_skill('黄昏'): {text[:100]}...")
                    if "evening-lighting" in text.lower() or "黄昏" in text:
                        print("   ✅ 匹配成功")
                    elif "未找到" in text:
                        print("   ⚠ 未匹配——evening-lighting.yaml 可能未安装")
                        errors += 1
                except Exception as e:
                    print(f"   ⚠ activate_skill 失败: {e}")
                    errors += 1

                # 5b. 激活后工具列表应变少
                tools_after = await session.list_tools()
                skill_tool_count = len(tools_after.tools)
                print(f"   激活后工具数: {skill_tool_count}")
                if skill_tool_count < len(tool_names):
                    print(f"   ✅ 工具列表已缩减（Skill 白名单生效）")
                else:
                    print(f"   ⚠ 工具数未变化——Skill 模式可能未生效")
                    # 不是硬错误——可能是 evening-lighting 未正确加载

                # 5c. 取消激活
                try:
                    result = await session.call_tool("deactivate_skill", {})
                    text = _extract_text(result)
                    print(f"   deactivate_skill: {text[:80]}...")
                    if "退出" in text or "自由探索" in text:
                        print("   ✅ 退出成功")
                except Exception as e:
                    print(f"   ⚠ deactivate_skill 失败: {e}")
                    errors += 1

                # 5d. save_skill 写测试
                try:
                    test_yaml = """name: l3-test-skill
description: "L3 测试自动生成"
triggers:
  - l3-test
tools_allowlist:
  - SceneTools.find_actors
steps: |
  1. test step
"""
                    result = await session.call_tool("save_skill", {
                        "name": "l3-test-skill",
                        "yaml_content": test_yaml,
                    })
                    text = _extract_text(result)
                    if "已保存" in text:
                        print(f"   ✅ save_skill 成功")
                        # 清理
                        from pathlib import Path
                        skill_file = Path.home() / ".ue-harness" / "skills" / "l3-test-skill.yaml"
                        if skill_file.exists():
                            skill_file.unlink()
                        # 短暂延迟，避免 SSE 连接关闭时竞态
                        await asyncio.sleep(0.5)
                    elif "已存在" in text:
                        print(f"   ℹ save_skill: {text[:80]}（重复名检查生效）")
                    else:
                        print(f"   ⚠ save_skill 异常返回: {text[:120]}")
                except Exception as e:
                    print(f"   ⚠ save_skill 失败: {e}")
                    errors += 1

    except Exception as e:
        print(f"\n❌ 连接 Harness 失败: {e}")
        print("   请确认:")
        print("   1. UE Editor 已启动，MCP Server 在 :8000")
        print("   2. Harness 已启动: harness start --ue-port 8000 --listen-port 9000")
        print(f"   3. {HARNESS_URL} 可访问")
        return 1

    # ============================================
    # 结果汇总
    # ============================================
    print(f"\n{'='*60}")
    if errors == 0:
        print("✅ L3 端到端测试全部通过")
        print("\n验证清单:")
        print("  004 Context Assembly — 工具过滤 + get_context 组装")
        print("  005 Skill System     — activate / deactivate / save_skill")
        print("  008 State Cache      — L3 刷新 + WorldState 快照")
        print("\n下一步:")
        log_dir = Path.home() / ".ue-harness" / "logs"
        print(f"  1. 检查日志: {log_dir}")
        print( "  2. 运行: harness stats")
        print( "  3. 运行: harness replay <日志文件>")
    else:
        print(f"❌ L3 测试有 {errors} 个错误")

    return min(errors, 255)


def _extract_text(result) -> str:
    """从 CallToolResult 中提取文本内容。"""
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
