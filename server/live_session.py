"""Single-session coordinator for the Text Studio live workflow."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import re
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from timeline.tts_client import TTSClient, load_api_key


LIVE_STATUSES = ("idle", "starting", "running", "paused", "stopped", "failed")
SEGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_LIVE_TEXT_CHARS = 1_000_000
MAX_LIVE_SEGMENT_CHARS = 200  # matches the default TTS Gateway request limit
VOICE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
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


def _live_audio_settings(playback_speed: Any, volume: Any) -> tuple[float, float]:
    try:
        playback_speed = float(playback_speed)
        volume = float(volume)
    except (TypeError, ValueError) as exc:
        raise LiveSessionError("playback_speed and volume must be numeric", 400) from exc
    if not math.isfinite(playback_speed) or playback_speed <= 0:
        raise LiveSessionError("playback_speed must be positive", 400)
    if not math.isfinite(volume) or volume < 0:
        raise LiveSessionError("volume must not be negative", 400)
    return playback_speed, volume


def _technical_chunks(text: str) -> list[str]:
    """Only split oversized text for the Gateway request limit."""
    # ponytail: fixed-size chunks keep this layer deterministic; semantic segmentation belongs to Text Studio.
    return [text[index:index + MAX_LIVE_SEGMENT_CHARS] for index in range(0, len(text), MAX_LIVE_SEGMENT_CHARS)]


def prepare_live_segments(raw_segments: Any) -> list[dict[str, str]]:
    """Validate confirmed paragraph segments and apply only technical TTS chunking."""
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("segments must be a non-empty array")

    prepared: list[dict[str, str]] = []
    seen: set[str] = set()
    total_chars = 0
    for item in raw_segments:
        if not isinstance(item, dict):
            raise ValueError("every segment must be an object")
        segment_id = item.get("id")
        text = item.get("text")
        if not isinstance(segment_id, str) or not SEGMENT_ID.fullmatch(segment_id):
            raise ValueError("every segment needs a valid id")
        if segment_id in seen:
            raise ValueError(f"duplicate segment id: {segment_id}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"segment {segment_id} needs non-empty text")
        seen.add(segment_id)
        text = text.strip()
        total_chars += len(text)
        chunks = _technical_chunks(text)
        for chunk_index, chunk in enumerate(chunks, 1):
            item_id = segment_id if len(chunks) == 1 else f"{segment_id}-{chunk_index:02d}"
            if not SEGMENT_ID.fullmatch(item_id):
                raise ValueError(f"segment id is too long after technical chunking: {item_id}")
            prepared.append({"id": item_id, "text": chunk})
    if total_chars > MAX_LIVE_TEXT_CHARS:
        raise ValueError(f"text is too long; maximum is {MAX_LIVE_TEXT_CHARS} characters")
    return prepared


@dataclass
class LiveSnapshot:
    session_id: str
    status: str
    generated_segments: int
    total_segments: int
    ready_segments: int
    processing_segments: int
    completed_segments: int
    failed_segments: int
    client_connected: bool
    error: str = ""
    finished: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "generated_segments": self.generated_segments,
            "total_segments": self.total_segments,
            "ready_segments": self.ready_segments,
            "processing_segments": self.processing_segments,
            "completed_segments": self.completed_segments,
            "failed_segments": self.failed_segments,
            # Kept as a compatibility alias; the UI uses ready_segments explicitly.
            "cache_ready": self.ready_segments,
            "client_connected": self.client_connected,
            "error": self.error,
            "finished": self.finished,
        }


class LiveSession:
    def __init__(
        self,
        session_id: str,
        voice: str,
        segments: list[dict[str, str]],
        synthesize: Callable[[str, str], bytes],
        enqueue: Callable[[str, int, str, str, bytes], None],
        cache_status: Callable[[str], dict[str, Any]],
        playback_speed: float = 1.0,
        volume: float = 100.0,
    ) -> None:
        self.session_id = session_id
        self.voice = voice
        self.segments = segments
        self._synthesize = synthesize
        self._enqueue = enqueue
        self._cache_status = cache_status
        self.playback_speed = playback_speed
        self.volume = volume
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
            int(cache.get("ready", 0) or 0),
            int(cache.get("processing", 0) or 0),
            int(cache.get("completed", 0) or 0),
            int(cache.get("failed", 0) or 0),
            bool(cache.get("client_connected", False)),
            error,
            finished,
        )

    def _run(self) -> None:
        try:
            with self._lock:
                if self.status == "starting":
                    self.status = "running"
            for sequence, segment in enumerate(self.segments, 1):
                if self._stop.is_set():
                    return
                self._resume.wait()
                if self._stop.is_set():
                    return
                with self._lock:
                    if self.status == "paused":
                        self.status = "running"
                audio = self._synthesize(segment["text"], self.voice)
                if self._stop.is_set():
                    return
                self._enqueue(segment["id"], sequence, segment["text"], self.voice, audio)
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

    def start(self, voice: str, segments: Any, playback_speed: Any = 1.0, volume: Any = 100.0) -> dict[str, Any]:
        if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
            raise LiveSessionError("voice is invalid", 400)
        playback_speed, volume = _live_audio_settings(playback_speed, volume)
        try:
            segments = prepare_live_segments(segments)
        except ValueError as exc:
            raise LiveSessionError(str(exc), 400) from exc
        with self._lock:
            if self._session:
                if self._session.status in ("starting", "running", "paused"):
                    raise LiveSessionError("a live session is already running")
                raise LiveSessionError("reset the previous live session before starting a new one")
            session = LiveSession(
                uuid.uuid4().hex[:12],
                voice.strip(),
                segments,
                self._synthesize,
                self._enqueue,
                self._cache_status,
                playback_speed,
                volume,
            )
            self._session = session
            session.start()
            return {"session_id": session.session_id, "status": "starting"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
        if not session:
            return LiveSnapshot("", "idle", 0, 0, 0, 0, 0, 0, False).as_dict()
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
        if session and session._thread.is_alive():
            session.stop()
            session._thread.join(timeout=1)
            if session._thread.is_alive():
                raise LiveSessionError("live session is still stopping; retry reset shortly")
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
            "playback_speed": session.playback_speed,
            "volume": session.volume,
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }, cache=True)

    def _cache_status(self, session_id: str) -> dict[str, Any]:
        if self._cache_status_impl:
            return self._cache_status_impl(session_id)
        try:
            body = self._request_json("GET", f"/audio/session-status/{session_id}", cache=True)
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, RuntimeError):
            return {"ready": 0, "processing": 0, "completed": 0, "failed": 0, "client_connected": False}

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
