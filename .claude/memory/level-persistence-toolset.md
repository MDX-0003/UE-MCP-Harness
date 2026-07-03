---
name: level-persistence-toolset
description: UE 侧 LevelPersistenceToolset 插件——关卡保存/指纹/脏包查询五工具
metadata:
  type: reference
---

# LevelPersistenceToolset

位于 `{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/`，C++ Editor 插件，通过 UToolsetDefinition + UEditorSubsystem 模式向 UE MCP Server 注册 5 个工具。

## 五工具

| 工具 | 参数 | 用途 |
|---|---|---|
| `SaveCurrentLevel` | — | 保存当前关卡 + 返回指纹（含 OFPA 外部 Actor 处理） |
| `SaveAsset` | `AssetPath` | 保存指定资产 + 指纹 |
| `SaveAll` | — | 保存所有脏包 + 列表 |
| `ListDirtyPackages` | — | JSON 数组：所有脏包路径 |
| `GetLevelFingerprint` | `LevelPath`（`""`=当前关卡） | 只读指纹，已加载/未加载两种模式 |

## 指纹字段

`packageGuid`, `fileSizeBytes`, `lastModified` (ISO8601), `actorCount`, `actorNameHash` (CRC32 hex), `externalActorPackages/ActorsSaved/ActorsFailed`, `isLoaded` (GetLevelFingerprint 专用)

## 全限定工具名格式

`LevelPersistenceToolset.LevelPersistenceToolset.{ToolName}`（不是 `ToolsetRegistry.` 前缀——项目插件用模块名前缀）

## MCP 使用注意

- UE MCP 默认 deferred 模式，需先 `load_toolset("LevelPersistenceToolset.LevelPersistenceToolset")` 再调工具
- 返回值被 ToolsetRegistry 包装为 `{"returnValue": "<json_string>"}`，需二次解析
- 详见 [[level-persistence-contract]] 和 docs/contracts.md §4

## 限制

`actorNameHash` 只探测 Actor 增删改名，不探测 transform/属性/component 变化。分量变更依赖 dirty flag + mtime 间接捕获。
