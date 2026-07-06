# UE Agent Harness — 新机设置指南

从零开始在任意 Windows 机器上搭建 Harness 开发/运行环境。

## 1. 前置条件

| 依赖 | 最低版本 | 安装 |
|------|---------|------|
| Python | 3.12+ | `winget install python` 或从 python.org 下载 |
| uv | 0.10+ | `winget install astral-sh.uv` 或 `pip install uv` |
| Git | 任意 | `winget install Git.Git` |

## 2. 克隆并安装

```bash
git clone <repo-url> ue-agent-harness
cd ue-agent-harness

# 创建虚拟环境 + 安装全部依赖（含 dev）
uv sync
```

`.python-version` 已提交到仓库，`uv sync` 会自动选用合适的 Python 版本。

## 3. 配置文件

### 3.1 `.env` — Harness 主配置

在项目根目录创建 `.env`（**不提交 git**）：

```bash
# .env — Harness 本地配置（可选，以下为默认值）

# UE MCP Server 连接
HARNESS_UE_PORT=8000
HARNESS_UE_HOST=127.0.0.1

# Harness 监听端口（Claude Code 连接此端口）
HARNESS_LISTEN_PORT=9000
HARNESS_LISTEN_HOST=127.0.0.1

# UE 项目路径（用于截图 fallback）
HARNESS_UE_PROJECT_ROOT=D:/Path/To/Your/UEProject

# UE 截图目录（硬覆盖）
# HARNESS_UE_SCREENSHOT_DIR=

# 日志级别
HARNESS_LOG_LEVEL=INFO

# 启动时预加载全部工具集（默认 true，设为 false 加快启动）
HARNESS_PRELOAD_TOOLSETS=true
```

### 3.2 `.vision.env` — Vision 视觉验证 API

在项目根目录创建 `.vision.env`（**不提交 git，含 API Key**）：

```bash
# .vision.env — Vision Sub-Agent 配置

# API Key（必填 — Anthropic 兼容 API 的 key）
HARNESS_VISION_API_KEY=your-api-key-here

# API 端点
HARNESS_VISION_API_BASE_URL=https://your-proxy.example.com/anthropic

# Vision 模型（需要 vision 能力）
HARNESS_VISION_MODEL=claude-sonnet-4-6
```

也可通过环境变量设置：`export HARNESS_VISION_API_KEY=...`

### 3.3 `~/.claude.json` — Claude Code 发现 Harness

**此文件在仓库外**（用户目录），必须在每台新机上手动编辑。

Claude Code VS Code 扩展的 MCP 服务器配置在 `~/.claude.json` 的顶层 `mcpServers` 中（**不是** `~/.claude/mcp.json`，VS Code 扩展不读那个文件）。

文件路径：`C:\Users\<你的用户名>\.claude.json`

在已有的 `mcpServers` 对象中添加 `ue-harness`：

```json
{
  "mcpServers": {
    "gortex": {
      "args": ["mcp"],
      "command": "C:\\Users\\...\\gortex\\gortex.exe",
      "env": {}
    },
    "ue-harness": {
      "type": "http",
      "url": "http://127.0.0.1:9000/mcp"
    }
  }
}
```

Gortex 使用 stdio 模式（`command` + `args`），Harness 使用 HTTP 模式（`type` + `url`）。两种可共存。

> **注意**：`~/.claude.json` 也包含会话统计、迁移标记等运行时数据，手动编辑时只改 `mcpServers` 对象，不要动其他字段。

### 3.4 `.claude/settings.json` — Claude Code 权限

项目级 `.claude/settings.json` 已提交。建议在新机上追加以下权限，避免每次调 Harness 工具都弹确认框：

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run *)",
      "mcp__ue-harness__*",
      "mcp__gortex__*"
    ]
  }
}
```

## 4. UE Editor 设置

在 UE 5.8 编辑器中启用 MCP Server：

1. 打开 UE Editor
2. **Edit → Editor Preferences**
3. 搜索 `Model Context Protocol`
4. 勾选 **Auto Start Server**
5. 确认端口为 `8000`
6. 重启编辑器（或手动点 Start Server）

### 4.1 LevelPersistenceToolset 插件

Hard Boundary 指纹校验依赖 UE 侧插件。确保 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/` 已编译并启用。

