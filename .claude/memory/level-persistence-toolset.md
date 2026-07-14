---
name: level-persistence-toolset
description: UE 侧 LevelPersistenceToolset 插件——七工具：关卡保存/指纹/脏包/另存/加载
metadata:
  type: reference
---

# LevelPersistenceToolset

位于 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/`，C++ Editor 插件，通过 UToolsetDefinition + UEditorSubsystem 模式向 UE MCP Server 注册 7 个工具。

> **状态 (2026-07-14)：** 七工具全部实现并通过测试。`test_tools.py`（五工具基础测试）和 `test_saveas_load.py`（SaveLevelAs + LoadLevel）均已跑通。UE 侧无需进一步开发。

## 七工具一览

| 工具 | 参数 | 用途 |
|---|---|---|
| `SaveCurrentLevel` | — | 保存当前关卡 + 指纹（含 OFPA 外部 Actor 处理） |
| `SaveAsset` | `AssetPath` | 保存指定资产 + 指纹 |
| `SaveAll` | — | 保存所有脏包 + 列表 |
| `ListDirtyPackages` | — | JSON 数组：所有脏包路径 |
| `GetLevelFingerprint` | `LevelPath`（`""`=当前关卡） | 只读指纹，已加载/未加载两种模式 |
| **`SaveLevelAs`** | `TargetPath: string` | 另存当前关卡到新路径（含全部外部包） |
| **`LoadLevel`** | `LevelPath: string`, `bSaveDirty: bool=false` | 加载指定关卡，不弹保存对话框 |

## 指纹字段

`packageGuid`, `fileSizeBytes`, `lastModified` (ISO8601), `actorCount`, `actorNameHash` (CRC32 hex), `externalActorPackages/ActorsSaved/ActorsFailed`, `isLoaded` (GetLevelFingerprint 专用)

## SaveLevelAs / LoadLevel 额外字段

- `targetPath`: 快照目标包路径
- `sourcePath`: 原关卡包路径

## 全限定工具名格式

`LevelPersistenceToolset.LevelPersistenceToolset.{ToolName}`
（不是 `ToolsetRegistry.` 前缀——项目插件用模块名前缀）

## MCP 使用注意

- UE MCP 默认 deferred 模式，需先 `load_toolset("LevelPersistenceToolset.LevelPersistenceToolset")` 再调工具
- 返回值被 ToolsetRegistry 包装为 `{"returnValue": "<json_string>"}`，需二次解析
- 测试脚本 `test_tools.py`（五工具）和 `test_saveas_load.py`（SaveLevelAs + LoadLevel）位于插件根目录

---

## SaveLevelAs 核心流程

复刻 `FEditorFileUtils::SaveCurrentLevel`（Ctrl+S）的模式，但将主包保存到指定路径。

```
SaveLevelAs(TargetPath):
  ① 验证参数 + IsEditorReady + LevelPackage
  ② SourcePath == CleanPath → SaveCurrentLevel 快捷路径
  ③ 解析 TargetPath → 文件系统路径
  ④ 已存在目标快照 → 卸载旧包(ResetLoaders+ClearFlags) + 删除旧文件
  ⑤ Level->GetLoadedExternalObjectPackages() 收集全部外部包
     （含 PKG_NewlyCreated 的 HLOD 生成包 + UPackage::IsEmptyPackage 的空包）
  ⑥ 逐个 UPackage::Save 保存外部包（跳过主包）
  ⑦ FEditorFileUtils::SaveMap(World, TargetFilename) 保存主包
     （SaveMap = SaveWorld = Ctrl+S 核心内核）
  ⑧ LevelPackage->SetDirtyFlag(false)
     （SaveMap 写新路径但包在内存中未改名，dirty flag 残留）
  ⑨ 组装 JSON：status + targetPath + sourcePath + 文件指纹 + actorCount + actorNameHash
