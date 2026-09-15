"""Small manifest-based application updater.

The updater handles metadata and SHA-256 verification.  It never fetches model
files and it refuses to apply while the local runtime owns a task.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from urllib import error, request

from .manager import RuntimeManager


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    release_notes: str
    installer_url: str
    sha256: str
    mandatory: bool


def _version(value: str) -> tuple[int, ...]:
    parts = value.strip().lstrip("v").split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise UpdateError("invalid update version")
    return tuple(int(part) for part in parts)


def validate_manifest(value: object) -> UpdateManifest:
    if not isinstance(value, dict):
        raise UpdateError("update manifest must be an object")
    version = str(value.get("version", "")).strip()
    _version(version)
    url = str(value.get("installer_url", "")).strip()
    if not url.startswith("https://"):
        raise UpdateError("installer_url must use HTTPS")
    digest = str(value.get("sha256", "")).strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise UpdateError("update manifest sha256 is invalid")
    return UpdateManifest(version, str(value.get("release_notes", "")), url, digest, bool(value.get("mandatory", False)))


class UpdateManager:
    def __init__(self, data_root: Path, current_version: str = "0.1.0", manifest_url: str = "", runtime: RuntimeManager | None = None) -> None:
        self.data_root = Path(data_root)
        self.current_version = current_version
        self.manifest_url = manifest_url
        self.runtime = runtime
        self.manifest: UpdateManifest | None = None
        self.downloaded: Path | None = None
        self.error = ""

    def status(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "manifest_url_configured": bool(self.manifest_url),
            "available_version": self.manifest.version if self.manifest else "",
            "update_available": bool(self.manifest and _version(self.manifest.version) > _version(self.current_version)),
            "mandatory": bool(self.manifest.mandatory) if self.manifest else False,
            "downloaded": str(self.downloaded) if self.downloaded else "",
            "error": self.error,
        }

    def check(self) -> dict[str, object]:
        if not self.manifest_url:
            self.error = "update source is not configured"
            return self.status()
        try:
            with request.urlopen(self.manifest_url, timeout=10) as response:
                self.manifest = validate_manifest(json.load(response))
            self.error = ""
        except (OSError, ValueError, error.URLError, UpdateError) as exc:
            self.error = str(exc)
        return self.status()

    def download(self) -> Path:
        if not self.manifest:
            self.check()
        if not self.manifest:
            raise UpdateError(self.error or "no update manifest")
        target_dir = self.data_root / "updates"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"AI-Live-Studio-{self.manifest.version}.exe"
        try:
            with request.urlopen(self.manifest.installer_url, timeout=120) as response:
                payload = response.read()
        except (OSError, error.URLError) as exc:
            raise UpdateError(str(exc)) from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != self.manifest.sha256:
            raise UpdateError("downloaded installer SHA256 does not match manifest")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        self.downloaded = target
        return target

    def apply(self) -> dict[str, object]:
        if self.runtime:
            snapshot = self.runtime.snapshot()
            if snapshot["state"] != "IDLE" or not snapshot["model_released"]:
                raise UpdateError("application update is allowed only while runtime is idle")
        installer = self.downloaded or self.download()
        try:
            process = subprocess.Popen([str(installer)], shell=False)
        except OSError as exc:
            raise UpdateError(f"could not start installer: {exc}") from exc
        return {"started": True, "pid": process.pid, "installer": str(installer)}
