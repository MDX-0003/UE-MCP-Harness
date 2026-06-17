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
        cfg = Config(ue_host="127.0.0.1", ue_port=8000, ue_url_path="/mcp")
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
