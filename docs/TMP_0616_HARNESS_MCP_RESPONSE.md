# Harness MCP 交互记录总结

> 日期：2026-06-16  
> 目的：记录与 ue-harness MCP Server 的完整端口交互，包括命令和完整结果。

---

## 一、MCP 配置发现

### 1.1 用户级 MCP 配置 (`C:\Users\Administrator\.mcp.json`)

```json
{
  "mcpServers": {
    "unreal-handshake": {
      "command": "python",
      "args": ["E:/Programs/UE Project 5.7.1/ModelGenerator5.7/Plugins/GenerativeAISupport/Content/Python/mcp_server.py"],
      "env": {
        "UNREAL_HOST": "localhost",
        "UNREAL_PORT": "9877"
      }
    },
    "ue-harness": {
      "type": "http",
      "url": "http://127.0.0.1:9000/mcp"
    }
  }
}
```

**发现：**
- `unreal-handshake`：命令驱动型 MCP，按需启动 Python 脚本连接 UE 端口 9877
- `ue-harness`：HTTP 型 MCP，直接连接 `http://127.0.0.1:9000/mcp`（本项目）

### 1.2 Settings 中的权限配置

`C:\Users\Administrator\.claude\settings.json` 中：
- `"mcp__unreal-handshake__handshake_test"` 在白名单中（`permissions.allow[75]`）
- 多个 MCP 相关的 Bash 命令在白名单中（PID 查询、服务器启动脚本复制等）

### 1.3 端口监听状态

```
端口              服务                   状态
127.0.0.1:9000    Harness MCP Server     ESTABLISHED (PID 33796)
127.0.0.1:8000    UE MCP Server          CLOSE_WAIT (PID 21408)
```

---

## 二、Harness MCP Server 发现

### 2.1 Initialize 握手

**请求：**
```
POST http://127.0.0.1:9000/mcp
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"}
  }
}
```

**响应：**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {
      "experimental": {},
      "tools": {"listChanged": false}
    },
    "serverInfo": {
      "name": "ue-agent-harness",
      "version": "1.27.2"
    },
    "instructions": "你是 UE Editor Agent，通过 Harness 中间层连接 Unreal Engine 5.8。\n自由探索模式下可用约 20 个核心工具。\n可用 Skill:\n  - evening-lighting: 将场景光照调整为黄昏/傍晚氛围\n调 activate_skill <名称> 激活 Skill，调 deactivate_skill 退出。\n调 get_context 获取最新 UE 状态快照和活跃 Skill 进度。"
  }
}
```

**关键发现：**
| 字段 | 值 | 说明 |
|:--|:--|:--|
| 服务器名 | `ue-agent-harness` | 本项目 |
| 版本 | `1.27.2` | |
| 协议版本 | `2024-11-05` | MCP 最新版本 |
| 工具能力 | `tools.listChanged: false` | 工具列表不会动态变化通知 |
| 实验性功能 | `experimental: {}` | 无实验性扩展 |

**说明：`tools.listChanged: false` 意味着 `load_toolset` 加载新工具集后，Client 无法收到推送通知，需要轮询或新回合重查。**

### 2.2 Prompts 端点

**请求：**
```
POST http://127.0.0.1:9000/mcp
{"jsonrpc":"2.0","id":1,"method":"prompts/list","params":{}}
```

**响应：**
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found"}}
```

**结论：Harness 不实现 MCP Prompts 端点。** Instructions 仅通过 `initialize` 握手传递——这是 MCP 协议本身的限制（Server 无法向 Client 的 system prompt 推内容），Harness 采用"LLM 按需拉取"模式，由 `get_context` 承担动态上下文注入。

---

## 三、工具列表

### 3.1 概览

`tools/list` 返回 **56 个工具**（对比：UE 原生 MCP 端口 8000 有 211 个工具）。

```
分类                              数量
===================================
Harness 内置（Skill/Context）       4
EditorAppToolset（编辑器应用）      17
core.actor（Actor 操作）           16
core.scene（场景操作）             12
core.object（对象操作）             5
core.asset（资产管理）              2
===================================
合计                               56
```

### 3.2 完整工具清单

#### Harness 内置 (4)

| 工具名 | 描述 |
|:--|:--|
| `activate_skill` | 激活一个 Skill（按名称或描述片段匹配），激活后可用工具收窄为 Skill 白名单 |
| `deactivate_skill` | 退出当前活跃的 Skill 模式，回到自由探索模式 |
| `get_context` | 获取 Harness 组装的完整上下文：UE 状态快照 + 活跃 Skill + 可用工具 |
| `save_skill` | 创建一个新的 Skill YAML 到 `~/.ue-harness/skills/` |

#### 工具集元操作 (3)

| 工具名 | 描述 |
|:--|:--|
| `list_toolsets` | 列出所有可用工具集及其名称和描述 |
| `describe_toolset` | 获取指定工具集的详细信息，包括所有工具名称、描述和输入模式 |
| `load_toolset` | 加载一个工具集，将其工具注册为原生 MCP 工具（**下一轮**才可用） |

#### EditorAppToolset (17)

| 工具名 | 描述 | 关键参数 |
|:--|:--|:--|
| `CaptureAssetImage` | 为资产生成缩略图截图 | `asset_path` |
| `CaptureEditorImage` | 对编辑器视口截图 | — |
| `FocusOnActors` | 将编辑器摄像机聚焦到指定 Actor | `actors[]` |
| `GetCameraTransform` | 获取视口摄像机位置和旋转 | — |
| `SetCameraTransform` | 设置视口摄像机位置和旋转 | `xform` |
| `GetContentBrowserPath` | 获取内容浏览器当前路径 | — |
| `SetContentBrowserPath` | 导航到指定文件夹路径 | `path` |
| `GetOpenAssets` | 获取资产编辑器中当前打开的资产 | — |
| `GetSelectedActors` | 获取关卡编辑器中当前选中的 Actor | — |
| `GetSelectedAssets` | 获取内容浏览器中选中的资产 | — |
| `GetVisibleActors` | 返回视口视锥体内可见的所有 Actor | — |
| `OpenEditorForAsset` | 为资产打开资产编辑器 | `asset_path` |
| `ScreenCoordsToWorld` | 屏幕坐标 → 世界坐标碰撞检测 | `coords` |
| `WorldPosToScreenCoords` | 世界坐标 → 屏幕坐标投影 | `position` |
| `SearchCVars` | 搜索包含指定名称的控制台变量 | `name` |
| `SelectActors` | 选中关卡中的 Actor | `actors[]` |
| `SelectAssets` | 选中内容浏览器中的资产 | `assets[]` |

#### core.actor — ActorTools (16)

| 工具名 | 描述 | 关键参数 |
|:--|:--|:--|
| `add_component` | 向 Actor 实例或蓝图添加组件 | `owner`, `component_type` |
| `add_tag` | 向 Actor 添加标签 | `actor`, `tag` |
| `get_actor_bounds` | 获取 Actor 世界空间包围盒 | `actor` |
| `get_actor_transform` | 获取 Actor 位置、旋转、缩放 | `actor` |
| `get_component_actor` | 获取组件所属的 Actor | `component` |
| `get_components` | 获取 Actor 包含的组件列表 | `actor`, `component_type`(可选) |
| `get_label` | 获取 Actor 的编辑器友好名称 | `actor` |
| `get_parent_component` | 获取组件的父组件 | `component` |
| `get_root_component` | 获取 Actor 的根组件 | `actor` |
| `get_tags` | 获取 Actor 的标签列表 | `actor` |
| `has_tag` | 检查 Actor 是否有指定标签 | `actor`, `tag` |
| `remove_component` | 从 Actor 移除组件 | `component` |
| `remove_tag` | 从 Actor 移除标签 | `actor`, `tag` |
| `set_actor_transform` | 更新 Actor 的位置、旋转和/或缩放 | `actor`, `xform` |
| `set_label` | 设置 Actor 友好名称 | `actor`, `label` |
| `set_parent_component` | 设置场景组件的父组件 | `component`, `parent` |

#### core.scene — SceneTools (12)

