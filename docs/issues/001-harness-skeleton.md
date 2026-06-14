# 001 — Harness 骨架：端到端连线

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

建立 Harness 进程的基本骨架——CLI 入口、配置加载、面向 LLM 的 MCP Server（`mcp` SDK）启动。此时不连接 UE，仅验证 LLM → Harness 的 MCP 链路是通的。

Harness 启动后，LLM 客户端（Claude Code / MCP Inspector）可以连接到 Harness 的 MCP Server，调用 `ping` 并收到 `pong` 响应。

同时搭建 Python 项目结构（`pyproject.toml`、模块目录、测试框架），确保从第一天起就能跑测试。

## 验收标准

- [ ] `harness start --listen-port 9000` 启动成功，STDOUT 显示监听地址
- [ ] MCP Inspector 或 `mcp dev` 连接到 `localhost:9000`，`initialize` 握手成功
- [ ] 调用 `ping` → 返回 `{}`
- [ ] `tools/list` 返回空列表（UE 尚未连接，预期行为）
- [ ] `harness --version` 输出版本号
- [ ] `harness --help` 输出所有子命令
- [ ] `pyproject.toml` 定义依赖：`mcp`、`httpx`、`pydantic`、`structlog`、`pyyaml`
- [ ] `pytest` 可运行（至少一个占位测试通过）
- [ ] 项目目录结构已建立（按 architecture.md §4 的布局）

## 阻塞

无——可立即开始。
