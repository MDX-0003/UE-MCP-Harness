---
name: vision-interceptor-007
description: VisionInterceptor — 007 验证闭环的完整实现状态
metadata:
  type: project
---

# 007 Vision Interceptor — 验证闭环

## 实现文件
- **新建**: `harness/verification/interceptor.py` — `VisionInterceptor(ToolCallInterceptor)`
- **新建**: `tests/test_verification_interceptor.py` — 17 tests
- **修改**: `harness/state/models.py` — `WorldState.last_vision_verdict: dict | None`
- **修改**: `harness/context/prompt.py` — `_render_state_snapshot` 新增视觉验证反馈段落
- **修改**: `harness/server.py` — `build_server()` 新增 `skill_ref` 参数
- **修改**: `harness/cli.py` — 拦截器链注册 VisionInterceptor

## 数据流
```
LLM 调 CaptureAssetImage("") → Harness 透传 UE → base64 返回
→ VisionInterceptor.post_call 检测截图工具 → 提取 base64
→ 从活跃 Skill 提取 expected + tolerance → 调 VisionSubAgent.check()
→ 写入 WorldState.last_vision_verdict
→ LLM 调 get_context → 看到 "上次视觉验证: ✅/❌ ..."
```

## 验证状态
- `CaptureAssetImage("")` 通过 Harness 成功触发 Vision → ✅ PASS（1502×427 视口截图）
- `CaptureEditorImage` 成功触发 Vision → ✅ PASS（679KB 全窗口截图）
- get_context 正确显示视觉验证段落

## Skill YAML 修复（evening-lighting.yaml）
- Step 9: `CaptureAssetImage(assetPath="", bShowUI=false)` 替代 `CaptureEditorImage`
- Step 1,7: `find_actors` 加 `tag=""` 参数
- tools_allowlist: 移除 `SlateInspector.Screenshot`，新增 `SceneTools.add_to_scene_from_class`

Why: 007 是 MVP 闭环的核心——自动视觉验证让 LLM 能看到自己的修改效果。
How to apply: 激活 Skill 后 LLM 调截图工具 → VisionInterceptor 自动触发 → verdict 出现在 get_context。
