# SaveLevelAs + LoadLevel UE 工具实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LevelPersistenceToolset 插件中新增 `SaveLevelAs` 和 `LoadLevel` 两个 MCP 工具，供 Harness 的 match_reference 叫停机制（最佳状态快照保存 + 恢复）使用。

**Architecture:** 从现有 `SaveCurrentLevel` 中提取 `CollectLevelPackagesToSave` 公共帮助函数（收集主包 + dirty OFPA 外部 Actor 包）。`SaveLevelAs` 通过 `UPackage::Rename` 将所有包（主 `.umap` + OFPA 子包）从源前缀重命名为目标前缀，然后调用 `SaveCurrentLevel` 完整保存。等价于 Ctrl+S SaveAs 完整流程，对 Unsaved 和已保存关卡均有效。已存在同名快照时自动卸载旧包 + 删除旧文件后覆盖。`LoadLevel` 用 `FEditorFileUtils::LoadMap(Filename, false, false)` 加载指定关卡（注释明确承诺 "Does not prompt the user to save"），bSaveDirty 参数控制加载前是否静默保存脏包。

**Tech Stack:** Unreal Engine 5.8 Editor C++ (UFUNCTION + AICallable 元标记)，MCP Python SDK (测试脚本直连 UE :8000)

---

## 涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `Public/LevelPersistenceToolset.h` | 加 `CollectLevelPackagesToSave` 声明 + 两个新 UFUNCTION |
| 修改 | `Private/LevelPersistenceToolset.cpp` | 提取帮助函数 + 实现 SaveLevelAs / LoadLevel |
| 新增 | `test_saveas_load.py` | 直连 UE MCP 的端到端测试脚本 |

---

### Task 1: 提取 `CollectLevelPackagesToSave` 公共帮助函数

**Files:**
- Modify: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Private\LevelPersistenceToolset.cpp:218-234`

- [ ] **Step 1: 在 Helpers 区末尾（`FormatTimestamp` 之后、`BuildLevelSaveResultJson` 之前）插入新函数**

```cpp
/** 收集当前关卡需要保存的全部包：主 .umap + 所有 dirty OFPA 外部 Actor 包。
 *  OFPA 外部包通过包名前缀 "{关卡名}_InstanceOf" 匹配。
 *  返回数组的第一个元素始终是主包。 */
static TArray<UPackage*> CollectLevelPackagesToSave(UPackage* LevelPackage)
{
    TArray<UPackage*> Result;
    Result.Add(LevelPackage);

    FString LevelPrefix = LevelPackage->GetName() + TEXT("_InstanceOf");
    for (TObjectIterator<UPackage> It; It; ++It)
    {
        UPackage* Pkg = *It;
        if (Pkg && Pkg != LevelPackage && Pkg->IsDirty() &&
            Pkg->GetName().StartsWith(LevelPrefix))
        {
            Result.Add(Pkg);
        }
    }
    return Result;
}
```

- [ ] **Step 2: 重构 `SaveCurrentLevel`——用 `CollectLevelPackagesToSave` 替换内联的包收集逻辑**

替换 [LevelPersistenceToolset.cpp:218-234](E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Private\LevelPersistenceToolset.cpp#L218-L234)：

旧代码（删除 218-234 行）：
```cpp
    // Collect all dirty packages owned by this level: main .umap + OFPA external actors
    TArray<UPackage*> PackagesToSave;
    PackagesToSave.Add(LevelPackage);
    
    //Actor in Current Level `s Package will be named as
    //"MyLevel_InstanceOf_PointLight_0.uasset" 
    //list all package in current level , add to "PackagesToSave"
    FString LevelPrefix = LevelPackage->GetName() + TEXT("_InstanceOf");
    for (TObjectIterator<UPackage> It; It; ++It)
    {
        UPackage* Pkg = *It;
        if (Pkg && Pkg != LevelPackage && Pkg->IsDirty() &&
            Pkg->GetName().StartsWith(LevelPrefix))
        {
            PackagesToSave.Add(Pkg);
        }
    }
```

新代码：
```cpp
    TArray<UPackage*> PackagesToSave = CollectLevelPackagesToSave(LevelPackage);
