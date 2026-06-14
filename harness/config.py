"""Harness 配置管理。

从环境变量和 .env 文件加载配置，提供合理的默认值。
所有配置项可通过 CLI 参数覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Harness 全局配置。"""

    # UE MCP Server 连接
    ue_port: int = 8000
    ue_host: str = "127.0.0.1"
    ue_url_path: str = "/mcp"

    # Harness MCP Server 监听
    listen_host: str = "127.0.0.1"
    listen_port: int = 9000

    # Session
    mcp_protocol_version: str = "2025-11-25"

    # 超时（秒）
    request_timeout: float = 30.0
    sse_read_timeout: float = 60.0

    # 工具预加载
    preload_all_toolsets: bool = True

    # 日志
    log_level: str = "INFO"
    log_dir: Path = field(default_factory=lambda: Path.home() / ".ue-harness" / "logs")

    @property
    def ue_base_url(self) -> str:
        """UE MCP Server 完整 base URL。"""
        return f"http://{self.ue_host}:{self.ue_port}{self.ue_url_path}"

    @classmethod
    def from_env(cls) -> Config:
        """从环境变量和 .env 文件加载配置。"""
        _load_dotenv()
        return cls(
            ue_port=int(os.getenv("HARNESS_UE_PORT", "8000")),
            ue_host=os.getenv("HARNESS_UE_HOST", "127.0.0.1"),
            ue_url_path=os.getenv("HARNESS_UE_URL_PATH", "/mcp"),
            listen_host=os.getenv("HARNESS_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(os.getenv("HARNESS_LISTEN_PORT", "9000")),
            mcp_protocol_version=os.getenv(
                "HARNESS_MCP_PROTOCOL_VERSION", "2025-11-25"
            ),
            request_timeout=float(os.getenv("HARNESS_REQUEST_TIMEOUT", "30.0")),
            sse_read_timeout=float(os.getenv("HARNESS_SSE_READ_TIMEOUT", "60.0")),
            preload_all_toolsets=os.getenv(
                "HARNESS_PRELOAD_TOOLSETS", "true"
            ).lower()
            != "false",
            log_level=os.getenv("HARNESS_LOG_LEVEL", "INFO"),
            log_dir=Path(os.getenv("HARNESS_LOG_DIR", str(Path.home() / ".ue-harness" / "logs"))),
        )

    def merge_cli_overrides(
        self,
        ue_port: int | None = None,
        listen_port: int | None = None,
        ue_host: str | None = None,
        listen_host: str | None = None,
    ) -> Config:
        """返回合并了 CLI 覆盖的新 Config 实例。"""
        overrides: dict = {}
        if ue_port is not None:
            overrides["ue_port"] = ue_port
        if listen_port is not None:
            overrides["listen_port"] = listen_port
        if ue_host is not None:
            overrides["ue_host"] = ue_host
        if listen_host is not None:
            overrides["listen_host"] = listen_host
        if not overrides:
            return self
        # 直接构造新实例（Python 3.14 移除了 dataclasses.replace）
        current = {
            "ue_port": self.ue_port,
            "ue_host": self.ue_host,
            "ue_url_path": self.ue_url_path,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "mcp_protocol_version": self.mcp_protocol_version,
            "request_timeout": self.request_timeout,
            "sse_read_timeout": self.sse_read_timeout,
            "preload_all_toolsets": self.preload_all_toolsets,
            "log_level": self.log_level,
            "log_dir": self.log_dir,
        }
        current.update(overrides)
        return Config(**current)


def _load_dotenv() -> None:
    """尝试从项目根目录加载 .env 文件（无外部依赖）。"""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for dotenv in candidates:
        try:
            if not dotenv.is_file():
                continue
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass
