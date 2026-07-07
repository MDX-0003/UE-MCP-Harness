---
name: vision-model-config
description: MiMo Vision 模型配置详情——不要重设 max_tokens 为 1024，不要去掉 response_format
metadata:
  type: reference
---

# Vision 模型配置

## 当前配置
- **Model**: `mimo-v2.5-pro`（Xiaomi MiMo，带 vision 能力）
- **Base URL**: `https://token-plan-cn.xiaomimimo.com`
- **API 方式**: 通过 Anthropic SDK (`anthropic.Anthropic`) + `extra_body` 透传
- **JSON 模式**: `extra_body={"response_format": {"type": "json_object"}}` — MiMo 原生支持，强制 JSON 输出
- **max_tokens**: `4096` — **不要降回 1024 或 2048**。新结构化格式 (answer + confidence + caveats + observations[4]) 需要 2000-3500 tokens。4096 经过 5e295893 会话验证够用。
- **stop_reason 检测**: 已就位——`max_tokens` 截断时 logger.warning

## System prompt
- 已精简为 3 行角色定义 (`VISION_SYSTEM_PROMPT`)
- JSON schema 定义放在 `_VISION_FORMAT_REMINDER`，追加到每条 user message 末尾
- 原因: 带图片时 VLM 容易忽略 system prompt，user message 末尾指令服从率更高

## 已知行为
- MiMo 支持 `response_format` 但不保证 JSON 一定完整——max_tokens 截断时仍会碎裂
- `vision_ask`（纯文本追问）比 `vision_screenshot`（带图片）更稳定地遵循 JSON 格式
- 模型用 `stop_reason` 字段报告截断——已接入检测

Why: 多次因 max_tokens 不足导致响应截断。此配置是经过三轮调优的结果。
How to apply: 不要因为"省 token"而降 max_tokens。Vision 验证质量比 token 成本重要。
