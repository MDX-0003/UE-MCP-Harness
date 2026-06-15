"""测试 harness.verification 模块 — Vision Sub-Agent + 判决解析 + .vision.env 配置。"""

import json
import os
from pathlib import Path

import pytest

from harness.config import Config
from harness.verification.config import (
    VisionConfig,
    VISION_ENV_FILE,
    DEFAULT_VISION_API_BASE_URL,
    DEFAULT_VISION_MODEL,
    load_vision_env,
    create_vision_env_template,
)
from harness.verification.vision_agent import (
    VisionSubAgent,
    VisionVerdict,
    _parse_verdict,
)


# ---- Config: vision_api_base_url ----

class TestVisionConfigField:
    """测试 Config 中新增的 vision_api_base_url 字段。"""

    def test_default_base_url(self) -> None:
        cfg = Config()
        assert "token-plan-cn.xiaomimimo.com" in cfg.vision_api_base_url

    def test_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_VISION_API_BASE_URL", "https://custom.example.com/v1")
        cfg = Config.from_env()
        assert cfg.vision_api_base_url == "https://custom.example.com/v1"

    def test_base_url_default_when_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 确保环境变量未设置
        monkeypatch.delenv("HARNESS_VISION_API_BASE_URL", raising=False)
        cfg = Config.from_env()
        assert cfg.vision_api_base_url == DEFAULT_VISION_API_BASE_URL

    def test_api_key_field_exists(self) -> None:
        cfg = Config(vision_api_key="sk-ant-test")
        assert cfg.vision_api_key == "sk-ant-test"

    def test_vision_model_default(self) -> None:
        cfg = Config()
        assert cfg.vision_model  # 有值即可（用户可能自定义）


# ---- VisionConfig dataclass ----

class TestVisionConfigDataclass:
    """测试独立 VisionConfig。"""

    def test_defaults(self) -> None:
        vc = VisionConfig()
        assert vc.api_key == ""
        assert vc.api_base_url == DEFAULT_VISION_API_BASE_URL
        assert vc.model == DEFAULT_VISION_MODEL
        assert vc.max_size == (1024, 768)

    def test_custom_values(self) -> None:
        vc = VisionConfig(
            api_key="sk-ant-custom",
            api_base_url="https://proxy.example.com/anthropic",
            model="claude-opus-4-8",
        )
        assert vc.api_key == "sk-ant-custom"
        assert vc.api_base_url == "https://proxy.example.com/anthropic"


# ---- .vision.env 加载 ----

