# UE Agent Harness

UE5.8 编辑器的 AI Agent 中间层。Harness 作为 MCP Server 面向 LLM（Claude/GPT），作为 MCP Client 面向 UE MCP Server，提供上下文组装、状态缓存、视觉验证闭环、长任务记忆压缩、安全护栏。

## 快速开始

```bash
# 1. 在 UE 编辑器中启用 MCP Server
#    Editor Preference → General → Model Context Protocol → Auto Start Server

# 2. 启动 Harness
harness start --ue-port 8000 --listen-port 9000

# 3. 在 Claude Code 或其他 MCP 客户端中连接到 localhost:9000
```

## 文档

- [架构与实施方案](docs/architecture.md)
- [领域语言词汇表](docs/CONTEXT.md)
- [架构决策记录](docs/adr/)
- [开发 Issue](docs/issues/)

## 项目结构

```
ue-agent-harness/
├── harness/              # Python 包
│   ├── cli.py            # CLI 入口
│   ├── server.py         # MCP Server（面向 LLM）
│   ├── client.py         # MCP Client（面向 UE）
│   ├── config.py         # 配置管理
│   ├── context/          # 上下文组装
│   ├── verification/     # 视觉验证
│   ├── state/            # Write-Through State Cache
│   ├── memory/           # 任务记忆压缩
│   ├── recovery/         # 错误恢复
│   ├── safety/           # 安全护栏
│   └── observability/    # 日志与回放
├── skills/               # Harness Skill 示例
├── tests/                # 测试
├── docs/                 # 文档
│   ├── architecture.md
│   ├── CONTEXT.md
│   ├── adr/              # 架构决策记录
│   └── issues/           # 开发 Issue
└── pyproject.toml
```