| 工具名 | 描述 | 关键参数 |
|:--|:--|:--|
| `add_to_scene_from_asset` | 从资产路径生成新 Actor | `asset_path`, `name`, `xform`(可选) |
| `add_to_scene_from_class` | 从类实例化新 Actor | `actor_type`, `name`, `xform`(可选) |
| `delete_folder` | 删除 Outliner 中的文件夹 | `folder_path` |
| `find_actors` | 按条件搜索场景中的 Actor | `root`(可选), `glob`(可选) |
| `get_actors_in_folder` | 获取指定 Outliner 文件夹中的 Actor | `folder_path`, `recursive` |
| `get_current_level` | 获取当前关卡资产路径 | — |
| `get_folders` | 获取 Outliner 中所有文件夹路径 | — |
| `load_level` | 在编辑器中加载关卡 | `level_path` |
| `remove_from_scene` | 从场景中删除 Actor | `actor` |
| `rename_folder` | 重命名 Outliner 文件夹 | `folder_path`, `new_path` |
| `set_actor_folder` | 将 Actor 分配到 Outliner 文件夹 | `actor`, `folder_path` |
| `trace_world` | 在世界中进行射线追踪 | `start`, `end` |

#### core.object — ObjectTools (5)

| 工具名 | 描述 | 关键参数 |
|:--|:--|:--|
| `get_class` | 获取 Unreal 对象的类 | `instance` |
| `get_properties` | 获取对象上一个或多个属性的值 | `instance`, `properties[]` |
| `list_properties` | 列出对象上的所有属性 | `instance` |
| `search_subclasses` | 查找指定类的所有子类 | `base_class`, `class_name`(可选) |
| `set_properties` | 设置对象属性的值（支持嵌套子对象） | `instance`, `values`(JSON) |

---

## 四、交互操作记录

### 4.1 激活 Skill

**命令：**
```
POST http://127.0.0.1:9000/mcp
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "activate_skill",
    "arguments": {"name_or_desc": "evening-lighting"}
  }
}
```

**响应：**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Skill 'evening-lighting' 已激活。将场景光照调整为黄昏/傍晚氛围\n步骤 (10 步)、工具白名单 (10 个): SceneTools.find_actors, SceneTools.get_current_level, ActorTools.get_actor_transform, ActorTools.set_actor_transform, ObjectTools.list_properties..."
      }
    ],
    "isError": false
  }
}
```

**结果分析：**
- Skill 激活后，可用工具从 56 个缩小到 **10 个白名单**
- 步骤数为 10 步（含截图验证）
- 白名单覆盖场景查询、Actor 变换、对象属性、编辑器截图 4 个领域

### 4.2 获取 UE 状态

**命令：**
```
POST http://127.0.0.1:9000/mcp
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "get_context",
    "arguments": {}
  }
}
```

**响应：**
```
你是一个运行在 Unreal Engine 5.8 中的 UE Editor Agent。
你可以使用工具来控制 Unreal Editor。
尽量使用截图验证你的修改。

当前 UE 状态：（31 分钟前刷新）
- 地图：/Temp/Untitled_1
- PIE：未知
- 选中 Actor：无
- 场景 Actor 数：16

任务 Skill：evening-lighting
步骤数：10
工具白名单：SceneTools.find_actors, SceneTools.get_current_level,
             ActorTools.get_actor_transform, ActorTools.set_actor_transform,
             ObjectTools.list_properties, ObjectTools.get_properties,
             ObjectTools.set_properties, SlateInspector.Screenshot,
             EditorAppToolset.CaptureEditorImage,
             EditorAppToolset.GetCameraTransform

步骤：
1. 调 SceneTools.find_actors(glob="*DirectionalLight*") 找到场景中所有的 DirectionalLight Actor
2. 调 ActorTools.get_actor_transform(主光) 获取当前变换
3. 调 ObjectTools.list_properties(主光) 了解可调整的光照属性
4. 将主 DirectionalLight 的旋转角度调整为低角度
   （Pitch 接近 -70 到 -80 度，模拟日落前 10-20 度的地平线高度）
5. 获取当前光照属性 → 设置 LightColor 为暖色
   （色温约 3000-4000K 对应 RGB ~(1.0, 0.7, 0.4)）
