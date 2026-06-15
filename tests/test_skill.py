"""测试 harness.context.skill_registry — Skill CRUD + 匹配 + 验证。"""

from pathlib import Path

import pytest
import yaml

from harness.context.skill_registry import (
    SkillInfo,
    SkillRegistry,
    _normalize_list,
    validate_skill,
    BUILTIN_SKILL_TEMPLATE,
)


# ---- pyyaml 解析 ----

class TestPyyamlParsing:
    """测试 pyyaml 解析各种边界情况。"""

    def test_basic_fields(self) -> None:
        text = "name: test\nversion: 1.0\nenabled: true"
        result = yaml.safe_load(text)
        assert result["name"] == "test"
        assert result["version"] == 1.0
        assert result["enabled"] is True

    def test_literal_block(self) -> None:
        text = "steps: |\n  1. first\n  2. second\n  3. third"
        result = yaml.safe_load(text)
        assert "1. first" in result["steps"]
        assert "2. second" in result["steps"]

    def test_list_field(self) -> None:
        text = "triggers:\n  - dusk\n  - sunset\n  - 黄昏"
        result = yaml.safe_load(text)
        assert "triggers" in result
        assert result["triggers"] == ["dusk", "sunset", "黄昏"]

    def test_comments_ignored(self) -> None:
        text = "# this is a comment\nname: test\n# another comment\ndescription: hello"
        result = yaml.safe_load(text)
        assert result["name"] == "test"
        assert result["description"] == "hello"

    def test_string_with_colon(self) -> None:
        """pyyaml 正确处理含冒号的引号字符串。"""
        text = 'expected: "场景: 黄昏光照"'
        result = yaml.safe_load(text)
        assert result["expected"] == "场景: 黄昏光照"

    def test_nested_verification(self) -> None:
        text = "verification:\n  type: screenshot\n  expected: test\n  tolerance: 0.7"
        result = yaml.safe_load(text)
        assert result["verification"]["type"] == "screenshot"
        assert result["verification"]["tolerance"] == 0.7


class TestNormalizeList:
    """测试 _normalize_list 函数。"""

    def test_yaml_list(self) -> None:
        assert _normalize_list(["a", "b", "c"]) == ["a", "b", "c"]

    def test_comma_string(self) -> None:
        assert _normalize_list("a, b, c") == ["a", "b", "c"]

    def test_quoted_items(self) -> None:
        assert _normalize_list(['"item one"', "'item two'"]) == ["item one", "item two"]

    def test_empty(self) -> None:
        assert _normalize_list([]) == []
        assert _normalize_list("") == []
        assert _normalize_list(None) == []


# ---- validate_skill ----

class TestValidateSkill:
    """测试 Skill YAML 格式验证。"""

    VALID_SKILL = """name: test-skill
description: "test description"
triggers:
  - test
  - example
tools_allowlist:
  - SceneTools.find_actors
  - ActorTools.set_actor_transform
steps: |
  1. step one
  2. step two
verification:
  type: screenshot
  expected: "test"
  tolerance: 0.7
"""

    def test_valid_skill(self) -> None:
        errors = validate_skill(self.VALID_SKILL)
        assert errors == []

    def test_missing_name(self) -> None:
        yaml = self.VALID_SKILL.replace("name: test-skill", "name:")
        errors = validate_skill(yaml)
        assert any("name" in e for e in errors)

    def test_missing_triggers(self) -> None:
        yaml = self.VALID_SKILL.replace("triggers:", "triggersx:")
        errors = validate_skill(yaml)
        assert any("triggers" in e for e in errors)

    def test_missing_steps(self) -> None:
        yaml = self.VALID_SKILL.replace("steps: |", "stepsx:")
        errors = validate_skill(yaml)
        assert any("steps" in e for e in errors)

    def test_empty_triggers(self) -> None:
        yaml = self.VALID_SKILL.replace("  - test\n  - example", "")
        errors = validate_skill(yaml)
        assert any("triggers" in e for e in errors)


