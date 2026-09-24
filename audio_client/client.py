"""Persistent, retry-safe client for the LAN audio cache API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Dict, Optional
from urllib import error, request
from urllib.parse import urlencode, urljoin
import uuid

from .wav_compat import WavCompatibilityError, inspect_wav, prepare_mci_wav


LIVE_STARTUP_BUFFER_SECONDS = 12.0
CONTENT_CACHE_REVISION = "live-audio-v1"


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
        self.client_id = f"windows-{uuid.uuid4().hex[:12]}"

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

    def local_playback_state(self) -> Dict[str, Any]:
        """Summarize the newest Live Session still represented in local cache."""
        live_items = []
        for metadata_path in self.cache_dir.glob("*.json"):
            metadata = self._read_metadata(metadata_path.stem)
            if not metadata:
                continue
            wav_path = self.cache_dir / f"{metadata_path.stem}.wav"
            if not wav_path.is_file():
                continue
            server_metadata = metadata.get("server_metadata")
            if not isinstance(server_metadata, dict):
                continue
            session_id = server_metadata.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                continue
            live_items.append((metadata_path.stem, metadata, session_id.strip()))

        if not live_items:
            return {
                "session_id": "",
                "buffered_segments": 0,
                "buffered_seconds": 0.0,
                "playback_status": "idle",
            }

        def newest_key(item: tuple[str, Dict[str, Any], str]) -> tuple[str, float, str]:
            item_id, metadata, _session_id = item
            downloaded_at = metadata.get("downloaded_at")
            downloaded_at = downloaded_at if isinstance(downloaded_at, str) else ""
            sequence = metadata.get("sequence")
            if sequence is None and isinstance(metadata.get("server_metadata"), dict):
                sequence = metadata["server_metadata"].get("sequence")
            try:
                order = float(sequence)
            except (TypeError, ValueError):
                order = -1.0
            return downloaded_at, order, item_id

        active_session_id = max(live_items, key=newest_key)[2]
        active_items = [item for item in live_items if item[2] == active_session_id]
        buffered_segments = 0
        buffered_seconds = 0.0
        statuses = []
        for _item_id, metadata, _session_id in active_items:
            playback_status = str(metadata.get("playback_status") or "")
            statuses.append(playback_status)
            if playback_status not in ("buffering", "cached"):
                continue
            buffered_segments += 1
            try:
                duration = float(metadata.get("duration", 0) or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                buffered_seconds += duration

        session_started = any(status in ("playing", "played", "paused", "playback_failed") for status in statuses)
        has_cached = any(status == "cached" for status in statuses)
        if "paused" in statuses:
            playback_status = "paused"
        elif "playing" in statuses:
            playback_status = "playing"
        elif "buffering" in statuses:
            playback_status = "rebuffering" if session_started and not has_cached else "buffering"
        elif buffered_segments:
            playback_status = "buffered"
        elif session_started:
            playback_status = "rebuffering"
        else:
            playback_status = "idle"
        return {
            "session_id": active_session_id,
            "buffered_segments": buffered_segments,
            "buffered_seconds": round(buffered_seconds, 3),
            "playback_status": playback_status,
        }

    def fetch_next(self) -> Optional[ClientAudio]:
        existing = self._recoverable()
        if existing:
            return existing
        state = self.local_playback_state()
        query = urlencode({
            "client_id": self.client_id,
            "client_session_id": state["session_id"],
            "client_buffered_segments": state["buffered_segments"],
            "client_buffered_seconds": state["buffered_seconds"],
            "client_playback_status": state["playback_status"],
        })
        response = self._request("GET", f"audio/next?{query}")
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
        server_metadata = payload.get("metadata", {})
        if not isinstance(server_metadata, dict):
            server_metadata = {}
        content_key = self._content_key(server_metadata)
        download_started_at = _utc_now()
        self._write_metadata(item_id, {
            "id": item_id,
            "status": "downloading",
            "duration": payload.get("duration", 0),
            "server": self.server.rstrip("/"),
            "sequence": server_metadata.get("sequence"),
            "server_metadata": server_metadata,
            "content_key": content_key,
            "download_started_at": download_started_at,
        })
        path = self.cache_dir / f"{item_id}.wav"
        content_cache = "miss"
        if content_key and self._restore_content_blob(content_key, path):
            content_cache = "hit"
        else:
            audio = self._download(audio_url)
            if content_key:
                blob_path = self._content_blob_path(content_key)
                self._atomic_write(blob_path, audio)
                self._materialize_blob(blob_path, path)
            else:
                self._atomic_write(path, audio)

        download_ready_at = _utc_now()
        prepared_path, prepared_at = self._prepare_for_playback(path, content_key)

        session_id = str(server_metadata.get("session_id") or "").strip()
        playback_status = "cached"
        if session_id and self._session_needs_buffering(session_id):
            playback_status = "buffering"
        metadata = {
            "id": item_id,
            "status": "downloaded",
            "duration": payload.get("duration", 0),
            "server": self.server.rstrip("/"),
            "downloaded_at": _utc_now(),
            "download_started_at": download_started_at,
            "download_ready_at": download_ready_at,
            "sequence": server_metadata.get("sequence"),
            "server_metadata": server_metadata,
            "playback_status": playback_status,
            "content_key": content_key,
            "content_cache": content_cache,
        }
        if prepared_at:
            metadata["prepared_at"] = prepared_at
        if prepared_path:
            metadata["prepared_path"] = prepared_path
        self._write_metadata(item_id, metadata)
        if session_id and playback_status == "buffering":
            self._release_startup_buffer(session_id, force=server_metadata.get("session_final") is True)
            metadata = self._read_metadata(item_id) or metadata
        return ClientAudio(item_id, path, float(metadata["duration"] or 0), metadata)

    def ack(self, item_id: str, status: str = "completed") -> Dict[str, Any]:
        response = self._request_json("POST", "audio/ack", {"id": item_id, "status": status})
        if response.status != 200:
            raise AudioClientError(f"ack failed: HTTP {response.status} {response.body.decode('utf-8', 'replace')}")
        payload = json.loads(response.body.decode("utf-8"))
        metadata = self._read_metadata(item_id) or {"id": item_id}
        metadata["status"] = status
        metadata["acknowledged_at"] = _utc_now()
        metadata["ack_at"] = metadata["acknowledged_at"]
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
                content_key = str(metadata.get("content_key") or "")
                if not (content_key and self._restore_content_blob(content_key, path)):
                    audio = self._download(urljoin(self.server, f"audio/files/{metadata_path.stem}"))
                    if content_key:
                        blob_path = self._content_blob_path(content_key)
                        self._atomic_write(blob_path, audio)
                        self._materialize_blob(blob_path, path)
                    else:
                        self._atomic_write(path, audio)
                metadata["status"] = "downloaded"
                metadata["downloaded_at"] = _utc_now()
            if path.is_file() and metadata.get("status") == "downloading":
                metadata["status"] = "downloaded"
                metadata["downloaded_at"] = metadata.get("downloaded_at") or _utc_now()
            if path.is_file() and not metadata.get("download_ready_at"):
                metadata["download_ready_at"] = _utc_now()
            if path.is_file() and not metadata.get("prepared_path"):
                prepared_path, prepared_at = self._prepare_for_playback(path, str(metadata.get("content_key") or ""))
                if prepared_at:
                    metadata["prepared_at"] = prepared_at
                if prepared_path:
                    metadata["prepared_path"] = prepared_path
            if path.is_file() and "playback_status" not in metadata:
                metadata["playback_status"] = "cached"
            if path.is_file():
                self._write_metadata(metadata_path.stem, metadata)
                server_metadata = metadata.get("server_metadata")
                if isinstance(server_metadata, dict):
                    session_id = str(server_metadata.get("session_id") or "").strip()
                    if session_id and metadata.get("playback_status") == "buffering":
                        self._release_startup_buffer(session_id, force=server_metadata.get("session_final") is True)
                        metadata = self._read_metadata(metadata_path.stem) or metadata
            if not path.is_file():
                continue
            return ClientAudio(metadata_path.stem, path, float(metadata.get("duration", 0) or 0), metadata)
        return None

    def _content_key(self, server_metadata: Dict[str, Any]) -> str:
        source_hash = str(server_metadata.get("source_audio_sha256") or "").strip().lower()
        if len(source_hash) != 64 or any(character not in "0123456789abcdef" for character in source_hash):
            return ""
        try:
            playback_speed = float(server_metadata.get("playback_speed", 1.0) or 1.0)
            volume = float(server_metadata.get("volume", 100.0) if server_metadata.get("volume") is not None else 100.0)
        except (TypeError, ValueError):
            return ""
        payload = json.dumps({
            "revision": CONTENT_CACHE_REVISION,
            "source_audio_sha256": source_hash,
            "playback_speed": round(playback_speed, 6),
            "volume": round(volume, 6),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _content_blob_path(self, content_key: str) -> Path:
        return self.cache_dir / "blobs" / f"{content_key}.wav"

    @staticmethod
    def _valid_wav(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(4) == b"RIFF"
        except OSError:
            return False

    def _restore_content_blob(self, content_key: str, destination: Path) -> bool:
        blob_path = self._content_blob_path(content_key)
        if not self._valid_wav(blob_path):
            return False
        self._materialize_blob(blob_path, destination)
        return True

    def _materialize_blob(self, blob_path: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        try:
            os.link(blob_path, destination)
        except OSError:
            self._atomic_write(destination, blob_path.read_bytes())

    def _prepare_for_playback(self, path: Path, content_key: str) -> tuple[str, str]:
        """Prepare compatibility audio while the download path is still hot."""
        try:
            source_info = inspect_wav(path)
        except (OSError, WavCompatibilityError):
            return "", ""
        if source_info.sample_kind == "pcm16":
            return "", _utc_now()

        prepared = self._prepared_blob_path(content_key) if content_key else None
        if prepared and self._prepared_matches(prepared, source_info):
            return str(prepared.relative_to(self.cache_dir)), _utc_now()
        try:
            converted = prepare_mci_wav(path)
        except (OSError, WavCompatibilityError):
            return "", ""
        if converted == path:
            return "", ""
        if prepared:
            self._atomic_write(prepared, converted.read_bytes())
            return str(prepared.relative_to(self.cache_dir)), _utc_now()
        try:
            relative = str(converted.relative_to(self.cache_dir))
        except ValueError:
            relative = str(converted)
        return relative, _utc_now()

    def _prepared_blob_path(self, content_key: str) -> Optional[Path]:
        if not content_key:
            return None
        return self.cache_dir / "blobs" / f"{content_key}.pcm16.wav"

    @staticmethod
    def _prepared_matches(path: Path, source_info: Any) -> bool:
        try:
            info = inspect_wav(path)
        except (OSError, WavCompatibilityError):
            return False
        return (
            info.sample_kind == "pcm16"
            and info.channels == source_info.channels
            and info.sample_rate == source_info.sample_rate
            and info.frames == source_info.frames
        )

    def _session_items(self, session_id: str) -> list[Dict[str, Any]]:
        entries = []
        for metadata_path in self.cache_dir.glob("*.json"):
            metadata = self._read_metadata(metadata_path.stem)
            if not metadata:
                continue
            server_metadata = metadata.get("server_metadata")
            if not isinstance(server_metadata, dict) or str(server_metadata.get("session_id") or "").strip() != session_id:
                continue
            entries.append(metadata)
        return entries

    def _session_playback_started(self, session_id: str) -> bool:
        return any(
            str(metadata.get("playback_status") or "") in ("playing", "played", "paused", "playback_failed")
            for metadata in self._session_items(session_id)
        )

    def _session_needs_buffering(self, session_id: str) -> bool:
        statuses = [str(metadata.get("playback_status") or "") for metadata in self._session_items(session_id)]
        if not statuses:
            return True
        if any(status in ("playing", "played", "paused", "playback_failed", "cached") for status in statuses):
            return False
        return True

    def _release_startup_buffer(self, session_id: str, force: bool = False) -> bool:
        entries: list[tuple[str, Dict[str, Any]]] = []
        buffered_seconds = 0.0
        playback_started = False
        for metadata_path in self.cache_dir.glob("*.json"):
            metadata = self._read_metadata(metadata_path.stem)
            if not metadata:
                continue
            server_metadata = metadata.get("server_metadata")
            if not isinstance(server_metadata, dict) or str(server_metadata.get("session_id") or "").strip() != session_id:
                continue
            playback_status = str(metadata.get("playback_status") or "")
            if playback_status in ("playing", "played", "paused", "playback_failed"):
                playback_started = True
            if playback_status not in ("buffering", "cached"):
                continue
            entries.append((metadata_path.stem, metadata))
            try:
                duration = float(metadata.get("duration", 0) or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                buffered_seconds += duration
        if not force and not playback_started and buffered_seconds < LIVE_STARTUP_BUFFER_SECONDS:
            return False
        changed = False
        for item_id, metadata in entries:
            if metadata.get("playback_status") != "buffering":
                continue
            metadata["playback_status"] = "cached"
            released_at = _utc_now()
            if playback_started:
                metadata["rebuffer_released_at"] = released_at
            else:
                metadata["startup_buffer_released_at"] = released_at
            self._write_metadata(item_id, metadata)
            changed = True
        return changed

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
        path.parent.mkdir(parents=True, exist_ok=True)
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
