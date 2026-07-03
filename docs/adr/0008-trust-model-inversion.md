# 0008 — 信任模型倒转：UE 是唯一权威，WorldState 降级为带指纹校验的观测记录

**背景：** ADR 0004 的 Write-Through Cache 隐含假设"所有世界改动经过 Harness"。这个假设必然被打破——用户在编辑器里手动操作、编辑器自身行为（构建、自动保存）、其他插件的写入都不经过 Harness，L1 缓存对此完全盲视。原 Issue 013（将 WorldState 序列化到磁盘、启动时加载注入上下文）会把这种漂移固化为跨会话的错误事实。

2026-07-02 的方向评审（grill 讨论）确认了问题的准确表述：**失败模式不是"数据过时"，而是"过时数据冒充新鲜事实注入 LLM 上下文"**。过时但标注了来源和时效的观测仍然是有价值的线索（如同人类 TA 离开项目两周后脑中的场景记忆——他知道记忆过时，会先看一眼再动手）；冒充事实的过时数据才是毒药。

同次评审确认项目第一定位为**求职/作品集叙事**："做完"的标准是一个端到端闭环 demo + 有说服力的数字对比（见 Issue 014），而非功能清单的完整度。

**决策：**

1. **UE Editor 是世界状态的唯一权威源。** WorldState 从"权威镜像"降级为"带 provenance 的观测记录"——每条观测携带时间戳，整体快照携带关卡指纹。System Context 中的状态快照必须标注观测时间与指纹校验结果（"状态观测于 T，指纹匹配 / 已失配"）。

2. **引入 UE 侧指纹校验原语。** LevelPersistenceToolset（UE 侧插件，位于 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset`）提供五个工具：`GetLevelFingerprint` / `ListDirtyPackages` / `SaveCurrentLevel` / `SaveAsset` / `SaveAll`。指纹由 `文件 mtime + fileSizeBytes + actorCount + actorNameHash` 组成。Harness 在 Hard Boundary（首次连接、任务开始/结束、重连、LLM 显式请求）调用 fingerprint 比对：匹配 → 信任既有观测；失配 → 观测降级为历史记录，告知 LLM 世界在会话外被改动过，并触发 L3 刷新。

3. **Harness 外改动的主动检测。** Harness 记录自己引发的 dirty 包集合。若 `ListDirtyPackages`（过滤 `/Script/` 噪声包后）出现 Harness 未引发的 dirty 项，即为"发生了 Harness 外改动"的直接证据——此前被认为不可检测的信号，实际可检测。

4. **原 Issue 013 作废（文件已删除）。** 不再把 WorldState 磁盘快照作为权威恢复源。跨会话恢复依赖第 5 条的轨迹记忆 + 指纹校验。

5. **任务记忆（原 Issue 009，文件已删除并入本 ADR）重定义为"轨迹记忆"。** TaskMemory 记录的是任务意图、已完成/待完成步骤、错误、关键资产引用——这些是只有 Harness 知道、UE 侧不存在的信息，不因 Harness 外改动而失真。保留原 009 的核心设计：

   ```python
   class TaskMemory(BaseModel):
       task_id: str
       description: str          # "构建一个咖啡馆场景"
       completed: list[str]      # 已完成步骤
       pending: list[str]        # 待完成步骤（从 Skill YAML steps 初值化）
       current_step: str
       tool_call_count: int
       errors: list[str]
       key_assets: dict[str, str]
       level_fingerprint: dict   # 新增：持久化时的关卡指纹
   ```

   持久化时附带当时的关卡指纹；恢复时校验：指纹匹配 → 轨迹中的世界引用仍可信；失配 → 仅作为"曾做过什么"的历史注入，并提示 LLM 重新观测。原 009 记录的"对 005 的反馈"路径保留：完成的 TaskMemory + StateCacheInterceptor 的 write 调用序列 + Vision 验证结果，是 `save_skill` 自动生成 Skill YAML 的数据源。轨迹记忆的实现在 Issue 014 验收场景跑通后另立 Issue。

**已接受的限制：**

- `actorNameHash` 只能探测结构变化（Actor 增删、改名），探测不到 transform / 属性级修改。属性级漂移依赖 dirty-diff 与 mtime 间接捕获（改动未保存时体现为 dirty，已保存后体现为 mtime 变化）——三信号组合才是可用的漂移探测器。
- L1 write-through 保留（会话内即时性仍有价值），但其产物的语义从"事实"改为"观测"。

**后果：**

- Hard Boundary 触发条件（ADR 0004 定义的 4 种）新增第 5 种：**指纹失配**。
- L2 读回验证（ADR 0004 承诺、从未实现）成为正确性验证的主通道：写操作后重查实际值并与意图值 diff——确定性、零 Vision API 成本。Vision（ADR 0006）职责收窄为审美/整体效果判断。验证分工："灯是不是 15 度"用 L2 读回，"像不像黄昏"用截图。
- LevelPersistenceToolset 的输出消费者是 **Harness 内部校验逻辑 + System Context 中的一行漂移警告**，不作为常规工具集暴露给 LLM 自由调用（Save 类工具除外，可按 Skill 白名单暴露）。
- 检查点语义成为可能：任务开始时 fingerprint + 保存 = 检查点，验证失败可放弃未保存改动回退——"任务 = 事务"，作为 Issue 014 之后的候选方向。
- ADR 0004 的部分表述被本 ADR 修订，见其头部修订注记。
