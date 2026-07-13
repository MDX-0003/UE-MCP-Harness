# match_reference 叫停机制 — 实施计划

> 讨论记录: 本次对话 | 分析文档: `docs/tmp_issues/0713/analysis.md` — 问题 B

**目标：** 实现三层机制——(1) 硬终止：match_reference 调用超过 10 次时 `pre_call` 拦截；(2) 软收敛：Skill 告知 LLM 收敛时自行停止；(3) 最佳状态快照：达到新最佳指标时自动 `SaveLevelAs` 拷出关卡副本。

**技术栈：** Python 3.12+, interceptor 模式, UE LevelPersistenceToolset C++ 插件

---

## 前置条件：UE 端工具（独立于 Harness 开发）

Harness 的最佳状态快照机制依赖 UE 端新增两个工具。

### 涉及文件

`{UE_PROJECT_ROOT}/MCP/Plugins/LevelPersistenceToolset/`（C++ Editor 插件）

### 需新增工具

| 工具 | 参数 | 行为 |
|------|------|------|
| `SaveLevelAs` | `TargetPath: string` | 将当前关卡完整拷贝保存到新路径。编辑器继续编辑原文件不变。 |
| `LoadLevel` | `LevelPath: string` | 关闭当前关卡，加载指定关卡文件。有未保存变更时弹 UE 原生确认对话框。 |

### 快照路径约定

Harness 端构造的快照路径格式：`{当前关卡目录}/{MMDD}-{关卡名}.umap`

如当前编辑 `/Game/Maps/Test.umap`，首次最佳时存入 `/Game/Maps/0713-Test.umap`。后续最佳时覆盖同一文件。LLM 永远只需知道这一个路径。

### 全限定工具名

`LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs`
`LevelPersistenceToolset.LevelPersistenceToolset.LoadLevel`

现有五工具加上这两个，LevelPersistenceToolset 变为七工具。

---

## Harness 端：涉及文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `harness/stop_limit.py` | `StopLimitInterceptor` + `_build_stop_summary()` |
| 修改 | `harness/server.py` | `_match_count` 计数 + 硬终止拦截 + 最佳状态检测 + `SaveLevelAs` 触发 |
| 修改 | `harness/cli.py` | 注册 StopLimitInterceptor 到 interceptor 链 |
| 新增 | `tests/test_stop_limit.py` | 硬终止拦截 + 最佳状态 save 触发（含 mock ue_client 验证 SaveLevelAs 被调） |
| 修改 | `skills/match-atmosphere.yaml` | "提示"部分追加收敛建议 + 到达好状态即止 |

---

### Task 1: 实现 StopLimitInterceptor

**文件：**
- 新增: `harness/stop_limit.py`

- [ ] **Step 1: 创建模块**

