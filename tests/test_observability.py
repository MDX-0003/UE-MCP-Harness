"""测试 harness.observability 模块 — ToolCallLogger, stats, replay。"""

import json
import tempfile
from pathlib import Path

import pytest

from harness.interceptor import ToolCallCompleted
from harness.observability.logger import ToolCallLogger, _serialize_args, _truncate
from harness.observability.stats import _load_jsonl, _find_log_file


# ---- _serialize_args ----

class TestSerializeArgs:
    """测试参数序列化辅助函数。"""

    def test_empty_args(self) -> None:
        assert _serialize_args({}) == {}

    def test_normal_args(self) -> None:
        result = _serialize_args({"glob": "DirectionalLight*", "limit": 10})
        assert result == {"glob": "DirectionalLight*", "limit": 10}

    def test_long_string_truncation(self) -> None:
        long_val = "x" * 300
        result = _serialize_args({"data": long_val})
        assert len(result["data"]) == 214  # 200 + "...[truncated]"
        assert result["data"].endswith("...[truncated]")

    def test_short_string_preserved(self) -> None:
        """短于 200 的字符串不截断。"""
        val = "x" * 199
        result = _serialize_args({"data": val})
        assert result["data"] == val

    def test_int_preserved(self) -> None:
        result = _serialize_args({"count": 5})
        assert result["count"] == 5


# ---- _truncate ----

class TestTruncate:
    """测试文本截断函数。"""

    def test_short_text(self) -> None:
        assert _truncate("hello") == "hello"

    def test_exact_max(self) -> None:
        val = "x" * 2000
        assert _truncate(val, 2000) == val

    def test_over_max(self) -> None:
        val = "x" * 3000
        result = _truncate(val, 2000)
        assert len(result) == 2000 + len("...[truncated]")
        assert result.endswith("...[truncated]")


# ---- ToolCallLogger ----

class TestToolCallLogger:
    """测试日志拦截器的核心行为。"""

    async def test_start_creates_log_file(self, tmp_path: Path) -> None:
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        assert logger.log_path is not None
        assert logger.log_path.parent == tmp_path
        assert logger.log_path.suffix == ".jsonl"
        # 注意：文件在 _background_writer 第一次写入时才真正创建
        # start() 只启动后台协程，不保证文件已存在
        await logger.stop()

    async def test_post_call_writes_line(self, tmp_path: Path) -> None:
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        event = ToolCallCompleted(
            name="SceneTools.find_actors",
            args={"glob": "Light*"},
            parsed_text='["Light_0"]',
            duration_ms=42.0,
        )
        await logger.post_call(event)
        await logger.stop()

        # 验证文件内容
        content = logger.log_path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        entry = json.loads(content.strip())
        assert entry["tool_name"] == "SceneTools.find_actors"
        assert entry["tool_input"] == {"glob": "Light*"}
        assert entry["tool_output"] == '["Light_0"]'
        assert entry["error"] is None
        assert entry["duration_ms"] == 42.0
        assert "timestamp" in entry
        assert "session_id" in entry

    async def test_post_call_with_error(self, tmp_path: Path) -> None:
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        err = RuntimeError("连接超时")
        event = ToolCallCompleted(
            name="SceneTools.find_actors",
            args={"glob": "*"},
            error=err,
            duration_ms=30000.0,
        )
        await logger.post_call(event)
        await logger.stop()

        content = logger.log_path.read_text(encoding="utf-8")
        entry = json.loads(content.strip())
        assert entry["tool_name"] == "SceneTools.find_actors"
        assert entry["error"] == "连接超时"
        assert entry["tool_output"] is None

    async def test_multiple_events(self, tmp_path: Path) -> None:
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        for i in range(5):
            await logger.post_call(ToolCallCompleted(
                name=f"test.tool_{i}",
                args={"i": i},
                duration_ms=i * 10.0,
            ))
        await logger.stop()

        lines = logger.log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 5
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["tool_name"] == f"test.tool_{i}"

    async def test_stop_flushes_queue(self, tmp_path: Path) -> None:
        """stop() 等待队列排空后再关闭。"""
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        # 写入大量事件（触发排队）
        for i in range(100):
            await logger.post_call(ToolCallCompleted(
                name=f"test.tool_{i}",
                args={},
            ))
        await logger.stop()

        lines = logger.log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 100

    async def test_long_output_truncated(self, tmp_path: Path) -> None:
        logger = ToolCallLogger(tmp_path, "test-session")
        await logger.start()

        long_output = "x" * 3000
        event = ToolCallCompleted(
            name="test.tool",
            args={},
            parsed_text=long_output,
        )
        await logger.post_call(event)
        await logger.stop()

        content = logger.log_path.read_text(encoding="utf-8")
        entry = json.loads(content.strip())
        assert len(entry["tool_output"]) <= 2100  # 2000 + truncation marker
        assert "...[truncated]" in entry["tool_output"]


# ---- stats._load_jsonl ----

class TestLoadJsonl:
    """测试 JSONL 文件加载。"""

    def test_load_valid(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text(
            '{"tool_name": "a"}\n'
            '{"tool_name": "b"}\n',
            encoding="utf-8",
        )
        entries = _load_jsonl(f)
        assert len(entries) == 2
        assert entries[0]["tool_name"] == "a"

    def test_skip_empty_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text(
            '{"tool_name": "a"}\n'
            '\n'
            '{"tool_name": "b"}\n',
            encoding="utf-8",
        )
        entries = _load_jsonl(f)
        assert len(entries) == 2

    def test_skip_corrupted_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text(
            '{"tool_name": "a"}\n'
            'not valid json\n'
            '{"tool_name": "b"}\n',
            encoding="utf-8",
        )
        entries = _load_jsonl(f)
        assert len(entries) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        entries = _load_jsonl(f)
        assert entries == []

    def test_missing_file(self, tmp_path: Path) -> None:
        entries = _load_jsonl(tmp_path / "nonexistent.jsonl")
        assert entries == []


# ---- stats._find_log_file ----

class TestFindLogFile:
    """测试日志文件查找。"""

    def test_find_latest(self, tmp_path: Path) -> None:
        a = tmp_path / "20260601-120000.jsonl"
        b = tmp_path / "20260602-120000.jsonl"
        a.write_text('{"a": 1}')
        b.write_text('{"b": 1}')
        # 确保 mtime 有序——较新的文件排前面
        import os
        os.utime(str(a), (1000000000, 1000000000))
        os.utime(str(b), (2000000000, 2000000000))
        result = _find_log_file(tmp_path)
        assert result is not None
        assert result.name == "20260602-120000.jsonl"

    def test_find_by_session_id(self, tmp_path: Path) -> None:
        a = tmp_path / "abc123.jsonl"
        a.write_text('{"a": 1}')
        result = _find_log_file(tmp_path, session_id="abc123")
        assert result is not None
        assert result.name == "abc123.jsonl"

    def test_session_id_not_found(self, tmp_path: Path) -> None:
        a = tmp_path / "abc123.jsonl"
        a.write_text('{"a": 1}')
        result = _find_log_file(tmp_path, session_id="xyz")
        assert result is None

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = _find_log_file(tmp_path)
        assert result is None
