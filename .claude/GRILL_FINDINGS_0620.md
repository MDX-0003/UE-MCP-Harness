# Grill Findings — 2026-06-20

## 核心结论
项目**值得做完**。拓扑位置是 MCP 生态真空地带，双核功能（Context Assembly + Session Decoupling）有不可替代增量价值。完成度比预期高。

## P0 待修复（MVP 收尾）
- [ ] State Cache 磁盘持久化（`~/.ue-harness/sessions/{id}.json`）
- [ ] L3 刷新补全 Actor 属性获取（当前空壳）
- [ ] VisionInterceptor #007 落地
- [ ] Skill triggers 字段消费（注入 system prompt）
- [ ] 修复短名 fallback 误匹配（`endswith` → 精确匹配）

## 已知限制需文档化
- State Cache 一致性前提：仅 MCP 操作后准确，用户手动编辑器操作不感知
- MCP 协议版本：锁定 UE 5.8 + Claude Code 2025Q2+
- Vision 验证：延迟 2-5s/次，建议默认 final_only 模式

## 面试叙事
主线索：发现 10 个局限性 → 6 ADR → 分阶段实现 → 真实 Bug 修复
双支柱：Context Assembly + Session Decoupling
深度亮点：Vision Sub-Agent 独立上下文 + Write-Through Cache 拦截器链
诚实标注：State Cache 一致性边界、协议版本锁定