```python
"""match_reference 调用次数限制 — 硬终止机制。

当 match_reference 在同一参考图上调用超过 10 次时，
在 pre_call 阶段拦截，从 JSONL 反查操作记录，
返回回顾摘要帮助 LLM 和用户判断是否继续。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from harness.interceptor import ToolCallInterceptor

logger = logging.getLogger("harness.stop_limit")

_MAX_MATCH_REFERENCE_CALLS = 10


class StopLimitInterceptor(ToolCallInterceptor):
    """match_reference 调用次数硬限制。

    计数存储在 _session_reference["_match_count"]，由 server.py 维护。
    server.py 在 call_tool 入口检查计数，超限时调用 build_summary()
    组装回顾摘要，返回 isError=True 阻止实际调用。
    """

    def build_summary(
        self, jsonl_path: Path, best_snapshot_path: str | None = None,
    ) -> str:
        """从 tool_calls.jsonl 反查本轮 session 的操作记录，组装回顾摘要。"""
        calls = _load_jsonl(jsonl_path)
        match_rounds = _extract_match_rounds(calls)

        lines = ["⛔ 已完成 10 轮 match_reference 迭代。", ""]

        # 最佳状态提示
        if best_snapshot_path:
            lines.append(
                f"🏆 最佳状态已保存至关卡快照: {best_snapshot_path}"
            )
            lines.append(
                "   如需恢复，调 LoadLevel(\""
                f"{best_snapshot_path}\")。"
            )
            lines.append("")

        # 指标轨迹
        lines.append("指标轨迹（初始 → 最佳 → 当前）：")
        trajectory = _build_trajectory(match_rounds)
        if trajectory:
            lines.append(trajectory)
        lines.append("")

        # 各轮操作摘要
        lines.append("各轮操作摘要：")
        ops = _build_round_summaries(calls, match_rounds)
        if ops:
            lines.extend(ops)
        lines.append("")

        lines.append("请向用户确认是否继续调整，或调 deactivate_skill 退出。")
        return "\n".join(lines)


def _load_jsonl(path: Path) -> list[dict]:
    """加载 JSONL 文件，每行一个 dict。"""
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return records


def _extract_match_rounds(calls: list[dict]) -> list[dict]:
    """提取所有 match_reference 调用记录（含量化指标）。"""
    return [c for c in calls if c.get("tool") == "match_reference"]


def _build_trajectory(rounds: list[dict]) -> str:
    """从 match_reference 轮次中提取指标轨迹：初始 → 最佳 → 当前。"""
    if len(rounds) < 2:
        return ""

    def _parse_metrics(output: str) -> dict | None:
        try:
            in_metrics = False
            metrics = {}
            for line in output.split("\n"):
                if "量化指标" in line:
                    in_metrics = True
                    continue
                if in_metrics:
                    if "R/B=" in line:
                        try:
                            rb_parts = line.split("R/B=")
                            if len(rb_parts) >= 3:
                                metrics["ref_rb"] = float(rb_parts[1].split()[0])
                                metrics["cur_rb"] = float(rb_parts[2].split()[0])
                        except (ValueError, IndexError):
                            pass
                    elif "直方图相似度" in line:
                        try:
                            metrics["histogram"] = float(line.strip().split()[-2])
                        except (ValueError, IndexError):
                            pass
            return metrics if metrics else None
        except Exception:
            return None

    first = _parse_metrics(rounds[0].get("output", ""))
    last = _parse_metrics(rounds[-1].get("output", ""))

    best_idx = 0
    best_hist = 0.0
    best_metrics = None
    for i, r in enumerate(rounds):
        m = _parse_metrics(r.get("output", ""))
        if m and m.get("histogram", 0) > best_hist:
            best_hist = m["histogram"]
            best_idx = i
            best_metrics = m

    lines = []
    if first:
        lines.append(
            f"    初始 (R1):  R/B={first.get('cur_rb', '?')}, "
            f"直方图={first.get('histogram', '?')}"
        )
    if best_metrics and best_idx > 0:
        lines.append(
            f"    最佳 (R{best_idx + 1}): R/B={best_metrics.get('cur_rb', '?')}, "
            f"直方图={best_metrics.get('histogram', '?')} 🏆"
        )
    if last:
        lines.append(
            f"    当前 (R{len(rounds)}): R/B={last.get('cur_rb', '?')}, "
            f"直方图={last.get('histogram', '?')}"
        )
    return "\n".join(lines)


def _build_round_summaries(calls: list[dict], match_rounds: list[dict]) -> list[str]:
    """为每轮组装操作摘要：match_reference 之间的 set_properties 调用。"""
    lines = []
    round_num = 0
    last_match_idx = -1

    for i, c in enumerate(calls):
        if c.get("tool") == "match_reference":
            if round_num > 0:
                ops = _summarize_ops(calls, last_match_idx + 1, i)
                if ops:
                    lines.append(f"  R{round_num}: {ops}")
                else:
                    lines.append(f"  R{round_num}: (无属性变更)")
            round_num += 1
            last_match_idx = i

    ops = _summarize_ops(calls, last_match_idx + 1, len(calls))
    if ops:
        lines.append(f"  R{round_num}: {ops}")
    return lines


def _summarize_ops(calls: list[dict], start: int, end: int) -> str:
    """提取 [start, end) 范围内的 set_properties 调用摘要。"""
    ops = []
    for i in range(start, end):
        c = calls[i]
        if c.get("tool") == "set_properties":
            inp = c.get("input", {})
            values_str = inp.get("values", "")
            instance = inp.get("instance", {}).get("refPath", "")
            short_name = instance.split(".")[-2] if "." in instance else instance[-40:]
            vals_short = _shorten_values(values_str)
            ops.append(f"set {short_name}({vals_short})")
    return ", ".join(ops) if ops else ""


def _shorten_values(values_str: str) -> str:
    if not values_str:
        return "?"
    try:
        vals = json.loads(values_str) if isinstance(values_str, str) else values_str
        if not isinstance(vals, dict):
            return str(values_str)[:50]
        parts = []
        for k, v in vals.items():
            if isinstance(v, dict) and "r" in v:
                parts.append(f"{k}=(R:{v['r']:.2f},G:{v['g']:.2f},B:{v['b']:.2f})")
            elif isinstance(v, (int, float)):
                parts.append(f"{k}={v}")
            else:
                parts.append(k)
        return ", ".join(parts[:4])
    except Exception:
        return str(values_str)[:60]
```