若插件不可用，Harness 会降级运行（指纹校验和漂移检测跳过），不影响核心功能。

## 5. 启动验证

### 5.1 启动 Harness

```bash
# 确保 UE Editor 已运行且 MCP Server 已启用
uv run harness start --ue-port 8000 --listen-port 9000
```

正常输出：
```
UE Agent Harness v0.1.0 正在启动...
UE MCP Server: http://127.0.0.1:8000/mcp
Harness 监听: http://127.0.0.1:9000/sse
正在连接 UE MCP Server...
MCP 握手成功。Session: xxx, 协议版本: 2025-11-25
✓ 已预加载 211 个工具
✓ 截图专用 session 已就绪
✓ State Cache 已就绪
✓ LevelPersistenceToolset 5 工具全部就绪
```

### 5.2 验证 MCP 握手

```bash
curl -s --max-time 5 -X POST http://127.0.0.1:9000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}'
```

预期返回含 `"serverInfo":{"name":"ue-agent-harness"}` 的 JSON。

### 5.3 验证 Claude Code 发现 MCP

1. 在 VS Code 中 `Ctrl+Shift+P` → `Developer: Reload Window`
2. 检查 Claude Code 面板的 MCP 服务器列表是否出现 `ue-harness`
3. 或查看 Harness 日志是否有来自 Claude Code 的 initialize 请求

### 5.4 运行测试

```bash
uv run pytest tests/ -v
# 预期：319 passed, 4 skipped
```

## 6. 常见问题

### 6.1 `HTTP 502` 连接错误

**现象**：Harness 启动时报 `HTTP 502:`

**原因**：Windows 系统代理（Fiddler 等）拦截了 `127.0.0.1:8000` 的请求。

**修复**：`harness/client.py` 已设置 `trust_env=False` 绕过代理（v0.1.0+）。如果仍有问题，检查：
```bash
netsh winhttp show proxy
```
如有代理，将其关闭或添加 `127.0.0.1` 到绕过列表。

### 6.2 `All connection attempts failed`

**现象**：启动时报连接失败。

**原因**：UE Editor 未运行或 MCP Server 未启用。

**检查**：
```bash
# 确认 UE 进程存在
tasklist | grep UnrealEditor

# 确认端口 8000 在监听
netstat -an | grep 8000
```

### 6.3 Claude Code 找不到 Harness

**检查清单**：
1. `~/.claude.json` 的 `mcpServers` 中是否包含 `ue-harness`？
2. Harness 是否正在运行？（`curl http://127.0.0.1:9000/mcp`）
3. 是否已 Reload VS Code 窗口？
4. `.claude/settings.json` 中 `permissions.allow` 是否包含 `mcp__ue-harness__*`？

### 6.4 `ModuleNotFoundError: No module named 'PIL'`

运行：`uv sync`（Pillow 已在 `pyproject.toml` 中声明）

## 7. 文件参考

| 文件 | 位置 | 提交 git? | 说明 |
|------|------|:---:|------|
| `.python-version` | 项目根 | ✅ | uv 用此文件选择 Python 版本 |
| `.env` | 项目根 | ❌ | Harness 主配置（端口、路径） |
| `.vision.env` | 项目根 | ❌ | Vision API Key（敏感） |
| `.claude/mcp.json` | 项目根 | ✅ | Harness MCP 服务器声明 |
| `.claude/settings.json` | 项目根 | ✅ | Claude Code 权限白名单 |
| `.claude/settings.local.json` | 项目根 | ❌ | 本机 Claude Code 钩子 |
| `~/.claude.json` | 用户目录 | — | **用户级 MCP 配置（新机必编辑 mcpServers）** |
| `.venv/` | 项目根 | ❌ | uv 虚拟环境（`uv sync` 创建） |
| `.ue-harness/` | 项目根 | ❌ | 本地日志、快照、缓存 |

## 8. 开发环境

```bash
# 安装（含 dev 依赖）
uv sync

# 运行测试
uv run pytest tests/ -v

# 运行单个模块
uv run pytest tests/test_client.py -v

# 启动 Harness
uv run harness start

# 查看统计
uv run harness stats

# 回放日志
uv run harness replay <session_id>
```

Gortex 代码智能工具（可选）：在项目根运行 `gortex daemon start --detach`，之后 `smart_context` 和代码导航功能可用。
