---
name: config-and-paths
description: Harness 配置路径约定——log_dir、session_id、env 加载
metadata:
  type: project
---

# 配置路径约定

## log_dir 默认值
- **已改为** `{repo_root}/.ue-harness/logs`（不再使用 `~/.ue-harness/logs`）
- 实现：`harness/config.py` 中 `_default_log_dir()` 使用 `Path(__file__).resolve().parent.parent / ".ue-harness" / "logs"`
- 也可通过 `.env` 的 `HARNESS_LOG_DIR` 或环境变量覆盖

## session_id 使用约定
- JSONL 文件名 = SnapshotRecorder 目录名 = 完整的 UE session_id（32 位 hex）
- `ToolCallLogger` 和 `SnapshotRecorder` 在 UE 连接**之后**创建，确保使用真实 session_id
- 不要使用短 UUID（8 位）作为文件名

## .env 约定
- 项目根目录 `.env` 可覆盖配置（`_load_dotenv()` 自动读取）
- 环境变量优先级：`.env` < 系统环境变量 < CLI 参数
- `.env` 已加入 `.gitignore`

## 其他约定
- Skill 目录: `~/.ue-harness/skills/`（跨项目共享，未改为项目路径）
- 项目记忆目录: `{repo}/.claude/memory/`（非 C 盘）

Why: 日志和快照应留在仓库内便于调试和归档；避免 C 盘膨胀。
How to apply: 无需额外配置。`harness start` 自动使用 repo 下的 `.ue-harness/logs/`。