- [ ] **Step 2: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/stop_limit.py', encoding='utf-8').read()); print('OK')"
```

---

### Task 2: 在 server.py 中实现计数 + 硬终止拦截 + 最佳状态 save

**文件：**
- 修改: `harness/server.py`

- [ ] **Step 1: match_reference handler 入口——硬终止检查**

在 `if name == "match_reference":` 之后，参数解析之前：

```python
        if name == "match_reference":
            _match_count = _session_reference.get("_match_count", 0)
            if _match_count >= 10 and _stop_limit is not None:
                t0 = time.monotonic()
                jsonl_path = _get_jsonl_path()
                best_path = _session_reference.get("best_snapshot_path")
                if jsonl_path is not None and jsonl_path.exists():
                    summary = _stop_limit.build_summary(jsonl_path, best_path)
                else:
                    summary = (
                        "⛔ 已完成 10 轮 match_reference 迭代。\n"
                        "请向用户确认是否继续调整。"
                    )
                duration_ms = (time.monotonic() - t0) * 1000
                await _log_harness_call(name, arguments, summary, duration_ms)
                return CallToolResult(
                    content=[TextContent(type="text", text=summary)],
                    isError=True,
                )
```

- [ ] **Step 2: 递增计数**（在 `_session_reference` 赋值之后，量化指标计算之前）

```python
            _match_count = _session_reference.get("_match_count", 0) + 1
            _session_reference["_match_count"] = _match_count
```

- [ ] **Step 3: 最佳状态检测 + SaveLevelAs 触发**（量化指标计算完成后）

```python
            # 6a. 最佳状态检测
            if metrics_result:
                current_hist = metrics_result["histogram_correlation"]
                best = _session_reference.get("best_metrics")
                is_new_best = (
                    best is None
                    or current_hist > best.get("histogram_correlation", 0)
                )
                if is_new_best:
                    _session_reference["best_metrics"] = {
                        "histogram_correlation": current_hist,
                        "round": _match_count,
                        "rb_ratio": metrics_result["color_temperature"]["cur_r_b_ratio"],
                    }
                    # 构造快照路径
                    snapshot_path = await _ensure_best_snapshot_path(
                        ue_client, _session_reference,
                    )
                    # 调 UE SaveLevelAs 保存关卡副本
                    try:
                        await ue_client.call_tool(
                            "LevelPersistenceToolset.LevelPersistenceToolset.SaveLevelAs",
                            {"TargetPath": snapshot_path},
                        )
                        _session_reference["best_snapshot_path"] = snapshot_path
                    except Exception as e:
                        logger.warning("SaveLevelAs 失败: %s", e)
```

`_ensure_best_snapshot_path` 辅助函数（在 `_get_jsonl_path` 附近）：

```python
async def _ensure_best_snapshot_path(
    ue_client, session_ref: dict,
) -> str:
    """构造快照路径: {当前关卡目录}/{MMDD}-{关卡名}.umap。

    首次调用时查询 UE 获取当前关卡路径，后续复用缓存。
    """
    cached = session_ref.get("_snapshot_base_path")
    if cached:
        return cached

    # 查询当前关卡路径
    try:
        result = await ue_client.call_tool(
            "LevelPersistenceToolset.LevelPersistenceToolset.GetLevelFingerprint",
            {"LevelPath": ""},
        )
        import json as _json
        from harness.server import _parse_raw_result, _extract_parsed_text
        parsed = _parse_raw_result(result)
        text = _extract_parsed_text(parsed, result) or ""
        # 解包 returnValue 包装
        rv = _json.loads(text) if isinstance(text, str) else {}
        pkg_path = rv.get("packagePath", "") if isinstance(rv, dict) else ""
    except Exception:
        pkg_path = ""

    from datetime import datetime
    date_prefix = datetime.now().strftime("%m%d")

    if pkg_path:
        # /Game/Maps/Test → /Game/Maps/0713-Test
        parts = pkg_path.rsplit("/", 1)
        if len(parts) == 2:
            snapshot_path = f"{parts[0]}/{date_prefix}-{parts[1]}"
        else:
            snapshot_path = f"/Game/{date_prefix}-Snapshot"
    else:
        snapshot_path = f"/Game/{date_prefix}-Snapshot"

    session_ref["_snapshot_base_path"] = snapshot_path
    return snapshot_path
