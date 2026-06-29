---
name: ue-screenshot-tools
description: UE 截图工具的两种路径及其在当前环境的表现
metadata:
  type: project
---

# UE 截图工具 — 两种路径

## CaptureEditorImage（Slate 窗口截图）
- `UEditorAppToolset::CaptureEditorImage()` — `EditorAppToolset.cpp:768`
- 遍历所有可见 Slate 窗口，逐个调 `FSlateApplication::TakeScreenshot()`
- `TakeScreenshot` → `SlateRHIRenderer::PrepareToTakeScreenshot` 查找 `WindowToViewportInfo`
- 最终依赖 GPU RHI `ReadSurfaceData` 读回像素
- **状态**: 不稳定——有时返回 679KB PNG，有时 "Failed to capture any editor windows"。依赖 UE 窗口可见状态和 GPU readback。

## CaptureAssetImage("")（视口渲染截图）
- `UEditorAppToolset::CaptureAssetImage("", bShowUI)` — `EditorAppToolset.cpp:382`
- 空 AssetPath 走 `FViewportScreenshotCapture::Start(bShowUI)`
- 使用 `FScreenshotRequest::RequestScreenshot`（UE 内置视口截图系统）
- 异步操作，需要 18-30 秒完成
- **状态**: 可靠工作（已验证 1502×427 截图成功）

## VisionInterceptor 截图检测
- `_is_screenshot_tool()` 关键词: `captureeditorimage`, `captureassetimage`, `screenshot`
- `_extract_image_base64()` 支持三种格式: MCP image block / 嵌套 returnValue / Data URI

## 当前环境限制
- 远程桌面/headless 下 `CaptureEditorImage` 可能因 GPU readback 失败
- `CaptureAssetImage("")` 更可靠，推荐作为 Skill 中的截图工具

Why: 两个截图路径的区别决定了 Vision 闭环能否触发的根本原因。
How to apply: Skill YAML 步骤用 `CaptureAssetImage(assetPath="", bShowUI=false)`。测试时确保 `sse_read_timeout >= 120`。
