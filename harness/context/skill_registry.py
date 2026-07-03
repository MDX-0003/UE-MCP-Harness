"""Skill 注册表 — YAML 文件 CRUD + 匹配引擎 (Issue 005)

Skill 以 YAML 文件存储在 ~/.ue-harness/skills/，格式参见 docs/issues/005-skill-system.md。

核心功能：
  load_skills()         扫描目录，加载所有 Skill YAML
  list_skills()         列出 (name, description, triggers)
  match_skill(query)    按 name/description/trigger 片段匹配
  save_skill(name,yaml) 验证 → 写入 → 刷新缓存
  delete_skill(name)    删除文件 → 刷新缓存
  validate_skill(yaml)  格式验证（必填字段检查）

YAML 解析使用 pyyaml 库。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("harness.context.skill_registry")

# 内置 Skill 模板（随 Harness 分发）
BUILTIN_SKILL_TEMPLATE = """# {name} Skill
name: {name}
description: "任务描述"
triggers:
  - 关键词1
  - 关键词2
tools_allowlist:
  - SceneTools.find_actors
steps: |
  1. 第一步
  2. 第二步
  3. 第三步

verification:
  type: screenshot
  expected: "预期效果描述"
  tolerance: 0.7
"""

# 必填字段
REQUIRED_FIELDS = {"name", "triggers", "steps"}


@dataclass
class SkillInfo:
    """Skill 的快照信息（用于列表和匹配）。"""
    name: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    tools_allowlist: list[str] = field(default_factory=list)
    steps_count: int = 0
    file_path: Path | None = None


@dataclass
class SkillRegistry:
    """Skill 注册表——管理 ~/.ue-harness/skills/ 下的所有 Skill YAML。

    内部维护一个 {name: SkillInfo} 缓存，load_skills() 刷新。
    """

    skills_dir: Path = field(default_factory=lambda: Path.home() / ".ue-harness" / "skills")
    _skills: dict[str, SkillInfo] = field(default_factory=dict)
    _loaded: bool = False

    # ---- 加载 ----

    def load_skills(self, install_builtin: bool | None = None) -> dict[str, SkillInfo]:
        """扫描 skills_dir，加载所有 .yaml 文件到内存缓存。

        格式错误的 YAML 跳过但不影响其他 Skill。
        Args:
            install_builtin: 是否安装内置 evening-lighting 模板。
                             None=仅在默认目录时安装，True=强制安装，False=不安装。
        Returns:
            {skill_name: SkillInfo} 映射。
        """
        self._skills.clear()
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # 首次运行：复制内置 evening-lighting 模板（仅在默认用户目录）
        if install_builtin is None:
            install_builtin = (self.skills_dir == Path.home() / ".ue-harness" / "skills")
        if install_builtin:
            _ensure_builtin_skill(self.skills_dir)

        for file_path in sorted(self.skills_dir.glob("*.yaml")):
            try:
                info = _parse_skill_file(file_path)
                if info:
                    self._skills[info.name] = info
            except Exception as e:
                logger.warning("跳过无效 Skill 文件 %s: %s", file_path.name, e)

        self._loaded = True
        logger.info("已加载 %d 个 Skill", len(self._skills))
        return self._skills

    # ---- 查询 ----

    def list_skills(self) -> list[SkillInfo]:
        if not self._loaded:
            self.load_skills()
        return sorted(self._skills.values(), key=lambda s: s.name)

    def match_skill(self, query: str) -> list[SkillInfo]:
        """按 name / description / triggers 片段匹配 Skill。

        Args:
            query: 用户意图或 LLM 传入的 name/description 片段。

        Returns:
            匹配的 SkillInfo 列表（可能 0/1/多个）。
            匹配规则：对每个 Skill 的 name + description + triggers 做子串匹配（大小写不敏感）。
        """
        if not self._loaded:
            self.load_skills()

        q = query.lower().strip()
        if not q:
            return []

        matches: list[SkillInfo] = []
        for info in self._skills.values():
            # 搜索所有可匹配字段
            searchable = [info.name.lower(), info.description.lower()]
            searchable.extend(t.lower() for t in info.triggers)
            if any(q in field for field in searchable):
                matches.append(info)

        return matches

    def get_skill(self, name: str) -> SkillInfo | None:
        """精确按 name 获取 Skill。"""
        if not self._loaded:
            self.load_skills()
        return self._skills.get(name)

    def load_skill_yaml(self, name: str) -> str | None:
        """读取 Skill 的原始 YAML 内容（用于 LLM 阅读或编辑）。"""
        info = self.get_skill(name)
        if info and info.file_path and info.file_path.is_file():
            return info.file_path.read_text(encoding="utf-8")
        return None

    # ---- 写操作 ----

    def save_skill(self, name: str, yaml_content: str) -> SkillInfo:
        """保存 Skill YAML 到文件系统并刷新缓存。

        Args:
            name: Skill 名称（同时用作文件名 {name}.yaml）。
            yaml_content: 完整的 YAML 文本。

        Returns:
            解析后的 SkillInfo。

        Raises:
            ValueError: YAML 格式验证失败。
        """
        # 验证
        errors = validate_skill(yaml_content)
        if errors:
            raise ValueError(f"Skill YAML 格式验证失败: {'; '.join(errors)}")

        # 写入
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.skills_dir / f"{_safe_filename(name)}.yaml"
        file_path.write_text(yaml_content.strip() + "\n", encoding="utf-8")

        # 刷新缓存
        info = _parse_skill_file(file_path)
        if info:
            self._skills[info.name] = info
            logger.info("Skill 已保存: %s → %s", name, file_path)
            return info

        raise ValueError(f"无法解析已写入的 Skill 文件: {file_path}")

    def delete_skill(self, name: str) -> bool:
        """删除 Skill 文件并从缓存中移除。

        Returns:
            True 表示成功删除，False 表示 Skill 不存在。
        """
        info = self.get_skill(name)
        if info is None:
            return False

        if info.file_path and info.file_path.is_file():
            info.file_path.unlink()
            logger.info("Skill 已删除: %s → %s", name, info.file_path)

        self._skills.pop(name, None)
        return True

    def reload(self) -> int:
        """重新扫描 skills_dir，返回加载的 Skill 数量。"""
        count = len(self.load_skills(install_builtin=False))
        logger.info("已重新加载 %d 个 Skill", count)
        return count

    def create_template(self, name: str) -> Path:
        """创建 Skill YAML 模板文件（覆盖已有文件时警告但不阻止）。

        Returns:
            创建的模板文件路径。
        """
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.skills_dir / f"{_safe_filename(name)}.yaml"
        template = BUILTIN_SKILL_TEMPLATE.format(name=name)
        file_path.write_text(template, encoding="utf-8")

        # 刷新缓存
        info = _parse_skill_file(file_path)
        if info:
            self._skills[info.name] = info

        logger.info("Skill 模板已创建: %s", file_path)
        return file_path


# ---- YAML 解析（pyyaml）----

def _parse_skill_file(file_path: Path) -> SkillInfo | None:
    """解析单个 Skill YAML 文件为 SkillInfo。"""
    raw = file_path.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("YAML 解析失败 %s: %s", file_path.name, e)
        return None

    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name", file_path.stem)
    description = str(parsed.get("description", ""))
    triggers = _normalize_list(parsed.get("triggers", []))
    tools_allowlist = _normalize_list(parsed.get("tools_allowlist", []))
    steps = str(parsed.get("steps", ""))
    step_lines = [s.strip() for s in steps.splitlines() if s.strip()]

    return SkillInfo(
        name=name,
        description=description,
        triggers=triggers,
        tools_allowlist=tools_allowlist,
        steps_count=len(step_lines),
        file_path=file_path,
    )


def _normalize_list(val: Any) -> list[str]:
    """将 YAML 值标准化为字符串列表。"""
    if isinstance(val, list):
        return [str(v).strip().strip("\"'") for v in val if v is not None]
    if isinstance(val, str):
        return [v.strip().strip("\"'") for v in val.split(",") if v.strip()]
    return []


# ---- 验证 ----

def validate_skill(yaml_content: str) -> list[str]:
    """验证 Skill YAML 格式。

    Returns:
        错误信息列表。空列表 = 验证通过。
    """
    errors: list[str] = []
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return [f"YAML 语法错误: {e}"]

    if not isinstance(parsed, dict):
        return ["YAML 内容必须是 key-value 映射"]

    for field in REQUIRED_FIELDS:
        val = parsed.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"缺少必填字段: {field}")

    # 验证 triggers
    triggers = _normalize_list(parsed.get("triggers", []))
    if not triggers or all(not t for t in triggers):
        errors.append("triggers 列表不能为空")

    # 验证 tools_allowlist
    tools = _normalize_list(parsed.get("tools_allowlist", []))
    if not tools or all(not t for t in tools):
        errors.append("tools_allowlist 列表不能为空")

    return errors


# ---- 辅助 ----

def _safe_filename(name: str) -> str:
    """将 Skill name 转换为安全的文件名。"""
    return re.sub(r'[<>:"/\\|?*\s]', '-', name).strip('-')


def _ensure_builtin_skill(skills_dir: Path) -> None:
    """首次运行时，将内置 Skill YAML 复制到 skills_dir。"""
    builtin_names = ["evening-lighting", "scene-verification"]
    for name in builtin_names:
        target = skills_dir / f"{name}.yaml"
        if target.exists():
            continue

        # 从 Harness 包内查找内置 Skill
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "skills" / f"{name}.yaml",
            Path.cwd() / "skills" / f"{name}.yaml",
        ]
        for src in candidates:
            if src.is_file():
                target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("已复制内置 Skill: %s → %s", src, target)
                break
        else:
            # 如果找不到内置文件，创建占位
            logger.debug("未找到内置 Skill 文件 %s，创建空模板", name)
            target.write_text(BUILTIN_SKILL_TEMPLATE.format(name=name), encoding="utf-8")
