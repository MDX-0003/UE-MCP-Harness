---
name: no-absolute-paths-in-docs
description: 项目文档禁止使用绝对路径，因为多台设备分别开发；接手时自行确认本机路径
metadata:
  type: project
---

# 文档路径规范

本项目在多台设备上分别开发，因此所有文档（`docs/` 目录下及 handoff 文档）**禁止使用绝对路径**。

**路径根目录约定：**

| 文件所属 | 相对根目录 | 示例 |
|---------|-----------|------|
| Harness 仓库内文件 | `UE-MCP-Harness` 仓库根 | `harness/client.py:261` |
| UE Engine 源码/插件 | UE Engine 安装目录下的 `Engine/` 目录 | `Source/Runtime/Core/Private/Misc/Paths.cpp` 或 `Plugins/Experimental/ToolsetRegistry/...` |
| UE 项目文件（项目插件、配置等） | UE 项目根目录，记为 `{UE_PROJECT_ROOT}` | `{UE_PROJECT_ROOT}/Plugins/MyPlugin/...` |

**Why:** 项目在不同机器上的路径结构不同（如 `D:\Programs\2024-2\...` vs `E:\Programs\...`），绝对路径会造成混淆和错误。

**How to apply:**
1. 写文档时，用上述相对路径替代绝对路径。
2. 接手项目时，自行确认或询问本机上的 UE Engine 安装路径和 UE 项目根路径。
3. 环境变量示例使用 `<your-xxx>` 占位符格式。
4. CLI 命令示例使用 `<your-xxx>` 占位符格式。
5. 如果需要在文档中引用具体机器的路径作为上下文，明确标注"本机验证用"并使用占位符。

[[config-and-paths]] [[dev-status]]
