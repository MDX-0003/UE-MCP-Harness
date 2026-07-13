# Plan 0706: Actor class_name 会话内补全（推断 + 惰性查询，不落盘）

对应 Bug_analysis_0706.md 的 P0-2。取代已废弃的 `docs/plans/class-name-persistence.md`（该版本含磁盘持久化层，评审否决，理由见下）。

## 起因链

```
ActorSnapshot.class_name 字段定义后从未被写入
  → build_scene_context 全局概览永远输出 "Unknown×N"
    → Vision prompt 丢失场景语义（不知道场景里有什么类型的物体）
      → Vision 判断质量下降（只能纯看图，无结构信息辅助）
```

根因不是"忘记写了"，而是写入时机设计缺失：class_name 不像 transform/properties 那样伴随 write tool 自然产生。本计划补上四条写入路径，全部限定在**会话内存**（WorldState），随 L3 刷新自然重建。

## 决策记录：为什么不做磁盘持久化

旧计划提出把 `{actor_name: class_name}` 按 map_path hash 写入 `~/.ue-harness/cache/` 跨会话复用，论证是"class_name 是不变元数据，不挑战 ADR 0008"。评审否决，四条理由，**后续会话请勿重提该方案**：

1. **免罪论证有洞**：不变的是"某个 actor 实例的 class"，但缓存的 key 是 name→class **映射**，而 name→actor 绑定是可变的——actor 删除后名字可被不同类复用；编辑器改 label 时常连内部 FName 一起改；两次会话之间用户可在编辑器里任意重构关卡。缓存的实际上是低频可变状态，这正是 ADR 0008 否决 Issue 013 的同一理由的小号版本。
2. **失效语义无解**：唯一可用的有效性 key 是关卡指纹，但指纹每次编辑都变——严格按指纹做 key 则缓存几乎每次会话失效（无收益）；按 map_path 做 key 则过时条目永远无法检测（不可信）。两头都站不住。
3. **收益趋近于零**：名称推断可覆盖绝大多数 actor（内部对象名几乎总是 `{ClassName}_{N}` 格式），磁盘层只服务推断 miss 的残集（改过名的 actor），全部收益 = 每次 Harness 重启后省 0–5 次亚秒级只读 `get_class`。而改过名的 actor 恰是 name→class 绑定最不稳定的——磁盘层专门服务的对象正是最不该缓存的对象。
4. **破坏持久化边界**：当前 Harness 不落盘任何世界状态衍生物（跨会话只有 JSONL 日志、session 归档、skills），这条不变量本身是 ADR 0008 的架构亮点。为省几次调用开第一个口子，工程和叙事上都是亏的。

若未来真实日志证明惰性查询成本可观（不太可能），正确方向是让 UE 侧 `find_actors` 返回携带 class 信息（一次调用拿全量），而不是 Harness 侧落盘。

## 范围

### 做什么（四条写入路径，全部内存态）

| # | 路径 | 时机 | UE 调用成本 |
|---|------|------|------------|
| 1 | 名称推断 | L3 全量刷新时 | 0 |
| 2 | L1 写穿透顺手填 | `add_to_scene_*` 拦截 | 0 |
| 3 | 读结果回填 | LLM 自己调 `get_class` 时捕获返回值 | 0（搭 LLM 已发起调用的便车） |
| 4 | 惰性 UE 查询 | Vision 上下文组装时，对 mentioned/dirty 中仍 miss 的 actor | 0–5 次/会话 |

### 不做什么

- 不做任何磁盘缓存（理由见上）
- 不新建 `class_name_cache.py`（推断函数放 `normalize.py`，符合"不加文件优先"红线）
- 不主动批量查询所有 Actor（维持惰性）
- 不改变 L3 全量刷新的性能特征（推断是纯字符串操作）

## 实施步骤

### Step 1: 名称推断函数 → `harness/state/normalize.py`

```python
def infer_class_name(actor_name: str) -> str | None:
    """从 UE 默认命名规则推断 class name。
    SpotLight_0 → "SpotLight"
    StaticMeshActor_7 → "StaticMeshActor"
    KeyLight → None  (无 _数字 后缀，无法推断)
    """
```

规则：去掉末尾 `_数字`，剩余部分需以大写字母开头。误判风险可接受——`MyActor_0` 推断为 `MyActor` 即便不是引擎类名，也远比 "Unknown" 有信息量。放 normalize.py 是因为它和 `_parse_ref_path` 同属"从 UE 命名约定提取语义"这一职责。

### Step 2: L3 刷新时推断 → `harness/state/refresher.py`

