"""Persistent, retry-safe client for the LAN audio cache API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Dict, Optional
from urllib import error, request
from urllib.parse import urljoin


class AudioClientError(RuntimeError):
    pass


@dataclass
class ClientAudio:
    id: str
    path: Path
    duration: float
    metadata: Dict[str, Any]


class AudioClient:
    def __init__(self, server: str, cache_dir: Path, poll_interval: float = 1.0, api_key: str = "", timeout: int = 15) -> None:
        self.server = server.rstrip("/") + "/"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = max(0.05, float(poll_interval))
        self.api_key = api_key
        self.timeout = timeout

    def health(self) -> Dict[str, Any]:
        response = self._request("GET", "health")
        if response.status != 200:
            raise AudioClientError(f"health check failed: HTTP {response.status}")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AudioClientError("health response is not JSON") from exc
        if not isinstance(value, dict):
            raise AudioClientError("health response must be an object")
        return value

    def local_stats(self) -> Dict[str, int]:
        stats = {"received": 0, "completed": 0, "failed": 0, "cache": 0}
        stats["cache"] = sum(1 for path in self.cache_dir.glob("*.wav") if path.is_file())
        for path in self.cache_dir.glob("*.json"):
            metadata = self._read_metadata(path.stem)
            if not metadata:
                continue
            status = metadata.get("status")
            if status in ("downloaded", "completed", "failed"):
                stats["received"] += 1
            if status in ("completed", "failed"):
                stats[status] += 1
        return stats

    def fetch_next(self) -> Optional[ClientAudio]:
        existing = self._recoverable()
        if existing:
            return existing
        response = self._request("GET", "audio/next")
        if response is None:
            return None
        if response.status == 204:
            return None
        if response.status != 200:
            raise AudioClientError(f"next request failed: HTTP {response.status}")
        payload = json.loads(response.body.decode("utf-8"))
        item_id = str(payload.get("id", ""))
        if not item_id:
            raise AudioClientError("next response has no id")
        audio_url = urljoin(self.server, str(payload.get("url", f"audio/files/{item_id}")))
        self._write_metadata(item_id, {
            "id": item_id,
            "status": "downloading",
            "duration": payload.get("duration", 0),
            "server": self.server.rstrip("/"),
            "server_metadata": payload.get("metadata", {}),
        })
        audio = self._download(audio_url)
        path = self.cache_dir / f"{item_id}.wav"
        self._atomic_write(path, audio)
        metadata = {
            "id": item_id,
            "status": "downloaded",
            "duration": payload.get("duration", 0),
            "server": self.server.rstrip("/"),
            "downloaded_at": _utc_now(),
            "server_metadata": payload.get("metadata", {}),
        }
        self._write_metadata(item_id, metadata)
        return ClientAudio(item_id, path, float(metadata["duration"] or 0), metadata)

    def ack(self, item_id: str, status: str = "completed") -> Dict[str, Any]:
        response = self._request_json("POST", "audio/ack", {"id": item_id, "status": status})
        if response.status != 200:
            raise AudioClientError(f"ack failed: HTTP {response.status} {response.body.decode('utf-8', 'replace')}")
        payload = json.loads(response.body.decode("utf-8"))
        metadata = self._read_metadata(item_id) or {"id": item_id}
        metadata["status"] = status
        metadata["acknowledged_at"] = _utc_now()
        self._write_metadata(item_id, metadata)
        return payload

    def run(self, on_audio: Callable[[ClientAudio], Optional[str]], stop: Optional[Callable[[], bool]] = None) -> None:
        while not (stop and stop()):
            try:
                item = self.fetch_next()
                if item:
                    status = on_audio(item)
                    if status in ("completed", "failed"):
                        self.ack(item.id, status)
                    else:
                        # The consumer may acknowledge from another thread or
                        # process. Do not invoke it repeatedly for one item.
                        while True:
                            time.sleep(self.poll_interval)
                            current = self._read_metadata(item.id)
                            if not current or current.get("status") != "downloaded":
                                break
                else:
                    time.sleep(self.poll_interval)
            except (AudioClientError, OSError, error.URLError) as exc:
                print(f"audio client: {exc}", flush=True)
                time.sleep(self.poll_interval)

    def _recoverable(self) -> Optional[ClientAudio]:
        for metadata_path in sorted(self.cache_dir.glob("*.json")):
            metadata = self._read_metadata(metadata_path.stem)
            if not metadata or metadata.get("status") not in ("downloading", "downloaded"):
                continue
            path = self.cache_dir / f"{metadata_path.stem}.wav"
            response = self._request("GET", f"audio/status/{metadata_path.stem}")
            if response.status == 200:
                remote = json.loads(response.body.decode("utf-8"))
                if remote.get("status") in ("completed", "failed"):
                    metadata["status"] = remote["status"]
                    self._write_metadata(metadata_path.stem, metadata)
                    continue
            # The server keeps processing items across restarts. Retain the
            # local copy and let the caller decide when to acknowledge it.
            if not path.is_file():
                audio = self._download(urljoin(self.server, f"audio/files/{metadata_path.stem}"))
                self._atomic_write(path, audio)
                metadata["status"] = "downloaded"
                metadata["downloaded_at"] = _utc_now()
                self._write_metadata(metadata_path.stem, metadata)
            if not path.is_file():
                continue
            return ClientAudio(metadata_path.stem, path, float(metadata.get("duration", 0) or 0), metadata)
        return None

    def _download(self, url: str) -> bytes:
        response = self._request("GET", url)
        if response.status != 200:
            raise AudioClientError(f"download failed: HTTP {response.status}")
        audio = response.body
        if not audio.startswith(b"RIFF"):
            raise AudioClientError("downloaded file is not a WAV")
        return audio

    def _request_json(self, method: str, path: str, payload: Dict[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request(method, path, body, "application/json")

    def _request(self, method: str, path: str, body: Optional[bytes] = None, content_type: str = "") -> Any:
        target = path if path.startswith("http://") or path.startswith("https://") else urljoin(self.server, path)
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with request.urlopen(request.Request(target, data=body, headers=headers, method=method), timeout=self.timeout) as response:
                return _Response(response.status, response.read())
        except error.HTTPError as exc:
            return _Response(exc.code, exc.read())
        except (OSError, error.URLError) as exc:
            raise AudioClientError(str(exc)) from exc

    def _write_metadata(self, item_id: str, metadata: Dict[str, Any]) -> None:
        self._atomic_write(self.cache_dir / f"{item_id}.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))

    def _read_metadata(self, item_id: str) -> Optional[Dict[str, Any]]:
        path = self.cache_dir / f"{item_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)


@dataclass
class _Response:
    status: int
    body: bytes


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