# ---- SkillRegistry ----

class TestSkillRegistry:
    """测试 Skill 注册表 CRUD + 匹配。"""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> SkillRegistry:
        return SkillRegistry(skills_dir=tmp_path)

    def test_load_empty_dir(self, registry: SkillRegistry, tmp_path: Path) -> None:
        registry.load_skills()
        assert registry.list_skills() == []

    def test_list_skills(self, registry: SkillRegistry) -> None:
        registry.save_skill("evening-lighting", TestValidateSkill.VALID_SKILL)
        skills = registry.list_skills()
        assert len(skills) == 1
        assert skills[0].name == "test-skill"

    def test_save_and_load(self, registry: SkillRegistry) -> None:
        info = registry.save_skill("my-skill", TestValidateSkill.VALID_SKILL)
        assert info.name == "test-skill"  # name from YAML overrides filename
        assert info.steps_count == 2

        # 重新加载
        registry2 = SkillRegistry(skills_dir=registry.skills_dir)
        registry2.load_skills()
        assert len(registry2.list_skills()) == 1

    def test_delete_skill(self, registry: SkillRegistry) -> None:
        registry.save_skill("temp-skill", TestValidateSkill.VALID_SKILL)
        assert len(registry.list_skills()) == 1

        result = registry.delete_skill("test-skill")
        assert result is True
        assert registry.list_skills() == []

    def test_delete_nonexistent(self, registry: SkillRegistry) -> None:
        result = registry.delete_skill("nonexistent")
        assert result is False

    def test_get_skill(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        info = registry.get_skill("test-skill")
        assert info is not None
        assert info.name == "test-skill"

        assert registry.get_skill("nonexistent") is None

    def test_load_skill_yaml(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        yaml_text = registry.load_skill_yaml("test-skill")
        assert yaml_text is not None
        assert "name: test-skill" in yaml_text

        assert registry.load_skill_yaml("nonexistent") is None

    def test_create_template(self, registry: SkillRegistry) -> None:
        path = registry.create_template("coffee-shop")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "name: coffee-shop" in content
        assert "triggers:" in content

    def test_save_invalid_skill(self, registry: SkillRegistry) -> None:
        with pytest.raises(ValueError, match="验证失败"):
            registry.save_skill("bad", "name: bad\n# missing triggers and steps")

    # ---- 匹配 ----

    def test_match_by_name(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("test-skill")
        assert len(matches) == 1
        assert matches[0].name == "test-skill"

    def test_match_by_name_fragment(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("test")
        assert len(matches) == 1

    def test_match_by_description(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("test description")
        assert len(matches) == 1

    def test_match_by_trigger(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("example")
        assert len(matches) == 1

    def test_match_case_insensitive(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("TEST-SKILL")
        assert len(matches) == 1

    def test_match_no_result(self, registry: SkillRegistry) -> None:
        registry.save_skill("s1", TestValidateSkill.VALID_SKILL)
        matches = registry.match_skill("nonexistent-query")
        assert matches == []

    def test_match_multiple(self, registry: SkillRegistry) -> None:
        registry.save_skill("a", TestValidateSkill.VALID_SKILL)
        # 改名保存第二个
        s2 = TestValidateSkill.VALID_SKILL.replace("name: test-skill", "name: test-skill-2")
        registry.save_skill("b", s2)
        matches = registry.match_skill("test")
        assert len(matches) == 2

    def test_match_empty_query(self, registry: SkillRegistry) -> None:
        registry.save_skill("a", TestValidateSkill.VALID_SKILL)
        assert registry.match_skill("") == []
        assert registry.match_skill("  ") == []


# ---- 内置 Skill 模板 ----

class TestBuiltinTemplate:
    """测试内置模板安装。"""

    def test_template_has_required_fields(self) -> None:
        """模板 YAML 应包含所有必填字段。"""
        errors = validate_skill(BUILTIN_SKILL_TEMPLATE.format(name="test"))
        assert errors == []