6. 将 Intensity 降低到默认值的 30-50%
7. 找到 SkyLight → 降低其 Intensity
8. 考虑添加 PostProcessVolume 并设置 ColorGrading 暖色调（如果场景中没有）
9. 调 SlateInspector.Screenshot 截图当前视口
10. 视觉验证：场景应具有温暖的低角度光照和长阴影
```

**结果分析：**
- `get_context` 返回的是 Harness 组装后的**完整 prompt**，而非原始 JSON
- 包含三层内容：角色指令 + UE 状态快照 + 活跃 Skill 步骤
- 状态标注"31 分钟前刷新"，说明 State Cache 存在延迟
- PIE 状态"未知"，是覆盖盲区

---

## 五、架构关键发现

### 5.1 Harness 的三层 Context 组装

```
┌─────────────────────────────────────┐
│ Layer 1: 角色指令                    │
│ "你是运行在 UE 5.8 中的 Editor Agent" │
├─────────────────────────────────────┤
│ Layer 2: UE 状态快照 (State Cache)   │
│ 地图、PIE、选中 Actor、Actor 总数    │
├─────────────────────────────────────┤
│ Layer 3: 活跃 Skill 上下文           │
│ 步骤列表 + 工具白名单                │
└─────────────────────────────────────┘
```

### 5.2 Interceptor 链行为

| 拦截器 | 触发点 | 效果 |
|:--|:--|:--|
| DebugPreCall | 每次工具调用前 | 日志记录 |
| StateCache | post_call | 更新 L1 写入缓存 |
| Vision | post_call（截图工具） | 截图→视觉验证闭环 |

### 5.3 Skill 系统

| 特性 | 实现方式 |
|:--|:--|
| 存储 | YAML 文件，`~/.ue-harness/skills/` 目录 |
| 注册 | `save_skill` 工具 |
| 匹配 | 名称或描述片段模糊匹配 |
| 激活后行为 | 工具白名单 + 步骤指令注入 |
| 退出 | `deactivate_skill` 恢复到 56 工具的自由探索模式 |

### 5.4 State Cache 行为

| 属性 | 值 |
|:--|:--|
| L1 (write-through) | post_call 后同步写入 |
| L3 (refresh) | 硬边界刷新，当前快照显示 31 分钟延迟 |
| 覆盖盲区 | PIE 状态返回"未知" |
| 响应格式 | 快照数据组装为自然语言嵌入 prompt |

### 5.5 协议限制

- Harness **不实现** `prompts/list` 端点（返回 Method not found）
- `tools.listChanged: false` — 工具集动态加载后无法推送通知
- `load_toolset` 的工具在**下一个回合**才可用（因为无推送机制）
- Instructions 仅通过 `initialize` 握手传递，无法在运行中更新 system prompt（MCP 协议固有限制）

---

## 六、与 UE 原生 MCP 的对比

| 维度 | UE 原生 MCP (8000) | ue-harness (9000) |
|:--|:--|:--|
| 工具数 | 211 | 56（含 4 个 Harness 内置） |
| 工具集管理 | 无 | `list_toolsets` / `describe_toolset` / `load_toolset` |
| Skill 系统 | 无 | CRUD + 激活 / 停用 |
| Context 组装 | 无 | 三层 prompt 组装 + 步骤注入 |
| 状态缓存 | 无 | L1 write-through + L3 刷新 |
| 视觉验证 | 无 | Vision interceptor 闭环（006 阶段） |
| 工具过滤 | 全量暴露 | 按 Skill 白名单动态收窄 |
| 日志 | 无 | JSONL 日志 + stats + replay |
| SSE 支持 | ✅ | ✅ |

---

## 七、传输层细节

### Accept Header 要求

Harness MCP Server **严格要求** Accept header 必须同时包含 `application/json` 和 `text/event-stream`：

- `Accept: text/event-stream` → 400: "Client must accept both application/json and text/event-stream"
- `Accept: application/json` → 400: "Not Acceptable: Client must accept text/event-stream"
- `Accept: application/json, text/event-stream` → ✅ 正常

### SSE 响应格式

所有响应使用 SSE (Server-Sent Events) 格式：
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{...}}
```

---

*Generated: 2026-06-16 | Harness v1.27.2 | UE 5.8*
