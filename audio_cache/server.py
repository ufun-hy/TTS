"""HTTP API for the LAN audio cache."""

from __future__ import annotations

import base64
import hmac
import json
import uuid
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from server.tts_client import TTSClient

from .manager import AudioCacheError, AudioCacheManager, AudioItem
from .library import LibraryCapacityError
from .replay_journal import ReplayJournal


class AudioCacheServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_client_at = 0.0
        self.client_timeout = 10.0
        self._client_state: Dict[str, Any] = {}
        self._client_state_at = 0.0
        self._client_state_lock = threading.RLock()
        self._session_controls: Dict[str, str] = {}
        self._replay: Dict[str, Any] = {}
        self._replay_loaded = False
        self._replay_journal: Optional[ReplayJournal] = None

    def _load_replay(self, manager: AudioCacheManager) -> None:
        # Called under _client_state_lock, with a single persistent cache root.
        if self._replay_loaded:
            return
        journal = ReplayJournal(manager.root)
        data = journal.load()
        if data:
            session = data["session_id"]
            # A prior running task is never granted playback on process restart.
            restored = "stopped" if data["status"] == "stopped" else "paused"
            self._session_controls[session] = restored
            self._replay = {
                "session_id": session, "items": data["items"],
                "item_ids": data["item_ids"], "recovered": restored == "paused",
            }
        self._replay_journal = journal
        self._replay_loaded = True

    def _save_replay(self, status: str) -> None:
        # Caller holds _client_state_lock; persist before publishing a new state.
        if self._replay and self._replay_journal is not None:
            self._replay_journal.save(
                self._replay["session_id"], self._replay["item_ids"], status,
            )

    def replay_status(self, manager: AudioCacheManager) -> Dict[str, Any]:
        with self._client_state_lock:
            self._load_replay(manager)
            replay = dict(self._replay)
            if not replay:
                return {"session_id": "", "status": "idle", "items": 0}
            replay["status"] = self._session_controls.get(replay["session_id"], "stopped")
        replay["cache"] = manager.session_stats(replay["session_id"])
        return replay

    def replay_start(self, manager: AudioCacheManager, ids: Any) -> Dict[str, Any]:
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError("item_ids must contain 1 to 500 items")
        if any(not isinstance(item, str) or manager.library.get(item) is None for item in ids):
            raise ValueError("one or more library audio items do not exist")
        with self._client_state_lock:
            self._load_replay(manager)
            current = self._replay.get("session_id", "")
            if current and self._session_controls.get(current) in ("running", "paused"):
                raise ValueError("a replay session is already active")
            if any(status == "running" for session, status in self._session_controls.items()
                   if session != current):
                raise ValueError("stop active playback before starting an inventory replay")
            replay_id = "replay_" + uuid.uuid4().hex[:16]
            # Persist selection before staging: a crash cannot leave an
            # orphaned task that accidentally becomes playable.
            self._replay = {"session_id": replay_id, "items": len(ids), "item_ids": list(ids), "recovered": False}
            self._save_replay("paused")
            self._session_controls[replay_id] = "paused"
            try:
                for index, item_id in enumerate(ids, 1):
                    manager.stage_library_audio(item_id, replay_id, index)
            except Exception:
                manager.cleanup_session(replay_id)
                self._save_replay("stopped")
                self._session_controls[replay_id] = "stopped"
                raise
            self._save_replay("running")
            self._session_controls[replay_id] = "running"
            return self.replay_status(manager)

    def replay_control(self, manager: AudioCacheManager, action: str) -> Dict[str, Any]:
        if action not in ("pause", "resume", "stop"):
            raise ValueError("invalid replay action")
        with self._client_state_lock:
            self._load_replay(manager)
            session = self._replay.get("session_id")
            if not session:
                raise ValueError("no replay session")
            prior = self._session_controls.get(session, "stopped")
            if prior == "stopped" and action != "stop":
                raise ValueError("replay already stopped; start a new replay")
            desired = {
                "pause": "paused", "resume": "running", "stop": "stopped"
            }[action]
            self._save_replay(desired)
            self._session_controls[session] = desired
            self._replay["recovered"] = False
        return self.replay_status(manager)

    def client_connected(self) -> bool:
        return time.monotonic() - self.last_client_at <= self.client_timeout

    def update_client_state(self, query: Dict[str, list[str]]) -> None:
        def first(name: str, default: str = "") -> str:
            values = query.get(name, [])
            return values[0] if values else default

        try:
            buffered_segments = max(0, int(first("client_buffered_segments", "0") or 0))
        except (TypeError, ValueError):
            buffered_segments = 0
        try:
            buffered_seconds = max(0.0, float(first("client_buffered_seconds", "0") or 0))
        except (TypeError, ValueError):
            buffered_seconds = 0.0
        playback_status = first("client_playback_status", "idle")
        if playback_status not in ("idle", "buffered", "playing", "paused"):
            playback_status = "idle"
        state = {
            "session_id": first("client_session_id").strip(),
            "buffered_segments": buffered_segments,
            "buffered_seconds": round(buffered_seconds, 3),
            "playback_status": playback_status,
        }
        now = time.monotonic()
        with self._client_state_lock:
            self._client_state = state
            self._client_state_at = now
            self.last_client_at = now

    def client_state(self, session_id: str = "") -> Dict[str, Any]:
        with self._client_state_lock:
            state = dict(self._client_state)
            state_at = self._client_state_at
        if not state or time.monotonic() - state_at > self.client_timeout:
            return {}
        if session_id and state.get("session_id") != session_id:
            return {}
        return state

    def set_session_control(self, session_id: str, status: str) -> None:
        if not session_id or status not in {"starting", "running", "paused", "stopping", "stopped"}:
            raise ValueError("invalid session control")
        with self._client_state_lock:
            self._session_controls[session_id] = status

    def session_control(self, session_id: str) -> Dict[str, str]:
        with self._client_state_lock:
            # Unknown archived replay sessions must never auto-play after restart.
            default_status = "stopped" if session_id.startswith("replay_") else "running"
            return {"session_id": session_id, "status": self._session_controls.get(session_id, default_status)}

    def claim_next(self, manager: AudioCacheManager, requested_session: str = "") -> Optional[AudioItem]:
        """Claim only from an active session, or discover the newest one."""
        ready = manager.list_items("ready")
        if requested_session:
            status = self.session_control(requested_session)["status"]
            if status in {"paused", "stopping", "stopped"}:
                return None
            return manager.claim_next(requested_session)

        active = []
        for item in ready:
            session_id = str(item.metadata.get("session_id", "") or "")
            if not session_id:
                continue
            if self.session_control(session_id)["status"] in {"starting", "running"}:
                active.append(item)
        if active:
            newest = max(active, key=lambda item: (str(item.metadata.get("created_at", "")), item.id))
            return manager.claim_next(str(newest.metadata.get("session_id", "")))
        # Preserve the legacy untagged cache protocol when no session exists.
        if any(not item.metadata.get("session_id") for item in ready):
            return manager.claim_next("")
        return None


