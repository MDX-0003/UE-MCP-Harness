# 009 — 任务记忆：长任务结构化压缩

**类型：** AFK（无需人工交互，可独立完成并验证）

## 要构建什么

当任务 tool call 超过 20 步时，Harness 将原始对话历史压缩为结构化 `TaskMemory` JSON，注入 LLM context 替代不断增长的历史消息。

`TaskMemory` 追踪：任务描述、已完成步骤、待完成步骤、当前步骤、tool call 计数、错误列表、关键资产引用。从 Skill YAML 的 `steps` 列表初值化，随着 LLM 的执行逐步更新。

## 验收标准

- [ ] 任务开始时，`TaskMemory` 从匹配的 Skill `steps` 初始化 `pending` 列表
- [ ] 每完成一个步骤（Verification PASS 或 LLM 宣布完成），`TaskMemory.completed` 追加该步骤
- [ ] `current_step` 反映 `pending[0]`（如果 LLM 按顺序执行）或 LLM 当前正在处理的步骤
- [ ] 30+ 步任务后，注入 LLM context 的是结构化的 `TaskMemory` JSON（~500 tokens）而非原始 30 轮对话历史（~10k+ tokens）
- [ ] `errors` 列表记录失败步骤和错误原因
- [ ] `key_assets` 字典记录任务中创建/修改的关键资产路径
- [ ] Agent Session 结束时，`TaskMemory` 保存到磁盘 → 下次会话可恢复
- [ ] 压缩不影响 LLM 对任务进度的理解——可以用一个 30 步测试任务验证

## 阻塞

- #002（工具透传——需要 tool call 拦截）

## 设计说明

`TaskMemory` 数据模型：
```python
class TaskMemory(BaseModel):
    task_id: str
    description: str          # "构建一个咖啡馆场景"
    completed: list[str]      # ["地板放置", "墙体建造", "桌子布置"]
    pending: list[str]        # ["灯光设置", "装饰摆放"]
    current_step: str         # "正在调整 DirectionalLight 角度"
    tool_call_count: int      # 42
    errors: list[str]         # ["生成椅子资产失败: 资产未找到"]
    key_assets: dict[str, str]  # {"floor_material": "/Game/Materials/M_WoodFloor"}
```

与 Skill 的 `steps` 字段的关系：Skill `steps` 是初始模板——`pending` 从中初值化。LLM 可以在执行中动态调整步骤（拆分、跳过、新增），此时 `TaskMemory` 反映实际执行轨迹而非 Skill 原始模板。