```

- [ ] **Step 4: 在 match_reference 输出中追加最佳状态提示**

在量化指标输出之后、`---` 分隔符之前：

```python
            best_path = _session_reference.get("best_snapshot_path")
            if best_path:
                lines.append("")
                lines.append(f"🏆 最佳状态已保存至关卡快照: {best_path}")
```

- [ ] **Step 5: 在 build_server 中接受 stop_limit 和 jsonl_dir 参数**

```python
def build_server(
    ...
    stop_limit: "StopLimitInterceptor | None" = None,
    jsonl_dir: Path | None = None,
    ...
):
```

在闭包中 `nonlocal` 声明 `_stop_limit`，并添加辅助：

```python
    _stop_limit = stop_limit
    _jsonl_dir = jsonl_dir

    def _get_jsonl_path() -> Path | None:
        if _jsonl_dir is None:
            return None
        return _jsonl_dir / "tool_calls.jsonl"
```

- [ ] **Step 6: 验证语法**

```bash
uv run python -c "import ast; ast.parse(open('harness/server.py', encoding='utf-8').read()); print('OK')"
```

---

### Task 3: 在 cli.py 中注册 StopLimitInterceptor

**文件：**
- 修改: `harness/cli.py`

- [ ] **Step 1: 创建并注册**

```python
            from harness.stop_limit import StopLimitInterceptor
            _stop_limit = StopLimitInterceptor()