def make_handler(manager: AudioCacheManager, tts: Optional[TTSClient] = None, api_key: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "audio-cache/1.0"

        def _json(self, status: int, body: Dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            if not api_key:
                return True
            supplied = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return supplied.startswith(prefix) and hmac.compare_digest(supplied[len(prefix):], api_key)

        def _body(self) -> Dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                raise ValueError("content_type_must_be_application_json")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024 * 1024:
                raise ValueError("request body must be between 1 byte and 16 MiB")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            split = urlsplit(self.path)
            path = split.path
            if path in ("/health", "/healthz"):
                self._json(200, {
                    "status": "ok",
                    "cache": manager.stats(),
                    "client_connected": self.server.client_connected(),  # type: ignore[attr-defined]
                })
                return
            if path == "/audio/replay/status":
                self._json(200, self.server.replay_status(manager))  # type: ignore[attr-defined]
                return
            if path == "/audio/library/capacity":
                try:
                    self._json(200, manager.library_capacity())
                except (ValueError, OSError) as exc:
                    self._json(503, {"error": str(exc)})
                return
            if path == "/audio/library":
                query = parse_qs(split.query, keep_blank_values=True)
                session_id = (query.get("session_id") or [""])[0]
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                    self._json(200, {"items": manager.library.list(session_id, limit)})
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/audio/library/files/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    file = manager.library.file(item_id)
                except ValueError:
                    file = None
                if file is None:
                    self._json(404, {"error": "library_audio_not_found"})
                    return
                self._send_file(file)
                return
            if path == "/audio/next":
                query = parse_qs(split.query, keep_blank_values=True)
                self.server.update_client_state(query)  # type: ignore[attr-defined]
                requested_session = (query.get("client_session_id") or [""])[0].strip()
                item = self.server.claim_next(manager, requested_session)  # type: ignore[attr-defined]
                if not item:
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json(200, _next_payload(item))
                return
            if path.startswith("/audio/session-status/"):
                session_id = unquote(path.rsplit("/", 1)[-1])
                client_state = self.server.client_state(session_id)  # type: ignore[attr-defined]
                self._json(200, {
                    **manager.session_stats(session_id),
                    "client_connected": self.server.client_connected(),  # type: ignore[attr-defined]
                    "client_state": client_state,
                    "client_buffered_segments": int(client_state.get("buffered_segments", 0) or 0),
                    "client_buffered_seconds": float(client_state.get("buffered_seconds", 0) or 0),
                    "control_status": self.server.session_control(session_id)["status"],  # type: ignore[attr-defined]
                })
                return
            if path.startswith("/audio/session-control/"):
                session_id = unquote(path.rsplit("/", 1)[-1])
                self._json(200, self.server.session_control(session_id))  # type: ignore[attr-defined]
                return
            if path.startswith("/audio/files/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    audio = manager.audio_path(item_id)
                except AudioCacheError:
                    audio = None
                if not audio:
                    self._json(404, {"error": "audio_not_found"})
                    return
                self._send_file(audio)
                return
            if path.startswith("/audio/status/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    item = manager.get(item_id)
                except AudioCacheError:
                    item = None
                if not item:
                    self._json(404, {"error": "audio_not_found"})
                else:
                    self._json(200, _item_payload(item))
                return
            self._json(404, {"error": "not_found"})

        def _send_file(self, path: Path) -> None:
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        return
                    self.wfile.write(chunk)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if path == "/audio/library/delete":
                    item_ids = body.get("item_ids")
                    # Protect selected inventory even if playback has only staged
                    # some of the WAVs, plus all active cache-backed references.
                    replay = self.server.replay_status(manager)  # type: ignore[attr-defined]
                    protected = set()
                    if replay.get("status") in ("running", "paused"):
                        protected.update(replay.get("item_ids", []))
                    for entry in manager.list_items():
                        if entry.status in ("ready", "pending", "processing"):
                            original = entry.metadata.get("library_id")
                            if isinstance(original, str):
                                protected.add(original)
                            # Generated items use their cache ID as archive ID.
                            if entry.metadata.get("source") == "live_session":
                                protected.add(entry.id)
                    self._json(200, manager.library.delete(item_ids, protected))
                    return
                if path == "/audio/replay/start":
                    self._json(202, self.server.replay_start(manager, body.get("item_ids")))  # type: ignore[attr-defined]
                    return
                if path in ("/audio/replay/pause", "/audio/replay/resume", "/audio/replay/stop"):
                    self._json(200, self.server.replay_control(manager, path.rsplit("/", 1)[-1]))  # type: ignore[attr-defined]
                    return
                if path == "/audio/ack":
                    item = manager.ack(str(body.get("id", "")), str(body.get("status", "completed")))
                    self._json(200, _item_payload(item))
                    return
                if path == "/audio/enqueue":
                    item = self._enqueue(body)
                    self._json(201, _item_payload(item))
                    return
                if path == "/audio/preload":
                    self._preload(body)
                    return
                if path == "/audio/cleanup":
                    session_id = body.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        raise ValueError("session_id is required")
                    self._json(200, {"removed": manager.cleanup_session(session_id)})
                    return
                if path == "/audio/session-control":
                    session_id = body.get("session_id")
                    status = body.get("status")
                    self.server.set_session_control(session_id, status)  # type: ignore[attr-defined]
                    self._json(200, self.server.session_control(session_id))  # type: ignore[attr-defined]
                    return
                self._json(404, {"error": "not_found"})
            except LibraryCapacityError as exc:
                self._json(507, {"error": str(exc), "capacity": manager.library_capacity()})
            except (AudioCacheError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                self._json(502, {"error": str(exc)})

        def _enqueue(self, body: Dict[str, Any]) -> AudioItem:
            item_id = body.get("id")
            metadata = {
                key: body[key]
                for key in ("text", "voice", "target_duration", "sequence", "source", "session_id", "playback_speed", "volume",
                            "source_audio_sha256", "round_position", "round_total", "looping", "session_final")
                if key in body
            }
            if "voice" not in metadata:
                metadata["voice"] = "default"
            encoded = body.get("audio_base64")
            if encoded is not None:
                if not isinstance(encoded, str):
                    raise ValueError("audio_base64 must be a string")
                audio = base64.b64decode(encoded, validate=True)
            else:
                text = str(body.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required when audio_base64 is absent")
                if not tts:
                    raise RuntimeError("TTS client is not configured")
                audio, _latency = tts.synthesize(text, str(metadata["voice"]))
            import hashlib
            metadata["source_audio_sha256"] = hashlib.sha256(audio).hexdigest()
            return manager.add_audio(audio, metadata, str(item_id) if item_id is not None else None)

        def _preload(self, body: Dict[str, Any]) -> None:
            segments = body.get("segments")
            if not isinstance(segments, list):
                raise ValueError("segments must be an array")
            target = int(body.get("preload_segments", getattr(self.server, "preload_segments", 5)))
            if target < 1:
                raise ValueError("preload_segments must be positive")
            ready = manager.stats()["ready"]
            generated = []
            for index, segment in enumerate(segments):
                if ready >= target:
                    break
                if not isinstance(segment, dict):
                    raise ValueError("every segment must be an object")
                item_id = str(segment.get("id", f"segment_{index + 1:03d}"))
                if manager.has(item_id):
                    continue
                payload = dict(segment)
                payload.setdefault("sequence", index)
                item = self._enqueue(payload)
                generated.append(item.id)
                ready += 1
            self._json(200, {"preload_segments": target, "generated": generated, "ready": manager.stats()["ready"]})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"audio-cache {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def _item_payload(item: AudioItem) -> Dict[str, Any]:
    return {"id": item.id, "status": item.status, "duration": item.duration, "metadata": item.public_metadata()}


def _next_payload(item: AudioItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "url": f"/audio/files/{item.id}",
        "duration": item.duration,
        "metadata": item.public_metadata(),
    }


def serve(manager: AudioCacheManager, host: str, port: int, tts: Optional[TTSClient] = None, api_key: str = "", preload_segments: int = 5) -> None:
    server = AudioCacheServer((host, port), make_handler(manager, tts, api_key))
    server.preload_segments = preload_segments
    print(f"Audio cache API: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