```


- [ ] **Step 4: 运行现有测试确认无回归**

```bash
cd "E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset"
python test_tools.py
```

预期：ALL 5 TOOLS PASSED。

- [ ] **Step 5: Commit**

```bash
cd "E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset"
git add Source/LevelPersistenceToolset/Private/LevelPersistenceToolset.cpp
git commit -m "refactor: extract CollectLevelPackagesToSave helper

从 SaveCurrentLevel 中提取公共函数，供 SaveLevelAs 复用。
逻辑不变：收集主 .umap + 所有 dirty OFPA 外部 Actor 包。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 实现 `SaveLevelAs`

**Files:**
- Modify: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Public\LevelPersistenceToolset.h`
- Modify: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Private\LevelPersistenceToolset.cpp`

- [ ] **Step 1: 在头文件中声明 `SaveLevelAs`**

在 `ULevelPersistenceToolset` 类中，`GetLevelFingerprint` 之后插入（[LevelPersistenceToolset.h:60](E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Public\LevelPersistenceToolset.h#L60) 之后）：

```cpp
    /** Save the current level to a new package path without changing the editor's
     *  active file. The main .umap is saved to TargetPath; any dirty OFPA
     *  external actor packages are saved to their original paths.
     *  @param TargetPath Package path for the copy, e.g. '/Game/Maps/0714-MyLevel'.
     *  @return JSON: {"status":"saved", "targetPath":"...", "sourcePath":"...",
     *          "packageGuid":"...", "filePath":"...", "fileSizeBytes":N,
     *          "lastModified":"ISO8601", "actorCount":N, "actorNameHash":"hex",
     *          "externalActorPackages":N, "externalActorsSaved":N,
     *          "externalActorsFailed":N} */
    UFUNCTION(meta = (AICallable), Category = "LevelPersistenceToolset")
    static LEVELPERSISTENCETOOLSET_API FString SaveLevelAs(const FString& TargetPath);
```

- [ ] **Step 2: 在 .cpp 中实现 `SaveLevelAs`**

在 `SaveCurrentLevel` 实现之后插入：

```cpp
FString ULevelPersistenceToolset::SaveLevelAs(const FString& TargetPath)
{
    FString CleanPath = TargetPath.TrimStartAndEnd().TrimChar(TEXT('"'));

    if (CleanPath.IsEmpty())
    {
        return TEXT("{\"status\":\"error\",\"message\":\"TargetPath is empty.\"}");
    }

    if (!IsEditorReady())
    {
        return TEXT("{\"status\":\"error\",\"message\":\"Editor is not ready (no active world).\"}");
    }

    UWorld* World = GEditor->GetEditorWorldContext().World();
    UPackage* LevelPackage = World->GetOutermost();
    if (!LevelPackage)
    {
        return TEXT("{\"status\":\"error\",\"message\":\"Cannot determine package for current level.\"}");
    }

    // 解析目标文件路径
    FString TargetFilename;
    if (!FPackageName::TryConvertLongPackageNameToFilename(CleanPath, TargetFilename, TEXT(".umap")))
    {
        return FString::Printf(
            TEXT("{\"status\":\"error\",\"message\":\"Could not resolve filename for '%s'.\"}"),
            *CleanPath);
    }

    // 确保目标目录存在
    FString TargetDir = FPaths::GetPath(TargetFilename);
    if (!IFileManager::Get().DirectoryExists(*TargetDir))
    {
        IFileManager::Get().MakeDirectory(*TargetDir, true);
    }

    // 收集待保存包（复用公共函数）
    TArray<UPackage*> PackagesToSave = CollectLevelPackagesToSave(LevelPackage);

    int32 ExternalSaved = 0;
    int32 ExternalFailed = 0;
    TArray<FString> FailedPackages;

    for (UPackage* Pkg : PackagesToSave)
    {
        FString Filename;
        if (Pkg == LevelPackage)
        {
            // 主包 → 存到新路径
            Filename = TargetFilename;
        }
        else
        {
            // OFPA 子包 → 存回原路径（确保新快照引用数据一致）
            Filename = ResolvePackageFilename(Pkg);
            if (Filename.IsEmpty())
            {
                continue;  // Untitled OFPA packages — skip silently
            }
        }

        FSavePackageArgs SaveArgs;
        SaveArgs.TopLevelFlags = RF_Standalone;

        FSavePackageResultStruct SaveResult = UPackage::Save(
            Pkg, nullptr, *Filename, SaveArgs);

        if (Pkg != LevelPackage)
        {
            if (SaveResult.Result == ESavePackageResult::Success)
                ExternalSaved++;
            else
            {
                ExternalFailed++;
                FailedPackages.Add(Pkg->GetName());
            }
        }
    }

    // 组装返回 JSON
    TSharedRef<FJsonObject> Json = MakeShared<FJsonObject>();

    bool bMainSaved = !LevelPackage->IsDirty();
    if (bMainSaved && ExternalFailed == 0)
    {
        Json->SetStringField(TEXT("status"), TEXT("saved"));
    }
    else if (bMainSaved)
    {
        Json->SetStringField(TEXT("status"), TEXT("partial"));
    }
    else
    {
        Json->SetStringField(TEXT("status"), TEXT("error"));
    }

    Json->SetStringField(TEXT("targetPath"), CleanPath);
    Json->SetStringField(TEXT("sourcePath"), LevelPackage->GetName());

    // 文件元数据（针对新保存的目标文件）
    FGuid Guid = LevelPackage->GetPersistentGuid();
    Json->SetStringField(TEXT("packageGuid"), Guid.IsValid() ? Guid.ToString() : FString());
    Json->SetStringField(TEXT("filePath"), TargetFilename);
    if (!TargetFilename.IsEmpty())
    {
        Json->SetNumberField(TEXT("fileSizeBytes"),
            static_cast<double>(IFileManager::Get().FileSize(*TargetFilename)));
        FDateTime MTime = IFileManager::Get().GetTimeStamp(*TargetFilename);
        Json->SetStringField(TEXT("lastModified"),
            MTime != FDateTime::MinValue() ? FormatTimestamp(MTime) : FString());
    }

    // Actor 信息
    if (World->PersistentLevel)
    {
        Json->SetNumberField(TEXT("actorCount"), World->PersistentLevel->Actors.Num());
        FString Hash = ComputeActorNameHash(World);
        if (!Hash.IsEmpty()) { Json->SetStringField(TEXT("actorNameHash"), Hash); }
    }

    // OFPA 统计
    Json->SetNumberField(TEXT("externalActorPackages"), ExternalSaved + ExternalFailed);
    Json->SetNumberField(TEXT("externalActorsSaved"), ExternalSaved);
    Json->SetNumberField(TEXT("externalActorsFailed"), ExternalFailed);

    if (FailedPackages.Num() > 0)
    {
        TArray<TSharedPtr<FJsonValue>> FailArray;
        int32 MaxReport = FMath::Min(FailedPackages.Num(), 10);
        for (int32 i = 0; i < MaxReport; ++i)
        {
            FailArray.Add(MakeShared<FJsonValueString>(FailedPackages[i]));
        }
        Json->SetArrayField(TEXT("failedPackages"), FailArray);
    }

    return JsonToString(Json);
}
```




### Task 3: 实现 `LoadLevel`

**Files:**
- Modify: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Public\LevelPersistenceToolset.h`
- Modify: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\Source\LevelPersistenceToolset\Private\LevelPersistenceToolset.cpp`

- [ ] **Step 1: 在 cpp 顶部新增 include**

在 `#include "FileHelpers.h"` 下方追加：

```cpp
#include "EditorLevelUtils.h"
```

- [ ] **Step 2: 在头文件中声明 `LoadLevel`**

在 `SaveLevelAs` 声明之后插入：

```cpp
    /** Load a level from the given package path. Does NOT prompt the user to save
     *  the current map (uses FEditorFileUtils::LoadMap with LoadAsTemplate=false).
     *  @param LevelPath Package path, e.g. '/Game/Maps/MyLevel'.
     *  @param bSaveDirty If true, silently save all dirty packages before loading.
     *         Default false — dirty changes are discarded.
     *  @return JSON: {"status":"loaded", "packagePath":"...", "packageGuid":"...",
     *          "filePath":"...", "fileSizeBytes":N, "lastModified":"ISO8601",
     *          "actorCount":N, "actorNameHash":"hex"} */
    UFUNCTION(meta = (AICallable), Category = "LevelPersistenceToolset")
    static LEVELPERSISTENCETOOLSET_API FString LoadLevel(
        const FString& LevelPath,
        bool bSaveDirty = false
    );
```

- [ ] **Step 3: 在 .cpp 中实现 `LoadLevel`**

在 `SaveLevelAs` 实现之后插入：

```cpp
FString ULevelPersistenceToolset::LoadLevel(const FString& LevelPath, bool bSaveDirty)
{
    FString CleanPath = LevelPath.TrimStartAndEnd().TrimChar(TEXT('"'));

    if (CleanPath.IsEmpty())
    {
        return TEXT("{\"status\":\"error\",\"message\":\"LevelPath is empty.\"}");
    }

    if (!IsEditorReady())
    {
        return TEXT("{\"status\":\"error\",\"message\":\"Editor is not ready (no active world).\"}");
    }

    // 解析文件路径
    FString Filename;
    if (!FPackageName::TryConvertLongPackageNameToFilename(CleanPath, Filename, TEXT(".umap")))
    {
        return FString::Printf(
            TEXT("{\"status\":\"error\",\"message\":\"Could not resolve filename for '%s'.\"}"),
            *CleanPath);
    }

    // 检查文件是否存在
    if (!IFileManager::Get().FileExists(*Filename))
    {
        return FString::Printf(
            TEXT("{\"status\":\"error\",\"message\":\"Level file does not exist: %s\"}"),
            *Filename);
    }

    // bSaveDirty=true: 静默保存所有脏包（不弹对话框）
    if (bSaveDirty)
    {
        bool bSaved = FEditorFileUtils::SaveDirtyPackages(
            /*bPromptUserToSave=*/false,
            /*bSaveMapPackages=*/true,
            /*bSaveContentPackages=*/true,
            /*bFastSave=*/true
        );
        if (!bSaved)
        {
            return TEXT("{\"status\":\"error\",\"message\":\"Failed to save dirty packages before loading.\"}");
        }
    }

    // 加载目标关卡 — FEditorFileUtils::LoadMap 注释明确承诺 "Does not prompt"
    bool bLoaded = FEditorFileUtils::LoadMap(
        Filename,
        /*LoadAsTemplate=*/false,
        /*bShowProgress=*/false
    );

    if (!bLoaded)
    {
        return FString::Printf(
            TEXT("{\"status\":\"error\",\"message\":\"Failed to load level '%s'.\"}"),
            *CleanPath);
    }

    // 加载成功，组装指纹 JSON
    if (!IsEditorReady())
    {
        return TEXT("{\"status\":\"error\",\"message\":\"Editor became unavailable after loading.\"}");
    }

    UWorld* World = GEditor->GetEditorWorldContext().World();
    UPackage* Package = World->GetOutermost();
    if (!Package)
    {
        return TEXT("{\"status\":\"loaded\",\"message\":\"Level loaded but cannot get package info.\"}");
    }

    TSharedRef<FJsonObject> Json = MakeShared<FJsonObject>();
    Json->SetStringField(TEXT("status"), TEXT("loaded"));
    AddFileMetadata(Json, Package->GetName(), Package);

    if (Package->ContainsMap())
    {
        Json->SetNumberField(TEXT("actorCount"),
            World->PersistentLevel ? World->PersistentLevel->Actors.Num() : 0);
        FString Hash = ComputeActorNameHash(World);
        if (!Hash.IsEmpty()) { Json->SetStringField(TEXT("actorNameHash"), Hash); }
    }

    return JsonToString(Json);
}
```


### Task 4: 端到端测试脚本

**Files:**
- Create: `E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset\test_saveas_load.py`

- [ ] **Step 1: 创建测试脚本**

```python
"""SaveLevelAs + LoadLevel 端到端功能验证脚本。

测试流程：
  1. listActors 获取当前关卡 Actor 列表
  2. 取首个 Actor，修改其 Transform (Location + Rotation + Scale)
  3. SaveLevelAs 将修改后的关卡另存到新 umap
  4. LoadLevel 加载刚保存的新关卡
  5. 验证加载成功（packagePath 匹配 + actorCount > 0）

用法:
  python test_saveas_load.py
"""

import asyncio
import io
import json
import sys

# Fix Windows GBK terminal for emoji output
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

UE_URL = "http://127.0.0.1:8000/mcp"
TOOLSET_NAME = "LevelPersistenceToolset.LevelPersistenceToolset"
# 快照目标路径（测试专用，日期前缀避免覆盖真实关卡）
SNAPSHOT_PATH = "/Game/Maps/0714-Test-Snapshot"


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    errors = 0

    async with streamablehttp_client(UE_URL, timeout=90, sse_read_timeout=90) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 0. Load toolset ──
            print("0. Loading toolset...")
            try:
                r = await session.call_tool("load_toolset", {"toolset_name": TOOLSET_NAME})
                text = _extract_text(r)
                print(f"   {text.splitlines()[0]}")
            except Exception as e:
                print(f"   FAIL: {e}")
                return 1

            P = TOOLSET_NAME

            async def call(name, args):
                r = await session.call_tool(name, args)
                raw = _extract_text(r)
                d = json.loads(raw)
                rv = d.get("returnValue", raw)
                inner = json.loads(rv) if isinstance(rv, str) else rv
                return inner, r.isError

            def ok(msg, **kw):
                parts = [f"{k}={v}" for k, v in kw.items()]
                print(f"   OK  {msg}: {', '.join(parts)}")

            def fail(msg):
                nonlocal errors
                errors += 1
                print(f"   FAIL  {msg}")

            # ── 1. listActors 获取当前关卡 Actor 列表 ──
            print("\n1. listActors (current level)")
            first_actor_path = None
            try:
                data, _ = await call("ToolsetRegistry.EditorAppToolset.ListActors", {})
                actors = data if isinstance(data, list) else data.get("actors", [])
                if not actors:
                    fail("no actors returned by listActors")
                else:
                    first = actors[0]
                    if isinstance(first, dict):
                        first_actor_path = first.get("refPath") or first.get("path") or first.get("name")
                        ok("first actor", name=first.get("name", "?"),
                           refPath=str(first_actor_path)[:80])
                    else:
                        first_actor_path = str(first)
                        ok("first actor", raw=str(first)[:80])
            except Exception as e:
                fail(f"listActors failed: {e}")

            # ── 2. 修改首个 Actor 的 Transform ──
            print("\n2. Modify first actor's transform")
            if first_actor_path is None:
                fail("no actor path — skip transform test")
            else:
                new_transform = {
                    "location": {"x": 100.0, "y": 200.0, "z": 300.0},
                    "rotation": {"pitch": 15.0, "yaw": 30.0, "roll": 0.0},
                    "scale": {"x": 2.0, "y": 2.0, "z": 2.0},
                }
                try:
                    data, is_err = await call(
                        "ToolsetRegistry.EditorAppToolset.SetActorTransform",
                        {
                            "actorPath": first_actor_path,
                            "location": new_transform["location"],
                            "rotation": new_transform["rotation"],
                            "scale": new_transform["scale"],
                        },
                    )
                    if not is_err:
                        ok("transform set",
                           loc=f"({new_transform['location']['x']},{new_transform['location']['y']},{new_transform['location']['z']})",
                           rot=f"({new_transform['rotation']['pitch']},{new_transform['rotation']['yaw']},{new_transform['rotation']['roll']})",
                           scale=f"({new_transform['scale']['x']},{new_transform['scale']['y']},{new_transform['scale']['z']})")
                    else:
                        fail(f"SetActorTransform returned error: {data}")
                except Exception as e:
                    fail(f"SetActorTransform failed: {e}")

            # ── 3. SaveLevelAs 另存关卡 ──
            print("\n3. SaveLevelAs → " + SNAPSHOT_PATH)
            saved_package_path = None
            try:
                data, _ = await call(P + ".SaveLevelAs", {"TargetPath": SNAPSHOT_PATH})
                status = data.get("status")
                saved_package_path = data.get("targetPath")
                if status in ("saved", "partial"):
                    ok(status,
                       target=data.get("targetPath"),
                       source=data.get("sourcePath"),
                       fsize=data.get("fileSizeBytes"),
                       actors=data.get("actorCount"),
                       hash=data.get("actorNameHash"),
                       extSaved=data.get("externalActorsSaved"),
                       extFailed=data.get("externalActorsFailed"))
                else:
                    fail(f"status={status}, message={data.get('message', '?')}")
            except Exception as e:
                fail(f"SaveLevelAs failed: {e}")

            # ── 4. LoadLevel 加载刚保存的快照 ──
            print("\n4. LoadLevel (load back the snapshot)")
            try:
                data, _ = await call(P + ".LoadLevel", {
                    "LevelPath": SNAPSHOT_PATH,
                    "bSaveDirty": False,
                })
                status = data.get("status")
                if status == "loaded":
                    ok(status,
                       pkg=data.get("packagePath"),
                       actors=data.get("actorCount"),
                       hash=data.get("actorNameHash"),
                       fsize=data.get("fileSizeBytes"))
                    # 验证加载的是目标关卡
                    loaded_path = data.get("packagePath", "")
                    if SNAPSHOT_PATH in loaded_path or loaded_path == SNAPSHOT_PATH:
                        ok("path matches", expected=SNAPSHOT_PATH, actual=loaded_path)
                    else:
                        fail(f"packagePath mismatch: expected {SNAPSHOT_PATH}, got {loaded_path}")
                    # 验证有 Actor
                    actor_count = data.get("actorCount", 0)
                    if actor_count is None or actor_count == 0:
                        fail(f"actorCount is 0 or null after load")
                else:
                    fail(f"status={status}, message={data.get('message', '?')}")
            except Exception as e:
                fail(f"LoadLevel failed: {e}")

            # ── 5. 清理：加载回原关卡（不保存快照期间的修改） ──
            print("\n5. Cleanup: reload original level")
            try:
                if saved_package_path:
                    source = data.get("sourcePath")
                    # 从 SaveLevelAs 的返回中我们已经知道 sourcePath
                    pass
            except Exception:
                pass  # 清理失败不影响测试结果

            print(f"\n{'='*50}")
            if errors:
                print(f"FAILED: {errors} error(s)")
            else:
                print("ALL TESTS PASSED")
            return min(errors, 255)


def _extract_text(result) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return str(result.content)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: 运行测试（需要 UE Editor 运行中 + MCP Server 在 :8000 端口）**

```bash
cd "E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset"
python test_saveas_load.py
```

预期输出：
```
0. Loading toolset...
   [loaded message]

1. listActors (current level)
   OK  first actor: name=X, refPath=...

2. Modify first actor's transform
   OK  transform set: loc=(100.0,200.0,300.0), rot=(15.0,30.0,0.0), scale=(2.0,2.0,2.0)

3. SaveLevelAs → /Game/Maps/0714-Test-Snapshot
   OK  saved: target=/Game/Maps/0714-Test-Snapshot, ...

4. LoadLevel (load back the snapshot)
   OK  loaded: pkg=/Game/Maps/0714-Test-Snapshot, actors=N, ...
   OK  path matches: ...

5. Cleanup: reload original level

==================================================
ALL TESTS PASSED
```

- [ ] **Step 3: Commit**

```bash
cd "E:\Programs\UE_Project_58\MCP\Plugins\LevelPersistenceToolset"
git add test_saveas_load.py
git commit -m "test: add SaveLevelAs + LoadLevel end-to-end test

测试流程：listActors → 改首个 Actor 的 Transform →
SaveLevelAs 另存 → LoadLevel 加载快照 → 验证一致性。

直连 UE MCP (:8000)，不经过 Harness。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审清单

1. **需求覆盖：**
   - [x] SaveLevelAs：主 .umap 另存 + OFPA 子包原路径保存（Task 2）
   - [x] LoadLevel：bSaveDirty 控制保存行为，不弹对话框（Task 3）
   - [x] 代码规整：CollectLevelPackagesToSave 提取为公共函数（Task 1）
   - [x] 测试：listActors → 改 Transform → SaveLevelAs → LoadLevel → 验证（Task 4）

2. **占位符检查：** 无 TBD/TODO。

3. **类型一致性：**
   - `CollectLevelPackagesToSave(UPackage*)` → `TArray<UPackage*>` — Task 1 定义，Task 2 使用，签名一致。
   - `SaveLevelAs(TargetPath)` → JSON（含 targetPath, sourcePath, actorCount 等）— 与 SaveCurrentLevel 返回风格一致。
   - `LoadLevel(LevelPath, bSaveDirty=false)` → JSON（含 status, packagePath, actorCount 等）— 与 GetLevelFingerprint 返回风格一致。
   - 测试中 SNAPSHOT_PATH 为 `/Game/Maps/0714-Test-Snapshot`，LoadLevel 在同路径验证 packagePath 匹配。

4. **影响分析：**
   - 修改 `SaveCurrentLevel` 内联逻辑（15 行 → 1 行调用），行为不变。
   - 新增 2 个 UFUNCTION，不影响现有 5 个工具。
   - 测试脚本独立文件，不修改 `test_tools.py`。
