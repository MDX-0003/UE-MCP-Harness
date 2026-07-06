"""Branch Mark 评分引擎 — 离线分析 Harness JSONL 日志，输出评估指标。

用法:
    # 单次评分
    python -m branch_mark.score path/to/tool_calls.jsonl

    # A/B 对比
    python -m branch_mark.score baseline.jsonl candidate.jsonl

    # CLI 入口
    branch-mark score tool_calls.jsonl
    branch-mark score baseline.jsonl after.jsonl

指标检测基于 tool call 序列模式匹配，不依赖 UE 连接。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---- 指标定义 ----

@dataclass
class Metric:
    id: str
    label: str
    description: str
    score_type: str = "bool"  # bool | float | int


METRICS: list[Metric] = [
    Metric("l2_readback",      "L2 读回验证",
           "写操作后是否调了 get_actor_transform / get_properties 确认写入值"),
    Metric("targeted_question","针对性提问",
           "截图工具是否携带了具体验证问题（非泛泛的 '描述场景'）"),
    Metric("question_specificity", "问题具体度",
           "提问是否包含 Actor 名、属性名等可验证细节", "float"),
    Metric("follow_up_used",   "追问使用",
           "是否调了 vision_ask 深入分析"),
    Metric("session_closed",   "Session 关闭",
           "是否调了 vision_reset 关闭 Session"),
    Metric("loop_complete",    "闭环完成",
           "完整走了 L2读回→针对性截图→追问→关闭 全流程"),
    Metric("total_tool_calls", "总 tool call 数",
           "会话中工具调用总数（含 Harness 自有工具）", "int"),
    Metric("vision_calls",     "Vision API 调用次数",
           "vision_screenshot + vision_ask 的总次数", "int"),
]


# ---- 模式检测 ----

_WRITE_TOOLS = frozenset({
    "set_actor_transform", "set_properties", "set_label",
    "add_to_scene_from_class", "add_to_scene_from_asset",
    "remove_from_scene", "add_tag", "remove_tag",
    "add_component", "remove_component",
})
_READ_TOOLS = frozenset({
    "get_actor_transform", "get_properties", "get_actor_bounds",
    "get_label", "get_tags", "get_components",
})
_SCREENSHOT_TOOLS = frozenset({
    "vision_screenshot",
    "CaptureEditorImage", "CaptureAssetImage",
})


def _short_name(full: str) -> str:
    """ToolsetRegistry.EditorAppToolset.CaptureAssetImage → CaptureAssetImage"""
    return full.split(".")[-1] if "." in full else full


def _extract_actor_from_args(args: Any) -> str:
    """从 tool args 中提取 Actor 名。"""
    if not isinstance(args, dict):
        return ""
    actor = args.get("actor", {})
    if isinstance(actor, dict):
        return actor.get("name", "")
    if isinstance(actor, str):
        return actor
    return args.get("name", args.get("actor_name", ""))


def _question_has_specifics(question: str, known_actors: set[str]) -> float:
    """评估问题的具体度（0.0-1.0）。

    加分项：包含 Actor 名、位置词（对齐/悬浮/地面）、属性词（颜色/旋转/大小）
    """
    if not question or not question.strip():
        return 0.0
    q = question.lower()
    score = 0.0

    # Actor 名匹配（权重最高）
    actor_hits = sum(1 for a in known_actors if a.lower() in q)
    if actor_hits > 0:
        score += min(0.5, actor_hits * 0.15)

    # 属性关键词
    property_keywords = [
        "位置", "旋转", "颜色", "大小", "对齐", "悬浮", "地面",
        "阴影", "光源", "灯光", "方向", "亮度", "强度", "色温",
        "location", "rotation", "color", "light", "shadow",
        "transform", "scale", "intensity",
    ]
    prop_hits = sum(1 for kw in property_keywords if kw in q)
    score += min(0.3, prop_hits * 0.05)

    # 比较/验证关键词（说明在做判断而非描述）
    verify_keywords = [
        "是否", "有没有", "对不对", "正确", "对齐", "一致",
        "符合", "预期", "确认", "验证", "检查",
    ]
    verify_hits = sum(1 for kw in verify_keywords if kw in q)
    score += min(0.2, verify_hits * 0.05)

    return min(1.0, score)


def _is_generic_describe(question: str) -> bool:
    """判断提问是否属于泛泛的 '描述场景' 类。"""
    if not question or not question.strip():
        return True
    generic_patterns = [
        "描述", "describe", "怎么样", "什么样子", "看起来",
        "截图内容", "场景内容", "现在是什么",
    ]
    q = question.lower()
    return any(p in q for p in generic_patterns)


# ---- 评分引擎 ----

@dataclass
class ScoreResult:
    """一次评分的完整结果。"""
    source: str  # 文件路径
    metrics: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def total(self) -> float:
        """加权总分（0-100）。"""
        weights = {
            "l2_readback": 15,
            "targeted_question": 20,
            "question_specificity": 15,
            "follow_up_used": 10,
            "session_closed": 15,
            "loop_complete": 25,
        }
        total = 0.0
        for k, w in weights.items():
            v = self.metrics.get(k, 0)
            if isinstance(v, bool):
                total += w if v else 0
            elif isinstance(v, (int, float)):
                total += v * w if k == "question_specificity" else 0
        return total


def score_jsonl(path: Path | str) -> ScoreResult:
    """读取 JSONL 日志文件，返回评分结果。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"日志文件不存在: {path}")

    # 1. 解析所有工具调用
    calls: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not calls:
        return ScoreResult(source=str(path))

    result = ScoreResult(source=str(path), tool_calls=calls)

    # 2. 收集已知 Actor 名（兼容新旧两种 JSONL 格式）
    known_actors: set[str] = set()
    for c in calls:
        tool_name = c.get("tool_name") or c.get("tool", "")
        short = _short_name(tool_name)
        if short in ("find_actors", "get_visible_actors"):
            output = c.get("tool_output") or c.get("output", "") or ""
            for m in re.finditer(r'["\'](\w+)["\']', str(output)):
                known_actors.add(m.group(1))
        if short in _READ_TOOLS or short in _WRITE_TOOLS:
            args = c.get("tool_input") or c.get("input", {})
            name = _extract_actor_from_args(args)
            if name:
                known_actors.add(name)

    # 3. 检测各项指标

    # L2 读回：write → read 序列
    l2_readback = False
    last_write_actor = ""
    for c in calls:
        tool_name = c.get("tool_name") or c.get("tool", "")
        short = _short_name(tool_name)
        args = c.get("tool_input") or c.get("input", {})
        if short in _WRITE_TOOLS:
            last_write_actor = _extract_actor_from_args(args)
        elif short in _READ_TOOLS and last_write_actor:
            read_actor = _extract_actor_from_args(args)
            if read_actor and read_actor == last_write_actor:
                l2_readback = True
                break
    result.metrics["l2_readback"] = l2_readback

    # 针对性提问 + 具体度
    targeted = False
    specificity = 0.0
    for c in calls:
        tool_name = c.get("tool_name") or c.get("tool", "")
        short = _short_name(tool_name)
        args = c.get("tool_input") or c.get("input", {})
        if short in _SCREENSHOT_TOOLS:
            question = args.get("question", "") if isinstance(args, dict) else ""
            if question and not _is_generic_describe(question):
                targeted = True
                specificity = max(specificity, _question_has_specifics(question, known_actors))
        if short == "vision_ask":
            question = args.get("question", "") if isinstance(args, dict) else ""
            if question and not _is_generic_describe(question):
                targeted = True
                specificity = max(specificity, _question_has_specifics(question, known_actors))
    result.metrics["targeted_question"] = targeted
    result.metrics["question_specificity"] = round(specificity, 2)

    # 追问
    follow_up = any(
        _short_name(c.get("tool_name") or c.get("tool", "")) == "vision_ask"
        for c in calls
    )
    result.metrics["follow_up_used"] = follow_up

    # Session 关闭
    closed = any(
        _short_name(c.get("tool_name") or c.get("tool", "")) == "vision_reset"
        for c in calls
    )
    result.metrics["session_closed"] = closed

    # 闭环完成
    result.metrics["loop_complete"] = (
        l2_readback and targeted and follow_up and closed
    )

    # 统计
    result.metrics["total_tool_calls"] = len(calls)
    result.metrics["vision_calls"] = sum(
        1 for c in calls
        if _short_name(c.get("tool_name") or c.get("tool", ""))
        in ("vision_screenshot", "vision_ask")
    )

    return result