class TestLoadVisionEnv:
    """测试 .vision.env 文件的加载。

    注意：load_vision_env() 仅在环境变量尚未设置时写入 os.environ。
    各测试需独立清理环境变量避免相互污染。
    """

    VISION_VARS = [
        "HARNESS_VISION_API_KEY",
        "HARNESS_VISION_API_BASE_URL",
        "HARNESS_VISION_MODEL",
    ]

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """每个测试前清除 Vision 环境变量，保证隔离。"""
        for var in self.VISION_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_load_from_tmp_dir(self, tmp_path: Path) -> None:
        """从临时目录加载 .vision.env。"""
        env_file = tmp_path / VISION_ENV_FILE
        env_file.write_text(
            'HARNESS_VISION_API_KEY=sk-ant-from-file\n'
            'HARNESS_VISION_API_BASE_URL=https://file.example.com/anthropic\n'
            'HARNESS_VISION_MODEL=claude-fable-5\n',
            encoding="utf-8",
        )

        loaded = load_vision_env(project_root=tmp_path)
        assert loaded is True
        assert os.environ["HARNESS_VISION_API_KEY"] == "sk-ant-from-file"
        assert os.environ["HARNESS_VISION_API_BASE_URL"] == "https://file.example.com/anthropic"
        assert os.environ["HARNESS_VISION_MODEL"] == "claude-fable-5"

    def test_env_var_not_overwritten(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """已存在的环境变量不被 .vision.env 覆盖（CLI 参数优先级更高）。"""
        monkeypatch.setenv("HARNESS_VISION_API_KEY", "sk-ant-from-cli")

        env_file = tmp_path / VISION_ENV_FILE
        env_file.write_text(
            'HARNESS_VISION_API_KEY=sk-ant-from-file\n',
            encoding="utf-8",
        )

        load_vision_env(project_root=tmp_path)
        # CLI 设置的值不被文件覆盖
        assert os.environ["HARNESS_VISION_API_KEY"] == "sk-ant-from-cli"

    def test_no_file_returns_false(self, tmp_path: Path) -> None:
        """目录中没有 .vision.env 时返回 False。"""
        loaded = load_vision_env(project_root=tmp_path)
        assert loaded is False

    def test_comments_and_blanks_ignored(self, tmp_path: Path) -> None:
        """注释和空行被正确跳过。"""
        env_file = tmp_path / VISION_ENV_FILE
        env_file.write_text(
            '# 这是注释\n'
            '\n'
            'HARNESS_VISION_API_KEY=sk-ant-valid\n'
            '# 另一行注释\n',
            encoding="utf-8",
        )
        loaded = load_vision_env(project_root=tmp_path)
        assert loaded is True
        assert os.environ["HARNESS_VISION_API_KEY"] == "sk-ant-valid"

    def test_quoted_values_stripped(self, tmp_path: Path) -> None:
        """引号包裹的值被正确剥离。"""
        env_file = tmp_path / VISION_ENV_FILE
        env_file.write_text(
            'HARNESS_VISION_API_KEY="sk-ant-quoted"\n',
            encoding="utf-8",
        )
        load_vision_env(project_root=tmp_path)
        assert os.environ["HARNESS_VISION_API_KEY"] == "sk-ant-quoted"


# ---- create_vision_env_template ----

class TestCreateVisionEnvTemplate:
    """测试 .vision.env 模板创建。"""

    def test_creates_template(self, tmp_path: Path) -> None:
        path = create_vision_env_template(project_root=tmp_path)
        assert path.exists()
        assert path.name == VISION_ENV_FILE
        content = path.read_text(encoding="utf-8")
        assert "HARNESS_VISION_API_KEY=" in content
        assert "HARNESS_VISION_API_BASE_URL=" in content
        assert DEFAULT_VISION_API_BASE_URL in content

    def test_existing_file_not_overwritten(self, tmp_path: Path) -> None:
        """已存在的 .vision.env 不被覆盖。"""
        env_file = tmp_path / VISION_ENV_FILE
        env_file.write_text("HARNESS_VISION_API_KEY=existing-key", encoding="utf-8")

        path = create_vision_env_template(project_root=tmp_path)
        content = path.read_text(encoding="utf-8")
        assert content == "HARNESS_VISION_API_KEY=existing-key"


# ---- _parse_verdict ----

class TestParseVerdict:
    """测试 Vision model 响应的解析。"""

    def test_plain_json(self) -> None:
        raw = '{"pass": true, "reason": "光照正确", "adjustment": "无需调整"}'
        verdict = _parse_verdict(raw)
        assert verdict.pass_ is True
        assert verdict.reason == "光照正确"
        assert verdict.adjustment == "无需调整"
        assert verdict.need_more_info is False

    def test_json_in_markdown(self) -> None:
        raw = '```json\n{"pass": false, "reason": "太暗", "adjustment": "增加亮度"}\n```'
        verdict = _parse_verdict(raw)
        assert verdict.pass_ is False
        assert verdict.reason == "太暗"
        assert verdict.adjustment == "增加亮度"

    def test_need_more_info(self) -> None:
        raw = '{"need_more_info": true, "question": "灯光角度是多少？"}'
        verdict = _parse_verdict(raw)
        assert verdict.need_more_info is True
        assert verdict.question == "灯光角度是多少？"

    def test_malformed_json(self) -> None:
        raw = "这里是一些文本 pass true 然后 blah blah"
        verdict = _parse_verdict(raw)
        # 应优雅降级而非抛异常
        assert isinstance(verdict, VisionVerdict)
        assert verdict.raw_response == raw


# ---- VisionSubAgent ----

class TestVisionSubAgent:
    """测试 Vision Sub-Agent 核心逻辑。"""

    @pytest.fixture
    def config(self) -> Config:
        """使用测试 key 的配置——会走 mock 路径。"""
        return Config(vision_api_key="test-key")

    async def test_basic_check_returns_verdict(self, config: Config) -> None:
        agent = VisionSubAgent(config)
        tiny_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        verdict = await agent.check(tiny_png_b64, "场景应为黄昏", tolerance=0.7)
        assert isinstance(verdict, VisionVerdict)
        # mock 模式默认通过
        assert "[MOCK]" in verdict.reason

    async def test_history_accumulates(self, config: Config) -> None:
        agent = VisionSubAgent(config)
        tiny_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        await agent.check(tiny_png_b64, "场景应为黄昏")
        assert agent.history_length == 2  # user + assistant
        assert agent.call_count == 1

    async def test_reset_clears_history(self, config: Config) -> None:
        agent = VisionSubAgent(config)
        tiny_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        await agent.check(tiny_png_b64, "test")
        agent.reset()
        assert agent.history_length == 0
        assert agent.call_count == 0

    async def test_continue_with_info(self, config: Config) -> None:
        agent = VisionSubAgent(config)
        tiny_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        first = await agent.check(tiny_png_b64, "test")

        verdict = await agent.continue_with_info("DirectionalLight 角度为 15 度")
        assert isinstance(verdict, VisionVerdict)
        assert agent.call_count == 2

    async def test_extra_context_injected(self, config: Config) -> None:
        agent = VisionSubAgent(config)
        tiny_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        verdict = await agent.check(
            tiny_png_b64,
            "场景应为黄昏",
            tolerance=0.9,
            extra_context="当前 DirectionalLight 旋转角度为 (60, 0, 0)",
        )
        assert isinstance(verdict, VisionVerdict)

    def test_config_has_custom_base_url(self) -> None:
        """验证 VisionSubAgent 用的 Config 携带自定义 base_url。"""
        cfg = Config(
            vision_api_key="test-key",
            vision_api_base_url="https://token-plan-cn.xiaomimimo.com/anthropic",
        )
        assert cfg.vision_api_base_url == "https://token-plan-cn.xiaomimimo.com/anthropic"
        assert cfg.vision_api_key == "test-key"
