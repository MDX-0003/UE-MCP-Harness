# Harness Bug 分析报告（基准：最新会话 eb83e070，07-06 14:25–14:28）

先确认基准：logs 下 5 个会话目录中，非空且最新的正是被评分的 `eb83e0704628f13e2f95309c9023a16c`（14:25–14:28），所以上一轮讨论的基准无需更换。另外两个 0 字节的 `tool_calls.jsonl`（11:14 的 e8410cd1、14:55 的 23b43d47）是 Harness 启动后没有任何工具调用的空会话，前者有正常收尾的 session.json，后者没有——应该只是重启后闲置，不是日志系统坏了。

我把 JSONL、两份 verdict.json、Session 归档、两张截图原图和相关源码全部对证了一遍，**找到 7 组确凿的 bug，且它们串成了一条因果链**。先给结论：这次会话里 agent 的行为其实相当规范（两次 L2 读回、问题写得具体、及时关 Session），是周边的状态追踪、verdict 管道和日志系统在各个环节掉了链子。

------

## P0-1 StateCache 全链路失效：参数模式与真实工具对不上

这是最系统性的一个。[interceptor.py:115](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/state/interceptor.py#L115)、[interceptor.py:126-127](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/state/interceptor.py#L126-L127) 等全部 12 个 handler 都按 `args["actor"]["name"]` 取 Actor 名，但真实调用参数是：

- `set_actor_transform` → `{"actor": {"refPath": "/Game/....SpotLight_0"}}`（有 `refPath` 无 `name`）
- `set_properties` → 键名是 **`instance`** 不是 `actor`，属性 JSON 的键名是 **`values`** 不是 `json`（见 JSONL 第 10 行）

后果是级联的：

- `actor_name` 永远取到空串 → `dirty_actors` **全程为空**、transform/properties 从不进缓存——归档里 `auto:dirty: 0, auto:write: 0` 就是证据；
- [session.py:117-151](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/session.py#L117-L151) 的 `_format_write_description` 同样按 `actor`/`json` 键取值，所以 recent-writes 缓冲里记的是 `set_properties(?, [])` 这种废话——就算注入给 vision 也没有信息量。

注意这和上一轮说的评分器 bug 是**同一个根因家族**：branch_mark 和 StateCache 是两套各自手写的参数解析，都对着一个不存在的参数模式写的。建议做一个共享的参数归一化助手（提取 refPath 尾段、组件路径归属到 owner actor），interceptor、session、branch_mark 三处共用，一次修完。

## P0-2 `class_name` 在全代码库中从未被赋值 → "Unknown×16" 是必然

我 grep 了整个 `harness/`，**没有任何代码给 `ActorSnapshot.class_name` 赋值**。[refresher.py:53](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/state/refresher.py#L53) 的 L3 全量刷新只存名字；agent 调 `get_class`、`get_label`、`get_actor_transform` 的**读取结果**没有任何 handler 捕获回缓存。所以 [session.py:293](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/session.py#L293) 的 `snap.class_name or "Unknown"` 对任何 actor 都输出 Unknown——vision 抱怨的 "Unknown×16" 不是偶发，是结构性保证。

顺带：[refresher.py:46-48](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/state/refresher.py#L46-L48) 调 `find_actors` 传的是 `{"glob": "*"}`，而真实工具签名是 `actor_type`/`tag`（见 JSONL 第 18 行），且 `_parse_actor_list` 对 `{"returnValue":[{"refPath":...}]}` 格式也解析不出名字——L3 刷新这条路本身也是对着旧接口写的。**读路径缓存（get_class/get_label/get_actor_transform 结果回填）是待办清单里 "L2 读回自动注入" 的前置条件，建议一起做。**

## P0-3 提问模式 verdict 硬编码 `pass=True`

[vision_agent.py:384-389](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/vision_agent.py#L384-L389)：`_parse_verdict(verify_mode=False)` **无条件返回 `pass_=True`**——提问模式是自由文本回答，根本没有判定环节。于是出现了这次最扎眼的矛盾：verdict 正文明确说"灯光颜色**不是**红色/橙色"，而 `pass: true`，LLM 在工具返回里看到的是 "`[Vision 分析] ✅ PASS`" + 一段说验证失败的文字（拼接逻辑在 [server.py:516-524](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/server.py#L516-L524)）。

这直接解释了 agent 的行为：它收到 ✅ 徽章后确实做了一次轻量反应（06:27:18 重读了 lightColor 等五个属性、确认 L2 值正确），然后就转去做下一个修改了——**系统亲口告诉它 PASS，它没有理由启动修正闭环**。修法：提问模式也要求结构化输出（`{answer, verdict: yes/no/uncertain, evidence}`），或至少把徽章从 ✅/❌ 改成中性的 "ℹ️ 回答"，把判定权留给主 agent。

## P1-4 verdict 正文被 max_tokens 掐断

第二份 verdict 只有 0.2KB，正文停在"这表明它可能"——典型的 token 耗尽。[vision_agent.py:326](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/vision_agent.py#L326) 写死 `max_tokens=1024`，而 [vision_agent.py:331-352](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/vision_agent.py#L331-L352) 在专门跳过 thinking block——说明走的代理模型可能开着 extended thinking，思考消耗掉大部分配额后正文被截断。第一份 verdict 完整（约 550 字）、第二份只剩 45 字，正符合"思考长短不定"的特征。修法：提高 max_tokens、对 vision 调用显式关闭 thinking，并检查 `response.stop_reason == "max_tokens"` 时重试或在返回里标注"回答被截断"。

## P1-5 JSONL 记录失真：off-by-one + 关键内容缺失

拦截器注册顺序（[cli.py:218-225](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/cli.py#L218-L225)）：`tool_logger` 排第 2，`vision_interceptor` 排第 5。而 logger 的 verdict 来源是 `get_verdict=lambda: _cache.last_vision_verdict`（[cli.py:203](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/cli.py#L203)）——**记录第 N 次调用时读到的是第 N-1 次的 verdict**。证据链完整：JSONL 第 14 行（第一次截图）无 verdict；第 29 行（第二次截图）挂着第一问的 verdict；第二问的 verdict 从未进入 JSONL。`screenshot: null` 是同样的机制（snapshotter 排在 logger 之后）。

更隐蔽的失真：`vision_screenshot` 的 JSONL `output` 只有 "Screenshot 已获取"、`ms: 515`——这是拦截链跑之前的文本和耗时；LLM 实际收到的是含完整 verdict 的拼接文本，vision API 的约 11 秒耗时也没记。**branch-mark 和人工复盘都在消费这份失真数据**。修法参考其他 vision_* 工具：在 [server.py:540](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/server.py#L540) 返回前用 `_log_harness_call` 记录最终 result_text，vision_screenshot 不走通用 logger。

## P1-6 Session 归档统计三处失真

- `question_count: 3` 实为 2：[session.py:504](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/session.py#L504) 在 `start()` 里就调了一次 `touch()`，创建即计一问；
- `context_sources` 恒为零：归档统计的是 `session.context_blocks`（[session.py:549-554](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/harness/verification/session.py#L549-L554)），但 `build_full_prompt_context` 是动态拼字符串、**从不 append ContextBlock**（只有 `vision_tell` 会）——所以 `auto:*` 计数器在代码上不可能非零，`status_text` 的"自动注入上下文"一栏也永远不显示；
- `session.json` 的 `tool_call_count: 29` vs JSONL 30 行，口径差一。

这组是纯记账问题，不影响运行，但会误导你判断"上下文注入到底有没有生效"——这次它显示 0，而实际上确实注入了（只是内容是 Unknown×16 的垃圾），两个错误叠加让归档完全不可信。

## P2-7 Vision 推理质量 + 相机 SOP 的缺口

看截图原图发现的问题：第一张图里聚光灯的锥形线框指向**画面右侧的空区域**，红光没有照到帧内任何可见表面；vision 的判断依据是"锥形线框是青色所以灯不是红色"——**线框是编辑器的 attenuation 可视化，颜色不代表灯光颜色**，这个推理从根上就错了。两层修复：

1. `VISION_SYSTEM_PROMPT_QUESTION` 应加一条领域知识：判断灯光颜色/强度要看被照亮的表面，gizmo、线框、图标都是编辑器可视化元素；
2. 相机 SOP 缺口：验证光照修改时，`FocusOnActors(灯)` 恰好把相机怼在灯本身上，正确做法是对准**被照射的目标表面**。instructions 里的 4 角度预设应按"验证对象类型"分流（几何修改→对准该 Actor；光照修改→对准受光面）。

另外截图是 1280×401，高度被压得太小，建议给 `vision_max_size` 加高度下限或建议 agent 先调视口比例。

------

## 因果链复盘：37 分会话里到底发生了什么

把所有 bug 串起来，这次会话的真实剧本是：agent 正确地改了灯色 → L2 读回确认写入 → 带着具体问题截图 → **P0-1/P0-2 让注入的上下文变成 "Unknown×16" 的噪声** → vision 只能看图，且 **P2-7 让它用错误依据给出"不是红色"的答案** → **P0-3 给这个否定答案盖了 ✅ PASS 章** → agent 看到 PASS，做了次礼节性复读就转向下一个任务 → 第二问的答案又被 **P1-4** 截断 → 全程的真实交互被 **P1-5** 记歪，branch-mark 再用带 bug 的解析器打出 37.2 分。每一环单独看都是小问题，串起来就是"验证体系形同虚设"。

## 修复优先级建议

1. **共享参数归一化**（修 P0-1，同时顺手修掉 branch_mark 的同源 bug）——一处代码解决三处失效；
2. **结构化 verdict + 截断防护**（P0-3、P1-4）——让 vision 的回答变得可信、可判定，这是闭环行为的前提；
3. **JSONL 记录保真**（P1-5）——否则 branch-mark 的 A/B 对比建立在失真数据上；
4. **读路径缓存回填**（P0-2）——同时解锁待办的 auto:l2 注入和 class_name，让上下文注入真正产生价值；
5. 记账修正（P1-6）和 prompt/SOP 增强（P2-7）随后跟上。

修完 1–4 后建议用同一个任务重跑一次，这次的 JSONL 就能作为干净的 baseline——届时 vision 会拿到"lightColor 已读回为 (1, 0.1, 0.05)"的上下文，面对青色线框它应该给出"数据已写入、但当前视角看不到受光面，建议换角度"这类有价值的回答，而不是一个盖着 PASS 章的否定。