# ---- 输出渲染 ----

def compare(a: ScoreResult, b: ScoreResult) -> str:
    """生成 A/B 对比表。"""
    lines = []
    header = f"{'指标':<24} {'Before':<10} {'After':<10} {'Δ':>8}"
    lines.append(header)
    lines.append("-" * 56)

    for m in METRICS:
        va = a.metrics.get(m.id, "—")
        vb = b.metrics.get(m.id, "—")

        if m.score_type == "bool":
            sa = "Yes" if va else "No"
            sb = "Yes" if vb else "No"
            delta = ""
            if va != vb:
                delta = "+1" if vb else "-1"
        elif m.score_type == "float":
            sa = f"{va:.2f}" if isinstance(va, (int, float)) else str(va)
            sb = f"{vb:.2f}" if isinstance(vb, (int, float)) else str(vb)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                delta = f"{vb - va:+.2f}"
            else:
                delta = ""
        else:
            sa = str(va)
            sb = str(vb)
            if isinstance(va, int) and isinstance(vb, int):
                delta = f"{vb - va:+d}"
            else:
                delta = ""

        lines.append(f"{m.label:<24} {sa:<10} {sb:<10} {delta:>8}")

    # 总分
    lines.append("-" * 56)
    lines.append(f"{'加权总分':<24} {a.total:<10.1f} {b.total:<10.1f} {b.total - a.total:>+8.1f}")

    return "\n".join(lines)


