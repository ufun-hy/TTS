"""Configuration and install-relative paths for the desktop client."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional


@dataclass
class ClientConfig:
    server: str = "http://192.168.3.92:8000"
    cache_dir: str = "./cache"
    poll_interval: float = 1.0
    api_key: str = ""
    timeout: int = 5
    strict_session: bool = False
    session_id: str = ""
    startup_buffer_seconds: float = 0.0

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "ClientConfig":
        raw = raw or {}
        poll_interval = float(raw.get("poll_interval", cls.poll_interval))
        if poll_interval > 60:
            # Backward compatibility with the original CLI example, which used milliseconds.
            poll_interval /= 1000
        value = cls(
            server=str(raw.get("server", cls.server)).strip(),
            cache_dir=str(raw.get("cache_dir", raw.get("cache", cls.cache_dir))),
            poll_interval=poll_interval,
            api_key=str(raw.get("api_key", cls.api_key)),
            timeout=int(raw.get("timeout", cls.timeout)),
            strict_session=bool(raw.get("strict_session", False)),
            session_id=str(raw.get("session_id", "")).strip(),
            startup_buffer_seconds=float(raw.get("startup_buffer_seconds", 0.0)),
        )
        if not value.server:
            raise ValueError("server is required")
        if value.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if value.timeout < 1:
            raise ValueError("timeout must be positive")
        if value.startup_buffer_seconds < 0:
            raise ValueError("startup_buffer_seconds must not be negative")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "cache_dir": self.cache_dir,
            "poll_interval": self.poll_interval,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "strict_session": self.strict_session,
            "session_id": self.session_id,
            "startup_buffer_seconds": self.startup_buffer_seconds,
        }


def install_dir() -> Path:
    """Return the directory beside the executable, including PyInstaller builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    configured = os.environ.get("AI_AUDIO_CLIENT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return install_dir() / "config.json"


def load_config(path: Optional[Path] = None) -> ClientConfig:
    path = path or config_path()
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("config.json must contain an object")
        return ClientConfig.from_dict(raw)
    example = install_dir() / "config" / "audio-client.example.json"
    if example.is_file():
        with example.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        return ClientConfig.from_dict(raw)
    return ClientConfig()


def save_config(config: ClientConfig, path: Optional[Path] = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def resolve_cache_dir(config: ClientConfig) -> Path:
    path = Path(config.cache_dir).expanduser()
    return path if path.is_absolute() else install_dir() / path