```

### 关键设计决策

| 问题 | 选择 | 原因 |
|---|---|---|
| 保存接口 | `FEditorFileUtils::SaveMap` | = Ctrl+S 核心内核，内部处理 World Partition / OFPA / HLOD |
| 外部包收集 | `Level->GetLoadedExternalObjectPackages()` | 引擎 API，能正确找到所有关联包（含 `__ExternalActors__` 目录下的 HLOD 生成包） |
| 旧快照覆盖 | ResetLoaders + ClearFlags + Delete | 防止 "同名包已存在" 导致 SaveMap 失败 |

### 6 种尝试接口记录

| # | 接口 | 失败原因 |
|---|---|---|
| 1 | `UPackage::Save` 只保存主包 | OFPA 子包不处理，Untitled 无磁盘路径 |
| 2 | 主包新路径 + OFPA 子包原路径 | /Temp/ 子包 `ResolvePackageFilename` 返回空 |
| 3 | `UPackage::Rename` 全部重命名 + 保存 | World Partition HLOD 用 GUID 引用，Rename 无法更新 |
| 4 | `FEditorFileUtils::SaveLevel` | 对已保存关卡 DefaultFilename 被忽略 |
| 5 | `FEditorFileUtils::SaveMap` 单独调用 | dirty flag 残留 |
| 6 | **SaveCurrentLevel 模式 + SaveMap + SetDirtyFlag** | ✅ |

---

## LoadLevel 核心流程

```
LoadLevel(LevelPath, bSaveDirty=false):
  ① 验证参数 + IsEditorReady
  ② 解析 LevelPath → 文件路径
  ③ 检查文件是否存在
  ④ bSaveDirty==true → FEditorFileUtils::SaveDirtyPackages(bPromptToSave=false, bFastSave=true)
     bSaveDirty==false → 跳过，脏数据随旧 World 卸载丢弃
  ⑤ FEditorFileUtils::LoadMap(Filename, LoadAsTemplate=false, bShowProgress=false)
     （注释明确承诺 "Does not prompt the user to save the current map"）
  ⑥ 加载成功 → 组装 JSON：status="loaded" + 指纹
```

---

## 与 Untitled 关卡的不兼容

| 场景 | SaveLevelAs | LoadLevel |
|---|---|---|
| 已保存关卡（`/Game/`） | ✅ 完美 | ✅ 完美 |
| 基本 Untitled（`/Temp/`，无 World Partition） | ✅ 应可工作 | ✅ 应可工作 |
| Open World 模板 Untitled | ⚠️ HLOD warning（Ctrl+S 也有） | ❌ WorldPartition crash |

**HLOD warning 原因**：Open World 模板创建的 Untitled 关卡包含自动生成的 HLOD 代理 StaticMesh，存储在 `/Temp/.../__ExternalActors__/` 路径下。这些是引擎自动生成的 private object，SaveAs 无法重映射引用。**这是 UE5 引擎限制，手动Ctrl+S 同样存在**，非工具问题。编辑器 "Repair" 按钮可修复。

**Harness 实际使用的限制**：match_reference 操作需要保证是用户已保存的真实关卡，否则会再次遇到 Untitled + HLOD 场景导致的引擎崩溃。

---

## `CollectLevelPackagesToSave` vs `GetLoadedExternalObjectPackages`

两个收集函数分工不同：

| | `CollectLevelPackagesToSave` | `GetLoadedExternalObjectPackages` |
|---|---|---|
| 来源 | 手写帮助函数 | 引擎 API |
| 匹配方式 | 前缀匹配 `"{包名}_InstanceOf"` | 引擎内部索引 |
| 过滤 | `IsDirty()` | `IsDirty() \|\| PKG_NewlyCreated \|\| IsEmptyPackage` |
| 用途 | `SaveCurrentLevel`（存盘用） | `SaveLevelAs`（完整 SaveAs） |

## 限制

`actorNameHash` 只探测 Actor 增删改名，不探测 transform/属性/component 变化。分量变更依赖 dirty flag + mtime 间接捕获。
