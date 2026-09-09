"""Single-session coordinator for the Text Studio live workflow."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from timeline.tts_client import TTSClient, load_api_key


LIVE_STATUSES = ("idle", "starting", "running", "paused", "stopped", "failed")
VOICE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_LIVE_TEXT_CHARS = 1_000_000
MAX_LIVE_SEGMENT_CHARS = 200  # matches the default TTS Gateway request limit
VOICE_LABELS = {
    "default": "默认声音",
    "speaker_a": "主播A",
    "speaker_b": "主播B",
    "speaker_c": "主播C",
}


class LiveSessionError(RuntimeError):
    """A user-facing live session error with an HTTP status."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def split_live_text(text: str) -> list[str]:
    """Split a script into speech-sized sentences while preserving punctuation."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    segments: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", normalized):
        parts = re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+", paragraph.strip())
        for part in (part.strip() for part in parts if part.strip()):
            while len(part) > MAX_LIVE_SEGMENT_CHARS:
                boundary = max(part.rfind(mark, 0, MAX_LIVE_SEGMENT_CHARS + 1) for mark in "，,、；; ")
                boundary = boundary + 1 if boundary > 0 else MAX_LIVE_SEGMENT_CHARS
                segments.append(part[:boundary].strip())
                part = part[boundary:].strip()
            if part:
                segments.append(part)
    return segments


@dataclass
class LiveSnapshot:
    session_id: str
    status: str
    generated_segments: int
    total_segments: int
    cache_ready: int
    client_connected: bool
    error: str = ""
    finished: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "generated_segments": self.generated_segments,
            "total_segments": self.total_segments,
            "cache_ready": self.cache_ready,
            "client_connected": self.client_connected,
            "error": self.error,
            "finished": self.finished,
        }


class LiveSession:
    def __init__(
        self,
        session_id: str,
        voice: str,
        text: str,
        synthesize: Callable[[str, str], bytes],
        enqueue: Callable[[str, int, str, str, bytes], None],
        cache_status: Callable[[str], dict[str, Any]],
    ) -> None:
        self.session_id = session_id
        self.voice = voice
        self.segments = split_live_text(text)
        self._synthesize = synthesize
        self._enqueue = enqueue
        self._cache_status = cache_status
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._thread = threading.Thread(target=self._run, name=f"live-{session_id}", daemon=True)
        self.status = "starting"
        self.generated_segments = 0
        self.error = ""
        self.finished = False

    def start(self) -> None:
        self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.status not in ("starting", "running"):
                raise LiveSessionError(f"cannot pause a {self.status} session")
            self.status = "paused"
            self._resume.clear()

    def resume(self) -> None:
        with self._lock:
            if self.status != "paused":
                raise LiveSessionError(f"cannot resume a {self.status} session")
            self.status = "running"
            self._resume.set()

    def stop(self) -> None:
        with self._lock:
            if self.status in ("stopped", "failed"):
                return
            self.status = "stopped"
            self._stop.set()
            self._resume.set()

    def snapshot(self) -> LiveSnapshot:
        with self._lock:
            status = self.status
            generated = self.generated_segments
            error = self.error
            finished = self.finished
        cache = self._cache_status(self.session_id)
        return LiveSnapshot(
            self.session_id,
            status,
            generated,
            len(self.segments),
            int(cache.get("ready", cache.get("cache_ready", 0)) or 0),
            bool(cache.get("client_connected", False)),
            error,
            finished,
        )

    def _run(self) -> None:
        try:
            with self._lock:
                if self.status == "starting":
                    self.status = "running"
            for index, text in enumerate(self.segments, 1):
                if self._stop.is_set():
                    return
                self._resume.wait()
                if self._stop.is_set():
                    return
                with self._lock:
                    if self.status == "paused":
                        self.status = "running"
                audio = self._synthesize(text, self.voice)
                if self._stop.is_set():
                    return
                self._enqueue(self._segment_id(index), index, text, self.voice, audio)
                with self._lock:
                    self.generated_segments += 1
            with self._lock:
                if self.status != "failed":
                    self.status = "stopped"
                    self.finished = True
        except Exception as exc:  # keep the HTTP server alive when one segment fails
            with self._lock:
                if not self._stop.is_set():
                    self.status = "failed"
                    self.error = str(exc)

    def _segment_id(self, index: int) -> str:
        return f"segment_{index:03d}"


class LiveSessionManager:
    """Own one active session and bridge TTS Gateway to Audio Cache."""

    def __init__(
        self,
        gateway_url: str,
        cache_url: str,
        tts_api_key: str = "",
        cache_api_key: str = "",
        synthesize: Callable[[str, str], bytes] | None = None,
        enqueue: Callable[[str, int, str, str, bytes], None] | None = None,
        cache_status: Callable[[str], dict[str, Any]] | None = None,
        cache_cleanup: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.cache_url = cache_url.rstrip("/")
        self.cache_api_key = cache_api_key
        self._tts = TTSClient(self.gateway_url, tts_api_key or load_api_key())
        self._synthesize_impl = synthesize
        self._enqueue_impl = enqueue
        self._cache_status_impl = cache_status
        self._cache_cleanup_impl = cache_cleanup
        self._lock = threading.RLock()
        self._session: LiveSession | None = None

    def start(self, voice: str, text: str) -> dict[str, Any]:
        if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
            raise LiveSessionError("voice is invalid", 400)
        if not isinstance(text, str) or not text.strip():
            raise LiveSessionError("text is required", 400)
        if len(text) > MAX_LIVE_TEXT_CHARS:
            raise LiveSessionError(f"text is too long; maximum is {MAX_LIVE_TEXT_CHARS} characters", 400)
        segments = split_live_text(text)
        if not segments:
            raise LiveSessionError("text is required", 400)
        with self._lock:
            if self._session:
                if self._session.status in ("starting", "running", "paused"):
                    raise LiveSessionError("a live session is already running")
                raise LiveSessionError("reset the previous live session before starting a new one")
            session = LiveSession(
                uuid.uuid4().hex[:12],
                voice.strip(),
                text,
                self._synthesize,
                self._enqueue,
                self._cache_status,
            )
            self._session = session
            session.start()
            return {"session_id": session.session_id, "status": "starting"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
        if not session:
            return LiveSnapshot("", "idle", 0, 0, 0, False).as_dict()
        return session.snapshot().as_dict()

    def pause(self) -> dict[str, Any]:
        session = self._require_session()
        session.pause()
        return self.status()

    def resume(self) -> dict[str, Any]:
        session = self._require_session()
        session.resume()
        return self.status()

    def stop(self) -> dict[str, Any]:
        session = self._require_session()
        session.stop()
        return self.status()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
        if session and session.status in ("starting", "running", "paused"):
            session.stop()
            session._thread.join(timeout=1)
        if session:
            self._cache_cleanup(session.session_id)
        with self._lock:
            self._session = None
        return self.status()

    def voices(self) -> dict[str, Any]:
        try:
            body = self._request_json("GET", "/voices")
            values = body.get("voices", []) if isinstance(body, dict) else []
        except (OSError, ValueError, RuntimeError):
            values = [{"id": "default", "available": False}]
        voices = []
        for item in values:
            voice_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(voice_id, str) and VOICE_ID.fullmatch(voice_id):
                voices.append({
                    "id": voice_id,
                    "label": VOICE_LABELS.get(voice_id, voice_id),
                    "available": bool(item.get("available", True)),
                })
        return {"voices": voices or [{"id": "default", "label": VOICE_LABELS["default"], "available": False}]}

    def _require_session(self) -> LiveSession:
        with self._lock:
            if not self._session:
                raise LiveSessionError("no live session", 404)
            return self._session

    def _synthesize(self, text: str, voice: str) -> bytes:
        if self._synthesize_impl:
            return self._synthesize_impl(text, voice)
        audio, _latency = self._tts.synthesize(text, voice)
        return audio

    def _enqueue(self, item_id: str, sequence: int, text: str, voice: str, audio: bytes) -> None:
        if self._enqueue_impl:
            self._enqueue_impl(item_id, sequence, text, voice, audio)
            return
        session = self._require_session()
        self._request_json("POST", "/audio/enqueue", {
            "id": item_id,
            "sequence": sequence,
            "source": "live_session",
            "session_id": session.session_id,
            "text": text,
            "voice": voice,
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }, cache=True)

    def _cache_status(self, session_id: str) -> dict[str, Any]:
        if self._cache_status_impl:
            return self._cache_status_impl(session_id)
        try:
            body = self._request_json("GET", f"/audio/session-status/{session_id}", cache=True)
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, RuntimeError):
            return {"ready": 0, "client_connected": False}

    def _cache_cleanup(self, session_id: str) -> dict[str, Any]:
        if self._cache_cleanup_impl:
            return self._cache_cleanup_impl(session_id)
        try:
            body = self._request_json("POST", "/audio/cleanup", {"session_id": session_id}, cache=True)
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, RuntimeError) as exc:
            raise LiveSessionError(f"audio cache reset failed: {exc}", 502) from exc

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None, cache: bool = False) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if cache and self.cache_api_key:
            headers["Authorization"] = f"Bearer {self.cache_api_key}"
        if not cache and self._tts.api_key:
            headers["Authorization"] = f"Bearer {self._tts.api_key}"
        base_url = self.cache_url if cache else self.gateway_url
        request = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3 if cache else 10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or f"HTTP {exc.code}") from exc


def build_live_manager(gateway_url: str, cache_url: str, tts_api_key: str = "", cache_api_key: str = "") -> LiveSessionManager:
    return LiveSessionManager(gateway_url, cache_url, tts_api_key, cache_api_key)