`full_refresh()` 第 2 步创建 `ActorSnapshot(name=name)` 处，对 `class_name is None` 的快照调 `infer_class_name` 填入。已有 class_name 的（重连场景下缓存复用的旧快照）不覆盖——UE 查询/读回填的结果比推断更权威。

### Step 3: L1 写穿透顺手填 → `harness/state/interceptor.py`

`_handle_add_to_scene` 利用已修好的 `NormalizedCall`：`norm.payload["actor_type"]` 就是 class（如 `"PointLight"`；`add_to_scene_from_asset` 时是 asset path，取尾段）。创建快照时直接填入。约 +3 行。

### Step 4: get_class 读结果回填 → `harness/state/interceptor.py`

StateCacheInterceptor 的 handler 表新增 `get_class`：LLM 主动查询某 actor 的 class 时，post_call 从 `parsed_text` 提取 `returnValue`（形如 `/Script/Engine.SpotLight` → 取尾段），写入对应快照的 `class_name`。这是 Bug 分析 P0-2 里"读路径缓存回填"的第一块，也为后续 auto:l2 注入趟路。注意遵守红线：post_call 只写缓存，不改结果，异常不阻断。

### Step 5: Vision 组装时惰性 UE 查询 → `harness/verification/session.py` + `harness/cli.py`

`build_scene_context` 是纯函数、无 ue_client——保持它纯。查询发生在它的调用方之前：

- `VisionSessionManager` 构造时新增可选参数 `class_name_resolver: Callable[[list[str]], Awaitable[None]] | None`，沿用 cli.py 里 `get_verdict=lambda: ...` 的回调注入模式；
- `cli.py` 组装时传入一个闭包：接收 miss 的 actor 名列表，逐个调 UE `get_class`（refPath 由 `cache.map_path` + name 拼出），结果写回 WorldState，单个失败静默跳过；
- `add_screenshot` / `ask` 在调 `build_full_prompt_context` 前，计算 mentioned ∪ dirty 中 `class_name is None` 的集合，非空则 await resolver。resolver 为 None（测试/未接线）时跳过，行为退化为纯推断。

只对 mentioned/dirty 触发，全量 actor 的概览行里推断 miss 的照旧显示 Unknown——那是 P2 recency 信息，不值得为它发查询。

### Step 6: 测试（并入现有文件，不新建）

- `tests/test_normalize.py`：`infer_class_name` 的推断规则（`SpotLight_0`、`CineCameraActor_3`、`KeyLight→None`、`lowercase_0→None`、无后缀）；
- `tests/test_state.py`：L3 刷新后快照带推断 class_name；已有值不被推断覆盖；`add_to_scene` 顺手填；`get_class` 读回填（mock parsed_text）；
- `tests/test_vision_session.py`：resolver 被调用且只收到 miss 集合；resolver 为 None 时不炸；resolver 抛异常不阻断上下文组装；`build_scene_context` 概览不再输出 `Unknown×N`（推断命中场景）。

## 涉及文件

| 文件 | 操作 | 行数估算 |
|------|------|---------|
| `harness/state/normalize.py` | 新增 `infer_class_name` | +15 |
| `harness/state/refresher.py` | `full_refresh` 填推断值 | +5 |
| `harness/state/interceptor.py` | `_handle_add_to_scene` 顺手填 + `get_class` 读回填 handler | +20 |
| `harness/verification/session.py` | resolver 参数 + miss 集合计算 | +25 |
| `harness/cli.py` | 注入 resolver 闭包 | +15 |
| `tests/test_normalize.py` 等三个现有测试文件 | 新增用例 | ~60 |

**零新文件。**

## 预期效果

```
打开含 16 个 Actor 的关卡，首次 Vision：
  → L3 刷新时名称推断命中 ~12 个
  → mentioned/dirty 里 miss 的（如改名的 KeyLight）触发 0–5 次 get_class
  → 概览从 "Unknown×16" 变为 "PointLight×3、SpotLight×2、StaticMeshActor×8、..."

同会话第二次 Vision：
  → 全部内存命中，0 次 UE 查询

重启 Harness：
  → 推断重新覆盖 ~12 个，惰性查询重付 0–5 次
  → 这就是不做持久化的全部代价，可接受
```

## 验收

1. `uv run pytest tests/ -v` 全绿；
2. 重跑 Bug_analysis_0706 的同一任务（改灯色 + vision 验证），JSONL 中 vision 上下文不再出现 `Unknown×16`，vision 对"灯是什么颜色"的回答能引用 SpotLight 语义。
