# 007 — 验证循环集成：完整闭环

**类型：** HITL（需要人工验证 Vision model 对审美任务的判断效果）

## 要构建什么

将 Vision Sub-Agent（#006）与 Skill 执行流程（#005）整合为完整的 Verification Loop。当活跃 Skill 定义了 `verification` 策略时，Harness 在每个符合策略的 tool call 后自动：

1. 获取截图
2. 发送给 Vision Sub-Agent 判断
3. 将判断结果注入 System Context（Tier 1）作为"上次操作的视觉反馈"
4. LLM 基于反馈决定：继续下一步 / 调整当前步骤 / 任务完成

这是 Harness 与 Coding Agent 本质差异的首次端到端展示。

## 验收标准

- [ ] 执行 `evening-lighting` Skill → 每个步骤后根据 `verification` 策略自动截图 + Vision 判断
- [ ] Vision 结果注入下轮 System Context：`"上一步的视觉验证：{pass: false, reason: '亮度过高...', adjustment: '降至 30%'}"`
- [ ] LLM 根据 Vision 反馈调整参数 → 重新截图 → Vision PASS → 进入下一步
- [ ] Vision PASS 后 Task Memory 中标记当前步骤完成 → `pending` 的第一个进入 `current_step`
- [ ] 连续 3 次 FAIL → 标记步骤失败 → 记录到 Task Memory errors → 询问用户是否继续
- [ ] 未定义 `verification` 的步骤跳过视觉验证（零额外开销）
- [ ] 手动测试 evening-lighting Skill：UE 编辑器默认场景 → 用户说"改成黄昏" → Harness + LLM 自动调整 → 最终截图看起来确实像黄昏（人工判断）

## 阻塞

- #005（Skill 系统——需要 Skill 定义 verification 策略）
- #006（Vision Pipeline——需要 Vision Sub-Agent 能力）

## 设计说明

这是 MVP 中唯一标为 HITL 的 Issue，因为 Vision model 对"看起来像黄昏"这种主观审美判断的可靠性需要人工验证。如果验证效果不佳，可能需要：
- 调整 verification prompt 模板（对 Vision model 的指令措辞）
- 加入定量维度（色温范围、亮度值范围）作为辅助判断
- 降低 tolerance 阈值
