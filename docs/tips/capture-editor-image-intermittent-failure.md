---
name: capture-editor-image-intermittent-failure
description: "CaptureEditorImage 偶发 'Failed to capture any editor windows' — Slate 子窗口无独立 viewport 导致全窗口截图失败"
metadata:
  type: reference
  severity: P1
  reproducibility: intermittent
  ue_version: 5.8
  date: 2026-06-21
---

# CaptureEditorImage 偶发失败：根因分析

## 现象

Harness 调用 `mode="editor"` 截图时偶发报错：

```
ValueError: UE 截图失败: Failed to capture any editor windows.
```

Harness 侧已有 editor→viewport fallback (`capturer.py:66-76`)，但测试脚本
`tool_verify_harness_vision.py:50-52` 将 editor 模式视为独立测试点。

## 错误链路

```
tests/tool_verify_harness_vision.py:50 → mode="editor"
  → harness/verification/capturer.py:61-63 → MCP call CaptureEditorImage
    → UE: EditorAppToolset.cpp:768 CaptureEditorImage()
      → SlateApplication.cpp:3773 GetAllVisibleWindowsOrdered() — 获取所有可见 Slate 窗口
      → 遍历每个窗口调用 TakeScreenshot()
      → 全部失败 → RaiseScriptError("Failed to capture any editor windows.")
        → capturer.py:137 parse_screenshot() 检测到 isError → ValueError
```

## 根因：Slate 子窗口无独立 Viewport

`CaptureEditorImage()` 的设计意图是合成所有可见编辑器窗口的截图。但它的实现存在
与 Slate 子窗口渲染架构的结构性不匹配。三处源码证据：

### 证据 1：无差别遍历包含临时子窗口

`CaptureEditorImage()` 通过 `GetAllVisibleWindowsOrdered()` 获取所有可见窗口，
**包括临时性子窗口**（弹出菜单、combo box 下拉、工具提示、右键菜单等），
然后对每一个都尝试独立截图：