def render_single(result: ScoreResult) -> str:
    """渲染单次评分结果。"""
    lines = [f"评分: {result.source}", "-" * 40]
    for m in METRICS:
        v = result.metrics.get(m.id, "—")
        if m.score_type == "bool":
            lines.append(f"  {m.label:<20} {'Yes' if v else 'No'}")
        elif m.score_type == "float":
            lines.append(f"  {m.label:<20} {v:.2f}" if isinstance(v, float) else f"  {m.label:<20} {v}")
        else:
            lines.append(f"  {m.label:<20} {v}")
    lines.append(f"  {'加权总分':<20} {result.total:.1f}/100")
    return "\n".join(lines)


# ---- CLI ----

def main(argv: Sequence[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] not in ("show", "score"):
        print("用法: branch-mark show <task>  或  branch-mark score <jsonl> [baseline.jsonl]")
        sys.exit(1)

    if argv[0] == "show":
        import yaml

        task_name = argv[1] if len(argv) > 1 else ""
        tasks_dir = Path(__file__).resolve().parent / "tasks"
        task_file = tasks_dir / f"{task_name}.yaml"
        if not task_file.exists():
            print(f"任务 '{task_name}' 不存在。可用任务：")
            for f in sorted(tasks_dir.glob("*.yaml")):
                print(f"  {f.stem}")
            sys.exit(1)
        data = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        print("=" * 60)
        print(f"任务: {data.get('name', task_name)}")
        print(f"描述: {data.get('description', '')}")
        print("=" * 60)
        print()
        print("--- 场景要求 ---")
        print(data.get("scene_requirement", "").strip())
        print()
        print("--- 指令（复制给 LLM）---")
        print(data.get("instruction", "").strip())
        print()
        print("--- 评估指标 ---")
        for m in data.get("metrics", []):
            print(f"  {m.get('id')}: {m.get('label')} — {m.get('description')}")

    elif argv[0] == "score":
        if len(argv) < 2:
            print("用法: branch-mark score <jsonl> [baseline.jsonl]")
            sys.exit(1)

        jsonl_path = Path(argv[1])
        if not jsonl_path.exists():
            print(f"日志文件不存在: {jsonl_path}")
            sys.exit(1)

        if len(argv) >= 3:
            baseline_path = Path(argv[2])
            if not baseline_path.exists():
                print(f"Baseline 文件不存在: {baseline_path}")
                sys.exit(1)
            before = score_jsonl(baseline_path)
            after = score_jsonl(jsonl_path)
            print(compare(before, after))
        else:
            result = score_jsonl(jsonl_path)
            print(render_single(result))


if __name__ == "__main__":
    main()
