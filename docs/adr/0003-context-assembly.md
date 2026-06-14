# 0003 — Harness 控制上下文组装，而非 LLM 或 UE

**背景：** 直接 LLM→UE MCP 连接会给 LLM 完整的 ~157 工具列表，没有 State Cache 快照，也没有 Skill 注入。这浪费上下文，迫使每轮重新扫描。Harness 必须拥有"什么进入 LLM 上下文、以什么顺序、以什么粒度"的决策权。

**决策：** Harness 实现三层上下文组装管线。LLM 永远看不到原始的 UE 工具列表。

**Tier 1 — System Context（始终存在，约 500 tokens）：**
```
你是一个运行在 Unreal Engine 5.8 中的 UE Editor Agent。
你可以使用工具来控制 Unreal Editor。
尽量使用截图验证你的修改。

当前 UE 状态：
- 地图：/Game/Maps/Main
- PIE：已停止
- 选中：Actor_5
- 场景 Actor 数：48
- Build 状态：已构建

会话：coffee-shop-001 | 步骤：4/12 | 工具调用：23
```
来源：Harness 系统 prompt 模板 + State Cache 快照。

**Tier 2 — Task Context（Skill 匹配时注入，约 200-800 tokens）：**
```
任务：构建咖啡馆场景
Skill：coffee-shop-construction

已完成：
- 地板放置 ✓
- 墙体建造 ✓
- 桌子布置 ✓

下一步：灯光设置
步骤：
1. 找到场景中所有灯光
2. 在桌子旁添加暖色 PointLight
3. 调整 DirectionalLight 模拟窗户光
4. 截图验证温暖、宜人的氛围

此步骤可用工具：SceneTools.*, ActorTools.*, ObjectTools.*, SlateInspector.Screenshot
```
来源：Harness Skill YAML + 压缩后的 Task Memory。

**Tier 3 — Tool Reference（按需加载，约 200-500 tokens/toolset）：**
仅在 LLM 即将首次使用某工具集的工具时，通过 `describe_toolset` 从 UE 获取。Harness 内存中缓存整个 session。每轮不重发。

**设计后果：**
- Harness 必须在 Tier 3 加载之前解析 UE 工具列表以了解工具→工具集的映射。
- 初始 `tools/list` 由 Harness 在连接时内部调用。LLM 永远看不到完整的 157 工具列表——仅看到当前 Skill 的 `tools_allowlist` 暴露的工具。
- "自由探索"模式（无 Skill 匹配）下，Harness 暴露精选默认工具集（`SceneTools.*`, `ActorTools.*`, `ObjectTools.*`, `EditorAppToolset.*`, `SlateInspector.Screenshot`）——约 20 个工具，而非 157 个。
- `list_toolsets` 和 `describe_toolset` 保持可用，作为 LLM 显式请求更多工具的逃生通道。
