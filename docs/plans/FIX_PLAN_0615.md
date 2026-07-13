# 004 + 005 + 008 修复计划 — 2026-06-15

来源文档：`TMP_HANDOFF_0615_L04_L05_L08.md`，19 个风险中的 13 个纳入修复。

## 全局决策

| 决策 | 结论 |
|------|------|
| MCP prompt 通道 | 新增 `get_context` Harness 自有 MCP 工具，LLM 按需主动调 |
| YAML 解析器 | 引入 pyyaml 替换自定义解析器 |
| load_level → L3 刷新 | 同步触发——`_needs_refresh` 标志 → post 阶段直接调 `full_refresh()` |

---

## Step 1: MCP prompt 通道 + 激活/取消 — ✅ 完成

**目标**：让组装后的 system prompt 到达 LLM，提供 Skill 模式退出机制。

**涉及的修复**：R1, R6, R9, R15, R17

| 改动 | 文件 | 状态 |
|------|------|:---:|
| 新增 `get_context` MCP 工具 → 返回 `assemble_system_prompt(...)` | `server.py` | ✅ |
| 新增 `deactivate_skill` MCP 工具 → 清除 `_active_skill` | `server.py` | ✅ |
| `build_server()` 接受 `world_state` 参数 | `server.py` | ✅ |
| `create_app()` + `serve()` 传入初始化 instructions | `transport.py` | ✅ |
| `cmd_start` 生成 instructions（Skill 列表 + 核心工具提示） | `cli.py` | ✅ |
| `list_tools` 追加 `get_context` + `deactivate_skill` | `server.py` | ✅ |

**测试结果**：135 passed, 0 failed

---

## Step 2: Handler 路由校验 + load_level → L3 — ✅ 完成

**涉及的修复**：R10, R12

| 改动 | 文件 | 状态 |
|------|------|:---:|
| `post_call` 路由增加 short_name fallback 匹配 | `state/interceptor.py` | ✅ |
| `WorldState` 新增 `_needs_refresh: bool` | `state/models.py` | ✅ |
| `_handle_load_level` 设置 `_needs_refresh = True` | `state/interceptor.py` | ✅ |
| `call_tool` post 阶段检测 → 触发 `full_refresh` | `server.py` | ✅ |

---

## Step 3: pyyaml + 工具缓存 + 重复名检查 — ✅ 完成

**涉及的修复**：R2, R5, R7

| 改动 | 文件 | 状态 |
|------|------|:---:|
| 用 pyyaml 替换自定义 YAML 解析器 | `skill_registry.py` | ✅ |
| 更新测试覆盖 pyyaml 边界（含冒号字符串、嵌套结构） | `test_skill.py` | ✅ |
| `_parse_skill_yaml_to_dict` 改用 pyyaml | `server.py` | ✅ |
| `_rebuild_tool_reference` 加缓存复用 | `server.py` | ✅ |
| `save_skill` 加重复名检查 → `overwrite=true` 覆盖 | `server.py` | ✅ |

---

## Step 4: 占位文本更新 + reload — ✅ 完成

**涉及的修复**：R3, R8

| 改动 | 文件 | 状态 |
|------|------|:---:|
| `_render_state_snapshot` 删除 "#008 占位"→ "缓存未初始化" | `prompt.py` | ✅ |
| 新增 `last_full_refresh` 时间戳提示（秒/分钟/小时前） | `prompt.py` | ✅ |
| `SkillRegistry.reload()` 公开方法 | `skill_registry.py` | ✅ |
| `activate_skill("")` 空查询 → 触发 reload | `server.py` | ✅ |

---

## 执行日志

| 时间 | Step | 结果 |
|------|:---:|---|
| 2026-06-15 | 1 | ✅ 135 passed |
| 2026-06-15 | 2 | ✅ 135 passed |
| 2026-06-15 | 3 | ✅ 137 passed |
| 2026-06-15 | 4 | ✅ 137 passed |

**全部 13 个风险已修复，最终 137 tests, 0 failed。**
