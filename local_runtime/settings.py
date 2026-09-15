"""External model paths and Windows DPAPI-backed API-key settings."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from typing import Any


class SecretStoreError(RuntimeError):
    pass


class DpapiSecretStore:
    """Protect secrets for the current Windows user with CryptProtectData."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def set(self, value: str) -> None:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is only available on Windows")
        if not isinstance(value, str):
            raise SecretStoreError("secret must be text")
        data = value.encode("utf-8")
        protected = self._crypt(data, protect=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(base64.b64encode(protected).decode("ascii") + "\n", encoding="ascii")
        os.replace(temporary, self.path)

    def get(self) -> str:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is only available on Windows")
        try:
            protected = base64.b64decode(self.path.read_text(encoding="ascii"), validate=True)
        except (OSError, ValueError) as exc:
            raise SecretStoreError("stored API key is unreadable") from exc
        return self._crypt(protected, protect=False).decode("utf-8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _crypt(data: bytes, protect: bool) -> bytes:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is only available on Windows")

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        source_buffer = ctypes.create_string_buffer(data)
        source = DATA_BLOB(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
        target = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), "AI Live Studio", None, None, None, 0, ctypes.byref(target)):
            raise SecretStoreError(f"DPAPI operation failed: {ctypes.get_last_error()}")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)


def default_user_root() -> Path:
    value = os.environ.get("AI_LIVE_STUDIO_DATA")
    if value:
        return Path(value).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    return Path(local_app_data or (Path.home() / ".local" / "share")) / "AI-Live-Studio"


def load_settings(root: Path) -> dict[str, Any]:
    path = Path(root) / "config" / "models.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except (OSError, ValueError) as exc:
        raise SecretStoreError(f"cannot read model settings: {exc}") from exc
    if not isinstance(value, dict):
        raise SecretStoreError("model settings must be a JSON object")
    return value


def save_settings(root: Path, value: dict[str, Any]) -> Path:
    if not isinstance(value, dict):
        raise ValueError("model settings must be an object")
    path = Path(root) / "config" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def secret_store(root: Path) -> DpapiSecretStore:
    return DpapiSecretStore(Path(root) / "config" / "openai-compatible-api-key.dpapi")
