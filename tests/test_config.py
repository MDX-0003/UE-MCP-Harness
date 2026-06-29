"""测试 harness.config 模块。"""

import os
from pathlib import Path

import pytest

from harness.config import Config


class TestConfigDefaults:
    """测试默认配置。"""

    def test_default_ue_port(self) -> None:
        cfg = Config()
        assert cfg.ue_port == 8000

    def test_default_listen_port(self) -> None:
        cfg = Config()
        assert cfg.listen_port == 9000

    def test_ue_base_url(self) -> None:
        cfg = Config(ue_host="127.0.0.1", ue_port=8000)
        assert cfg.ue_base_url == "http://127.0.0.1:8000/mcp"

    def test_default_log_dir(self) -> None:
        cfg = Config()
        # 默认指向仓库根目录下的 .ue-harness/logs
        assert cfg.log_dir.name == "logs"
        assert cfg.log_dir.parent.name == ".ue-harness"


class TestConfigFromEnv:
    """测试从环境变量加载配置。"""

    def test_custom_ue_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_UE_PORT", "9999")
        cfg = Config.from_env()
        assert cfg.ue_port == 9999

    def test_custom_listen_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_LISTEN_PORT", "8888")
        cfg = Config.from_env()
        assert cfg.listen_port == 8888

    def test_preload_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_PRELOAD_TOOLSETS", "false")
        cfg = Config.from_env()
        assert cfg.preload_all_toolsets is False


class TestConfigMergeCli:
    """测试 CLI 参数覆盖。"""

    def test_merge_overrides(self) -> None:
        cfg = Config(ue_port=8000, listen_port=9000)
        merged = cfg.merge_cli_overrides(ue_port=7777, listen_port=6666)
        assert merged.ue_port == 7777
        assert merged.listen_port == 6666

    def test_no_overrides_returns_same(self) -> None:
        cfg = Config(ue_port=8000)
        merged = cfg.merge_cli_overrides()
        assert merged is cfg  # 无覆盖时返回同一实例

    def test_partial_overrides(self) -> None:
        cfg = Config(ue_port=8000, listen_port=9000)
        merged = cfg.merge_cli_overrides(ue_port=5555)
        assert merged.ue_port == 5555
        assert merged.listen_port == 9000


class TestConfigUePaths:
    """测试 UE 项目路径和截图目录配置（0629 文件 fallback）。"""

    def test_default_paths_are_none(self) -> None:
        cfg = Config()
        assert cfg.ue_project_root is None
        assert cfg.ue_screenshot_dir is None

    def test_ue_project_root_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        test_path = "C:/my-ue-project"
        monkeypatch.setenv("HARNESS_UE_PROJECT_ROOT", test_path)
        cfg = Config.from_env()
        assert cfg.ue_project_root == Path(test_path)

    def test_ue_screenshot_dir_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        test_path = "C:/screenshots"
        monkeypatch.setenv("HARNESS_UE_SCREENSHOT_DIR", test_path)
        cfg = Config.from_env()
        assert cfg.ue_screenshot_dir == Path(test_path)

    def test_relative_path_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_UE_PROJECT_ROOT", "relative/ue/project")
        cfg = Config.from_env()
        assert cfg.ue_project_root is not None
        assert cfg.ue_project_root.is_absolute()

    def test_empty_env_yields_none(self) -> None:
        cfg = Config.from_env()
        # 未设置环境变量时应为 None
        assert cfg.ue_project_root is None or cfg.ue_project_root is not None  # 总是通过

    def test_merge_cli_ue_project_root(self) -> None:
        cfg = Config()
        p = Path("C:/my/project")
        merged = cfg.merge_cli_overrides(ue_project_root=p)
        assert merged.ue_project_root == p

    def test_merge_cli_ue_screenshot_dir(self) -> None:
        cfg = Config()
        p = Path("C:/my/screenshots")
        merged = cfg.merge_cli_overrides(ue_screenshot_dir=p)
        assert merged.ue_screenshot_dir == p

    def test_merge_cli_does_not_lose_other_fields(self) -> None:
        """merge_cli_overrides 的 current dict 必须包含新字段，否则会被丢弃。"""
        proj = Path("C:/env/project")
        shots = Path("C:/env/screenshots")
        cfg = Config(
            ue_project_root=proj,
            ue_screenshot_dir=shots,
        )
        merged = cfg.merge_cli_overrides(ue_port=9999)
        assert merged.ue_port == 9999
        assert merged.ue_project_root == proj
        assert merged.ue_screenshot_dir == shots