> [../UE/UE_5.8/Engine/Plugins/Experimental/ToolsetRegistry/Source/ToolsetRegistry/Private/ToolsetRegistry/EditorAppToolset.cpp#L780-L802](../UE/UE_5.8/Engine/Plugins/Experimental/ToolsetRegistry/Source/ToolsetRegistry/Private/ToolsetRegistry/EditorAppToolset.cpp#L780-L802)

```cpp
FSlateApplication::Get().GetAllVisibleWindowsOrdered(Windows);

for (const TSharedRef<SWindow>& Window : Windows)
{
    FCapturedWindow& Entry = Captured.AddDefaulted_GetRef();
    if (!FSlateApplication::Get().TakeScreenshot(Window, Entry.Colors, Entry.Size) ||
        Entry.Colors.IsEmpty())
    {
        Captured.Pop();  // 任何一个窗口截图失败就丢弃
        continue;
    }
}
```

`GetAllVisibleWindowsOrdered` → `GetAllVisibleChildWindows` 递归返回所有可见且
非最小化的 Slate 窗口：

> [../UE/UE_5.8/Engine/Source/Runtime/Slate/Private/Framework/Application/SlateApplication.cpp#L3785-L3797](../UE/UE_5.8/Engine/Source/Runtime/Slate/Private/Framework/Application/SlateApplication.cpp#L3785-L3797)

```cpp
void FSlateApplication::GetAllVisibleChildWindows(TArray<TSharedRef<SWindow>>& OutWindows,
    TSharedRef<SWindow> CurrentWindow)
{
    if (CurrentWindow->IsVisible() && !CurrentWindow->IsWindowMinimized())
    {
        OutWindows.Add(CurrentWindow);
        for (child : CurrentWindow->GetChildWindows())
            GetAllVisibleChildWindows(OutWindows, child);  // 递归：弹窗、菜单都在其中
    }
}
```

### 证据 2：子窗口在 WindowToViewportInfo 中没有条目

`TakeScreenshot()` 流程中，`PrepareToTakeScreenshot` 通过 `WindowToViewportInfo`
查找窗口对应的渲染 viewport：

> [../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L1395-L1403](../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L1395-L1403)

```cpp
void FSlateRHIRenderer::PrepareToTakeScreenshot(const FIntRect& Rect,
    TArray<FColor>* OutColorData, SWindow* InScreenshotWindow)
{
    ScreenshotState.ViewportToCapture = WindowToViewportInfo.FindRef(InScreenshotWindow);
    //                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //                                  对子窗口返回 nullptr！
}
```

`WindowToViewportInfo` 由 `CreateViewport()` 填充，**仅顶级窗口**才会调用。
弹窗/菜单类子窗口共享父窗口的 framebuffer，不拥有独立 viewport：

> [../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L731-L747](../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L731-L747)

```cpp
void FSlateRHIRenderer::CreateViewport(const TSharedRef<SWindow> Window)
{
    if (WindowToViewportInfo.Contains(&Window.Get())) return;
    FlushRenderingCommands();
    // ...仅为顶级窗口创建 viewport
    WindowToViewportInfo.Add(&Window.Get(), new FSlateViewportInfo(...));
}
```

### 证据 3：Viewport 不匹配时 FlushRenderingCommands 被跳过

当 `ScreenshotState.ViewportToCapture == nullptr` 时，渲染循环中的匹配检查永远
为 false，`bScreenshotProcessed` 保持 false。最终 `FlushRenderingCommands()` 不被
调用 —— 渲染线程的 readback 命令未执行，`OutColorData` 保持为空：

> [../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L1702-L1747](../UE/UE_5.8/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderer.cpp#L1702-L1747)

```cpp
bool bScreenshotProcessed = false;
for (const FWindowToRender& WindowToRender : WindowsToRender)
{
    bScreenshotProcessed |= ScreenshotState.ViewportToCapture == WindowToRender.ViewportInfo;
    // 当 ViewportToCapture == nullptr 时，此条件永远为 false
}

// ...

if (bScreenshotProcessed)
{
    FlushRenderingCommands();  // ← 只有匹配时才 flush！
    ScreenshotState = {};
}
```

## 为什么是偶发的

正常情况：主编辑器窗口（有 viewport + WindowToViewportInfo 条目）的
`TakeScreenshot()` 成功 → `Captured` 非空 → 函数正常返回。

**偶发失败的触发条件**（所有窗口都失败，`Captured` 为空）：

1. **FlushRenderingCommands 内的消息泵导致窗口状态变化**（最可能）：
   `FlushRenderingCommands()` 内部会 pump Windows 消息。当某个子窗口的
   `TakeScreenshot` 被调用时，`PrivateDrawWindows` → `DrawWindows_Private`
   内部处理过程中，可能有弹窗/菜单被销毁（用户点击了其他地方、定时器触发等）。
   窗口树状态变化后，后续窗口的 `GeneratePathToWidgetChecked` 可能失败。

2. **DrawBuffer 状态污染**：一轮失败的 `TakeScreenshot`（子窗口无 viewport）
   未清理 `ScreenshotState`，且 draw buffer 被部分消费。下一轮
   `DrawPrepass(DrawOnlyThisWindow)` 可能在脏状态下运行。

3. **编辑器窗口全部不可渲染**的边界情况：
   - 桌面锁定 / UAC 弹窗 / 全屏独占应用在前台时，DWM 可能不合成编辑器窗口
   - `IsVisible() && !IsWindowMinimized()` 返回 true（Win32 API 级别的
     `IsWindowVisible` 只检查 `WS_VISIBLE` 样式位），但 D3D swap chain
     实际上不呈现内容

## 现有缓解措施

Harness `capturer.py:66-76` 已有 editor→viewport fallback：

```python
except Exception as e:
    log_exception(e, "capturer editor→viewport fallback")
    result = await ue_client.call_tool(
        "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
        {"AssetPath": "", "bShowUI": b_show_ui},
    )
    return parse_screenshot(result, max_width, max_height)
```

## 建议修复方向

### UE 侧（EditorAppToolset.cpp）

`CaptureEditorImage()` 应在遍历前过滤掉无独立 viewport 的子窗口：

```cpp
// 建议在遍历前添加过滤
Windows.RemoveAll([](const TSharedRef<SWindow>& W) {
    return !FSlateApplication::Get().GetRenderer()
        ->GetWindowToViewportInfo().Contains(&W.Get());
});
```

或更简单：只捕获顶级窗口，子窗口内容已在其父窗口画面中。

### Harness 侧

- `capturer.py` 的 fallback 机制已足够健壮
- 测试脚本可将 editor 模式失败降级为 WARNING 而非 ERROR

**Why:** Slate 的子窗口（ContextMenu/PopupMenu/Tooltip/ComboBox dropdown）是
`SWindow` 对象，存在于 `SlateWindows` 列表，`IsVisible()=true`，但它们**共享
父窗口的 viewport** 渲染——在 `WindowToViewportInfo` 中没有自己的条目。
`CaptureEditorImage` 对每个可见窗口独立调用 `TakeScreenshot` 会因
`ViewportToCapture=null` 而失败。当因消息泵/DrawBuffer 状态变化导致主窗口
也受影响时，`Captured` 变空，触发 ValueError。

**How to apply:** UE 侧修复为 P1（需改动 C++），Harness 侧已有 fallback 可暂时
接受。测试中遇到此错误时，重试 editor 模式 2-3 次通常可恢复。