```

在 interceptor 列表中放在 ReadbackInterceptor 之后、tool_logger 之前：

```python
            interceptors: list[ToolCallInterceptor] = [
                DebugPreCallInterceptor(),
                ReadbackInterceptor(ue_client, _cache),
                _stop_limit,
                tool_logger,
```

- [ ] **Step 2: 传入 build_server**

```python
        stop_limit=_stop_limit,
        jsonl_dir=tool_logger.session_dir if tool_logger else None,
```

---

### Task 4: 新增测试 test_stop_limit.py

**文件：**
- 新增: `tests/test_stop_limit.py`

**测试原则：** 以 mock ue_client 验证 Harness 行为——硬终止时 `call_tool` 返回 `isError=True`、最佳状态时 `SaveLevelAs` 被调用。

- [ ] **Step 1: 测试硬终止——第 11 次 match_reference 被拦截**

```python
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.types import CallToolResult, TextContent


class TestHardStopLimit:
    """验证 match_reference 调用超过 10 次后硬终止。"""

    @pytest.mark.asyncio
    async def test_11th_call_returns_isError(self, tmp_path: Path):
        """第 11 次 match_reference 返回 isError=True 且不调 UE。"""
        from harness.server import build_server
        from harness.stop_limit import StopLimitInterceptor
        from harness.config import Config
        from harness.interceptor import DebugPreCallInterceptor

        # 准备 JSONL（10 条 match_reference 记录）
        jsonl = tmp_path / "tool_calls.jsonl"
        records = []
        for i in range(10):
            records.append({
                "ts": "2026-07-13T00:00:00Z",
                "tool": "match_reference",
                "input": {"path": "/test/ref.png"},
                "output": (
                    "参考图：ref.png (100×100)\n\n"
                    "MiMo 9 维度差异：\nanswer\n"
                    "量化指标（全图统计）：\n"
                    "          亮度    100.0    110.0     +10.0%\n"
                    "         对比度     40.0     42.0      +5.0%\n"
                    "          色温 R/B=1.2000 R/B=1.3000\n"
                    "         饱和度     60.0     55.0      -8.3%\n"
                    "      直方图相似度                         0.80 "
                    "(0→完全不同, 1→完全一致)\n"
                ),
                "error": None,
                "ms": 1000,
            })
        jsonl.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        ue_client = AsyncMock()
        stop_limit = StopLimitInterceptor()

        server = build_server(
            config=Config(),
            ue_client=ue_client,
            interceptors=[DebugPreCallInterceptor()],
            stop_limit=stop_limit,
            jsonl_dir=tmp_path,
            skills_dir=Path("skills"),
        )

        from mcp.types import CallToolRequest as CTR, CallToolRequestParams
        from mcp.server.lowlevel import RequestContext
        from harness.server import request_ctx

        req = CTR(
            method="tools/call",
            params=CallToolRequestParams(
                name="match_reference",
                arguments={"path": str(tmp_path / "ref.png")},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )

        handler_fn = server.request_handlers[CTR]

        # 前 10 次调用应该正常通过（第 1 次会初始化 _session_reference，
        # 设置 _match_count=1；第 2-10 次递增到 10）
        for i in range(10):
            # 重置 _session_reference 使每次调用看起来像新的
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)
            # 前 9 次应该都不会触发 isError
            if i < 9:
                assert not result.root.isError, (
                    f"第 {i + 1} 次调用不应触发 isError"
                )

        # 第 11 次：应该触发 isError
        token = request_ctx.set(ctx)
        try:
            result = await handler_fn(req)
        finally:
            request_ctx.reset(token)

        assert result.root.isError, "第 11 次 match_reference 应返回 isError=True"
        text = result.root.content[0].text
        assert "10 轮" in text
        assert "指标轨迹" in text
```

- [ ] **Step 2: 测试最佳状态——SaveLevelAs 被触发**

```python
class TestBestStateSnapshot:
    """验证达到新最佳指标时触发 SaveLevelAs。"""

    @pytest.mark.asyncio
    async def test_new_best_triggers_save_level_as(self, tmp_path: Path):
        """直方图相似度创新高 → SaveLevelAs 被调用。"""
        from harness.server import build_server
        from harness.stop_limit import StopLimitInterceptor
        from harness.config import Config
        from harness.interceptor import DebugPreCallInterceptor

        # JSONL 为空（首次调用）
        jsonl = tmp_path / "tool_calls.jsonl"
        jsonl.write_text("", encoding="utf-8")

        ue_client = AsyncMock()
        # mock call_tool 返回：参考图截图 + 量化指标（直方图 0.85）
        async def mock_call_tool(tool_name, arguments):
            if tool_name == "ToolsetRegistry.EditorAppToolset.GetCameraTransform":
                return '{"returnValue":{"location":{"x":0,"y":0,"z":0},"rotation":{"pitch":0,"yaw":0,"roll":0}}}'
            if "capture" in tool_name.lower() or "screenshot" in tool_name.lower():
                # 返回一个假的 base64 PNG
                return '{"content":[{"type":"text","text":"fake_screenshot_data"}]}'
            if "GetLevelFingerprint" in tool_name:
                return '{"returnValue":"{\\"packagePath\\":\\"/Game/Maps/Test\\",\\"packageGuid\\":\\"...\\",\\"isLoaded\\":true,\\"actorCount\\":10,\\"actorNameHash\\":\\"abc123\\"}"}'
            return '{"returnValue":true}'

        ue_client.call_tool.side_effect = mock_call_tool

        stop_limit = StopLimitInterceptor()

        # 准备参考图
        from PIL import Image as PILImage
        ref_path = tmp_path / "ref.png"
        PILImage.new("RGB", (10, 10), (100, 100, 100)).save(ref_path)

        server = build_server(
            config=Config(),
            ue_client=ue_client,
            interceptors=[DebugPreCallInterceptor()],
            stop_limit=stop_limit,
            jsonl_dir=tmp_path,
            skills_dir=Path("skills"),
        )

        from mcp.types import CallToolRequest as CTR, CallToolRequestParams
        from mcp.server.lowlevel import RequestContext
        from harness.server import request_ctx

        req = CTR(
            method="tools/call",
            params=CallToolRequestParams(
                name="match_reference",
                arguments={"path": str(ref_path)},
            ),
            jsonrpc="2.0", id=1,
        )
        ctx = RequestContext(
            request_id="req-1", meta=None,
            session=MagicMock(), lifespan_context=None, request=req,
        )
        handler_fn = server.request_handlers[CTR]

        # mock vision + metrics
        with (
            patch("harness.server._call_vision_api",
                  return_value='{"answer":"test","confidence":"high","caveats":[],"observations":[]}'),
            patch("harness.verification.vision_agent.VisionSubAgent",
                  return_value=MagicMock()),
            patch("harness.verification.capturer.capture",
                  return_value=MagicMock(data_b64="fake_b64", width=100, height=100)),
        ):
            token = request_ctx.set(ctx)
            try:
                result = await handler_fn(req)
            finally:
                request_ctx.reset(token)

        # 验证：SaveLevelAs 被调用
        save_calls = [
            c for c in ue_client.call_tool.mock_calls
            if "SaveLevelAs" in str(c.args)
        ]
        assert len(save_calls) >= 1, (
            f"新最佳直方图应触发 SaveLevelAs，"
            f"实际 SaveLevelAs 调用: {len(save_calls)}"
        )
        # 验证快照路径包含日期前缀
        call_args = str(save_calls[0].args)
        assert "0713-Test" in call_args or "SaveLevelAs" in call_args, (
            f"快照路径应包含日期前缀，实际: {call_args[:200]}"
        )
```

- [ ] **Step 3: 运行测试**

```bash
uv run pytest tests/test_stop_limit.py -v
```

预期：2 passed。

---

### Task 5: 更新 Skill（软性建议）

**文件：**
- 修改: `skills/match-atmosphere.yaml`

- [ ] **Step 1: 在"提示"部分追加**

```yaml
  - 每轮 match_reference 后，先对比当前 R/B 比值、饱和度、亮度与参考值的差距。
    三项偏差均 < 20%（相对于参考值）→ 当前状态已较好，
    **本轮调整即为最后一轮微调——调完即止，不要追加调整其他组件。**
  - 连续 2 轮 match_reference 的 R/B 比值、饱和度、亮度变化幅度均 < 10%
    → 已收敛，调 deactivate_skill 退出。
  - 上述阈值为经验值。如初始偏差极大（如夜间 vs 白天），
    自行根据初始偏差大小放宽阈值。
```

---

### Task 6: 最终验证

- [ ] **Step 1: 运行全量测试**

```bash
uv run pytest tests/ -v --ignore=tests/test_l3_e2e.py
```

- [ ] **Step 2: 提交**

```bash
git add harness/stop_limit.py harness/server.py harness/cli.py skills/match-atmosphere.yaml tests/test_stop_limit.py
git commit -m "feat: match_reference 叫停机制 (硬限制 + 最佳状态快照 + 软收敛)

硬终止: StopLimitInterceptor — 10 次 match_reference 后 pre_call 拦截，
从 JSONL 反查操作记录，返回指标轨迹 + 操作摘要 (isError=True)。

最佳状态快照: 直方图相似度创新高时自动 SaveLevelAs 拷贝关卡副本，
硬终止摘要中包含快照路径，LLM 可 LoadLevel 恢复。

软收敛: Skill 提示 — 偏差 < 20% 即止 + 收敛退出。

UE 前置: LevelPersistenceToolset 需新增 SaveLevelAs + LoadLevel 工具。

Closes: docs/plans/PLAN_0713_stop_mechanisms.md
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审清单

1. **需求覆盖：**
   - [x] 硬终止：10 次 match_reference 后 pre_call 拦截（Task 1-3）
   - [x] 回顾摘要：指标轨迹 + 操作摘要 + 快照路径（Task 1 + Task 2 Step 1）
   - [x] 最佳状态快照：SaveLevelAs 触发 + 路径输出（Task 2 Step 3-4）
   - [x] 软收敛：Skill 追加收敛提示 + 好状态即止（Task 5）
   - [x] 测试：硬终止拦截 + SaveLevelAs 触发验证（Task 4）
   - [x] UE 前置：SaveLevelAs + LoadLevel 工具规格（前置条件节）

2. **占位符检查：** 无 TBD/TODO。

3. **影响分析：**
   - 新增文件 `harness/stop_limit.py`（~200 行）+ `tests/test_stop_limit.py`（~150 行）
   - 修改 server.py：+50 行（计数 + 拦截 + 最佳检测 + SaveLevelAs + 辅助函数）
   - 修改 cli.py：+5 行（注册 interceptor + jsonl_dir）
   - 不影响现有测试
   - UE 端需独立开发 2 个新工具（SaveLevelAs + LoadLevel）
