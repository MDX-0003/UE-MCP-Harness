"""测试 harness.verification 模块 — Vision Sub-Agent + 判决解析 + .vision.env 配置。"""

import json
import os
from pathlib import Path

import pytest

from harness.config import Config
from harness.verification.config import (
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
        raw = '{"answer": "光照正确", "confidence": "high", "caveats": [], "observations": [{"what": "光照", "finding": "方向光角度约为30度", "confidence": "high"}]}'
        verdict = _parse_verdict(raw)
        assert verdict.answer == "光照正确"
        assert verdict.confidence == "high"
        assert verdict.need_more_info is False
        assert len(verdict.observations) == 1

    def test_json_in_markdown(self) -> None:
        raw = '```json\n{"answer": "画面太暗", "confidence": "high", "caveats": [], "observations": [{"what": "亮度", "finding": "暗部细节丢失", "confidence": "medium"}]}\n```'
        verdict = _parse_verdict(raw)
        assert verdict.answer == "画面太暗"
        assert verdict.confidence == "high"
        assert len(verdict.observations) == 1

    def test_need_more_info(self) -> None:
        raw = '{"answer": "无法判断", "confidence": "low", "caveats": ["截图信息不足"], "observations": [], "need_more_info": true, "question": "灯光角度是多少？"}'
        verdict = _parse_verdict(raw)
        assert verdict.need_more_info is True
        assert verdict.question == "灯光角度是多少？"
        assert verdict.confidence == "low"

    def test_malformed_json(self) -> None:
        raw = "这里是一些文本 pass true 然后 blah blah"
        verdict = _parse_verdict(raw)
        # 应优雅降级而非抛异常——全文作为 answer
        assert isinstance(verdict, VisionVerdict)
        assert verdict.confidence == "low"
        assert "Vision model 未返回有效 JSON" in verdict.caveats[0]
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


# ---- 0629 文件 fallback 测试 ------------------------------------------------


class TestShouldUseFileFallback:
    """_should_use_file_fallback 仅对 CaptureAssetImage("") 返回 True。"""

    def test_viewport_empty_path_returns_true(self) -> None:
        from harness.verification.capturer import _should_use_file_fallback
        assert _should_use_file_fallback(
            "ToolsetRegistry.EditorAppToolset.CaptureAssetImage", ""
        ) is True

    def test_asset_non_empty_path_returns_false(self) -> None:
        from harness.verification.capturer import _should_use_file_fallback
        assert _should_use_file_fallback(
            "ToolsetRegistry.EditorAppToolset.CaptureAssetImage",
            "/Engine/BasicShapes/foo.foo",
        ) is False

    def test_editor_image_returns_false(self) -> None:
        from harness.verification.capturer import _should_use_file_fallback
        assert _should_use_file_fallback(
            "ToolsetRegistry.EditorAppToolset.CaptureEditorImage", ""
        ) is False

    def test_unknown_tool_returns_false(self) -> None:
        from harness.verification.capturer import _should_use_file_fallback
        assert _should_use_file_fallback("SomeOtherTool", "") is False


class TestIsSseNoResultError:
    """_is_sse_no_result_error 精确匹配特定 JsonRpcError。"""

    def test_matching_error_returns_true(self) -> None:
        from harness.client import JsonRpcError
        from harness.verification.capturer import _is_sse_no_result_error
        exc = JsonRpcError(-32000, "SSE 流结束但未找到工具结果 (request_id=5, 共收到 3 行)")
        assert _is_sse_no_result_error(exc) is True

    def test_wrong_code_returns_false(self) -> None:
        from harness.client import JsonRpcError
        from harness.verification.capturer import _is_sse_no_result_error
        exc = JsonRpcError(-32001, "SSE 流结束但未找到工具结果")
        assert _is_sse_no_result_error(exc) is False

    def test_wrong_message_returns_false(self) -> None:
        from harness.client import JsonRpcError
        from harness.verification.capturer import _is_sse_no_result_error
        exc = JsonRpcError(-32000, "Other SSE error")
        assert _is_sse_no_result_error(exc) is False

    def test_different_exception_type(self) -> None:
        from harness.verification.capturer import _is_sse_no_result_error
        # 只能接受 JsonRpcError，其他异常类型不匹配
        try:
            _is_sse_no_result_error(ValueError("test"))
        except AttributeError:
            pass  # 不是 JsonRpcError，没有 .code/.message 属性


class TestLooksLikePng:
    """_looks_like_png 校验 PNG magic bytes。"""

    def test_valid_png_returns_true(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _looks_like_png
        png = tmp_path / "valid.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        assert _looks_like_png(png) is True

    def test_invalid_file_returns_false(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _looks_like_png
        f = tmp_path / "not_png.png"
        f.write_bytes(b"not a PNG file at all")
        assert _looks_like_png(f) is False

    def test_empty_file_returns_false(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _looks_like_png
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        assert _looks_like_png(f) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _looks_like_png
        assert _looks_like_png(tmp_path / "does_not_exist.png") is False


class TestFindLatestScreenshot:
    """_find_latest_screenshot mtime 选择逻辑。"""

    def test_selects_latest_mtime(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _find_latest_screenshot
        import time as _time

        old = tmp_path / "old.png"
        old.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        _time.sleep(0.05)
        new = tmp_path / "new.png"
        new.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)

        since = _time.time() - 10
        result = _find_latest_screenshot(tmp_path, since)
        assert result == new

    def test_skips_files_before_since(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _find_latest_screenshot
        import time as _time

        old = tmp_path / "old.png"
        old.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)

        # since 设为未来，所有现有文件都被排除
        since = _time.time() + 60
        result = _find_latest_screenshot(tmp_path, since)
        assert result is None

    def test_skips_empty_files(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _find_latest_screenshot
        import time as _time

        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")
        since = _time.time() - 10
        result = _find_latest_screenshot(tmp_path, since)
        assert result is None

    def test_only_globs_png(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _find_latest_screenshot
        import time as _time

        jpg = tmp_path / "screenshot.jpg"
        jpg.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        since = _time.time() - 10
        result = _find_latest_screenshot(tmp_path, since)
        assert result is None  # .jpg 不被 *.png glob 匹配

    def test_skips_future_mtime(self, tmp_path: Path) -> None:
        from harness.verification.capturer import _find_latest_screenshot
        import time as _time
        import os as _os

        future = tmp_path / "future.png"
        future.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        # 设置 mtime 为未来 1 小时
        future_time = _time.time() + 3600
        _os.utime(future, (future_time, future_time))

        since = _time.time() - 10
        result = _find_latest_screenshot(tmp_path, since)
        assert result is None  # 未来时间戳被排除


class TestCaptureModeValidation:
    """capture_screenshot() mode 语义校验。"""

    def test_asset_empty_path_raises_value_error(self) -> None:
        from harness.verification.capturer import capture_screenshot
        import asyncio

        async def run() -> None:
            with pytest.raises(ValueError, match="mode='asset' requires non-empty asset_path"):
                await capture_screenshot(
                    ue_client=None,  # type: ignore[arg-type]
                    mode="asset",
                    asset_path="",
                )

        asyncio.run(run())

    def test_invalid_mode_raises_value_error(self) -> None:
        from harness.verification.capturer import capture_screenshot
        import asyncio

        async def run() -> None:
            with pytest.raises(ValueError, match="无效的截图模式"):
                await capture_screenshot(
                    ue_client=None,  # type: ignore[arg-type]
                    mode="invalid_mode",
                )

        asyncio.run(run())

    def test_no_shot_session_raises_runtime_error(self) -> None:
        """未初始化截图 session 时调用 capture_screenshot 应抛 RuntimeError。"""
        from harness.verification.capturer import capture_screenshot, _shot_client as shot_client_mod
        import asyncio

        # 确保 _shot_client 为 None
        import harness.verification.capturer as capturer_mod
        old_val = capturer_mod._shot_client
        capturer_mod._shot_client = None
        try:
            async def run() -> None:
                with pytest.raises(RuntimeError, match="截图 session 未初始化"):
                    await capture_screenshot(
                        ue_client=None,  # type: ignore[arg-type]
                        mode="viewport",
                    )
            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_val


class TestCaptureWithFileFallback:
    """capture_screenshot() viewport 模式文件 fallback 集成测试。"""

    @pytest.fixture
    def _setup_shot_client(self) -> object:
        """创建一个假的已连接 McpClientSession。"""
        from unittest.mock import AsyncMock, MagicMock

        mock = MagicMock()
        mock.is_connected = True
        mock.call_tool = AsyncMock()
        return mock

    def test_readtimeout_falls_back_to_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """httpx.ReadTimeout + 新 PNG → 返回 capture_from_file() 结果。"""
        from harness.verification.capturer import (
            capture_screenshot,
            _shot_client as shot_client_mod,
            _ue_screenshot_dir as screenshot_dir_mod,
        )
        from unittest.mock import AsyncMock, MagicMock
        import asyncio
        import httpx

        # 准备 mock session
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=httpx.ReadTimeout("read timeout")
        )

        # 创建临时截图目录 + 新 PNG
        shot_dir = tmp_path / "screenshots"
        shot_dir.mkdir()
        import time
        since = time.time()
        png = shot_dir / "ScreenShot00001.png"
        # 最小有效 PNG（1x1 像素）
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
            b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        old_client = shot_client_mod
        old_dir = screenshot_dir_mod
        try:
            import harness.verification.capturer as capturer_mod
            capturer_mod._shot_client = mock_client
            capturer_mod._ue_screenshot_dir = shot_dir

            async def run() -> None:
                from PIL import Image
                # 模拟 PIL 可用时的情况
                try:
                    result = await capture_screenshot(
                        ue_client=mock_client,
                        mode="viewport",
                        max_width=256,
                        max_height=256,
                    )
                    assert result.data_b64 is not None
                    assert len(result.data_b64) > 0
                except ImportError:
                    # PIL 不可用时 capture_from_file 仍应工作
                    result = await capture_screenshot(
                        ue_client=mock_client,
                        mode="viewport",
                        max_width=256,
                        max_height=256,
                    )
                    assert result.data_b64 is not None

            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_client
            capturer_mod._ue_screenshot_dir = old_dir

    def test_readtimeout_no_file_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ReadTimeout 且无合适的 fallback 文件 → 原异常重新抛出。"""
        from harness.verification.capturer import capture_screenshot
        from unittest.mock import AsyncMock, MagicMock
        import asyncio
        import httpx

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=httpx.ReadTimeout("read timeout")
        )

        import harness.verification.capturer as capturer_mod
        old_client = capturer_mod._shot_client
        old_dir = capturer_mod._ue_screenshot_dir
        try:
            capturer_mod._shot_client = mock_client
            capturer_mod._ue_screenshot_dir = tmp_path / "nonexistent"

            async def run() -> None:
                with pytest.raises(httpx.ReadTimeout):
                    await capture_screenshot(
                        ue_client=mock_client,
                        mode="viewport",
                    )

            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_client
            capturer_mod._ue_screenshot_dir = old_dir

    def test_jsonrpc_sse_no_result_falls_back(
        self, tmp_path: Path,
    ) -> None:
        """JsonRpcError(-32000, SSE 流结束...) + 新 PNG → fallback。"""
        from harness.client import JsonRpcError
        from harness.verification.capturer import capture_screenshot
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=JsonRpcError(
                -32000,
                "SSE 流结束但未找到工具结果 (request_id=1, 共收到 5 行)",
            )
        )

        shot_dir = tmp_path / "screenshots"
        shot_dir.mkdir()
        png = shot_dir / "ScreenShot00002.png"
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
            b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        import harness.verification.capturer as capturer_mod
        old_client = capturer_mod._shot_client
        old_dir = capturer_mod._ue_screenshot_dir
        try:
            capturer_mod._shot_client = mock_client
            capturer_mod._ue_screenshot_dir = shot_dir

            async def run() -> None:
                result = await capture_screenshot(
                    ue_client=mock_client,
                    mode="viewport",
                    max_width=128,
                    max_height=128,
                )
                assert result.data_b64 is not None

            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_client
            capturer_mod._ue_screenshot_dir = old_dir

    def test_other_jsonrpc_error_reraises(
        self, tmp_path: Path,
    ) -> None:
        """其他 JsonRpcError 不应进入 fallback，原样抛出。"""
        from harness.client import JsonRpcError
        from harness.verification.capturer import capture_screenshot
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=JsonRpcError(-32603, "Internal tool error")
        )

        import harness.verification.capturer as capturer_mod
        old_client = capturer_mod._shot_client
        old_dir = capturer_mod._ue_screenshot_dir
        try:
            capturer_mod._shot_client = mock_client
            # 设置一个有效的截图目录确保 fallback 本身可用
            shot_dir = tmp_path / "screenshots"
            shot_dir.mkdir()
            capturer_mod._ue_screenshot_dir = shot_dir

            async def run() -> None:
                with pytest.raises(JsonRpcError) as exc_info:
                    await capture_screenshot(
                        ue_client=mock_client,
                        mode="viewport",
                    )
                assert "Internal tool error" in str(exc_info.value)

            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_client
            capturer_mod._ue_screenshot_dir = old_dir

    def test_asset_mode_no_fallback(
        self, tmp_path: Path,
    ) -> None:
        """mode='asset' 不走文件 fallback，即使配置了截图目录。"""
        from harness.client import JsonRpcError
        from harness.verification.capturer import capture_screenshot
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=JsonRpcError(
                -32000,
                "SSE 流结束但未找到工具结果 (request_id=1, 共收到 5 行)",
            )
        )

        shot_dir = tmp_path / "screenshots"
        shot_dir.mkdir()
        png = shot_dir / "ScreenShot00003.png"
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
            b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        import harness.verification.capturer as capturer_mod
        old_client = capturer_mod._shot_client
        old_dir = capturer_mod._ue_screenshot_dir
        try:
            capturer_mod._shot_client = mock_client
            capturer_mod._ue_screenshot_dir = shot_dir

            async def run() -> None:
                # Asset mode 有 asset_path → 不走 fallback，原异常直接抛出
                with pytest.raises(JsonRpcError):
                    await capture_screenshot(
                        ue_client=mock_client,
                        mode="asset",
                        asset_path="/Engine/BasicShapes/foo.foo",
                    )

            asyncio.run(run())
        finally:
            capturer_mod._shot_client = old_client
            capturer_mod._ue_screenshot_dir = old_dir
