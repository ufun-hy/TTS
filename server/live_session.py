"""Single-session coordinator for the Text Studio live workflow."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from timeline.tts_client import RetryableTTSClientError, TTSClient, load_api_key
from server.context_variants import choose_variant_round, prepare_context_live
from server.semantic_tts_blocks import semantic_blocks


LIVE_STATUSES = ("idle", "starting", "running", "paused", "stopping", "stopped", "failed")
SEGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_LIVE_TEXT_CHARS = 1_000_000
MAX_LIVE_SEGMENT_CHARS = 200  # matches the default TTS Gateway request limit
VOICE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_BUFFER_HIGH_SECONDS = 300.0
DEFAULT_BUFFER_LOW_SECONDS = 180.0
BUFFER_POLL_SECONDS = 0.25
CACHE_ENQUEUE_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 120.0
VOICE_LABELS = {
    "default": "默认声音",
    "speaker_a": "主播A",
    "speaker_b": "主播B",
    "speaker_c": "主播C",
    "shiliu_1": "石榴1",
    "huangtao_1": "黄桃1",
}
_DYNAMIC_TIME_TOKEN = re.compile(r"\{\{(current_time|current_date|current_weekday)\}\}")


def resolve_dynamic_time(text: str, now: datetime | None = None) -> str:
    """Resolve supported time tokens immediately before a TTS request."""
    if now is None:
        current = datetime.now().astimezone()
    elif now.tzinfo is None:
        current = now.astimezone()
    else:
        current = now
    hour = current.hour
    if hour < 6:
        period = "凌晨"
    elif hour < 12:
        period = "上午"
    elif hour == 12:
        period = "中午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"
    hour12 = hour % 12 or 12
    time_text = f"{period}{hour12}点" + (f"{current.minute}分" if current.minute else "")
    values = {
        "current_time": time_text,
        "current_date": f"{current.year}年{current.month}月{current.day}日",
        "current_weekday": f"星期{('一', '二', '三', '四', '五', '六', '日')[current.weekday()]}",
    }
    return _DYNAMIC_TIME_TOKEN.sub(lambda match: values[match.group(1)], text)


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


def _buffer_thresholds(high: Any, low: Any) -> tuple[float, float]:
    try:
        high = float(high)
        low = float(low)
    except (TypeError, ValueError) as exc:
        raise ValueError("buffer thresholds must be numeric") from exc
    if not math.isfinite(high) or not math.isfinite(low) or low < 0 or high <= low:
        raise ValueError("buffer high must be greater than buffer low and both must be non-negative")
    return high, low


def _technical_chunks(text: str) -> list[str]:
    """Only split oversized text for the Gateway request limit."""
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


def prepare_candidate_pools(raw_segments: Any) -> list[dict[str, Any]]:
    """Validate one candidate pool per Text Studio paragraph."""
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("segments must be a non-empty array")

    pools: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_round_chars = 0
    for item in raw_segments:
        if not isinstance(item, dict):
            raise ValueError("every segment must be an object")
        segment_id = item.get("id")
        candidates = item.get("candidates")
        if not isinstance(segment_id, str) or not SEGMENT_ID.fullmatch(segment_id):
            raise ValueError("every segment needs a valid id")
        if segment_id in seen:
            raise ValueError(f"duplicate segment id: {segment_id}")
        if not isinstance(candidates, list):
            raise ValueError(f"segment {segment_id} needs candidates")
        cleaned = [value.strip() for value in candidates if isinstance(value, str) and value.strip()]
        if not cleaned:
            raise ValueError(f"segment {segment_id} needs non-empty candidates")
        seen.add(segment_id)
        max_round_chars += max(len(value) for value in cleaned)
        pools.append({"id": segment_id, "candidates": cleaned})
    if max_round_chars > MAX_LIVE_TEXT_CHARS:
        raise ValueError(f"text is too long; maximum is {MAX_LIVE_TEXT_CHARS} characters per round")
    return pools


def choose_candidate_round(
    pools: list[dict[str, Any]],
    previous_indexes: list[int] | None = None,
    rng: random.Random | None = None,
) -> tuple[list[dict[str, str]], list[int]]:
    """Pick one final candidate per paragraph; avoid an identical whole round when possible."""
    rng = rng or random.Random()
    indexes = [rng.randrange(len(pool["candidates"])) for pool in pools]
    if previous_indexes == indexes:
        changeable = [index for index, pool in enumerate(pools) if len(pool["candidates"]) > 1]
        if changeable:
            pool_index = rng.choice(changeable)
            old_index = indexes[pool_index]
            offset = rng.randrange(1, len(pools[pool_index]["candidates"]))
            indexes[pool_index] = (old_index + offset) % len(pools[pool_index]["candidates"])
    segments = [
        {"id": pool["id"], "text": pool["candidates"][indexes[index]]}
        for index, pool in enumerate(pools)
    ]
    return segments, indexes


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
    round_number: int = 0
    looping: bool = False
    client_buffered_seconds: float = 0.0
    client_buffered_segments: int = 0
    backpressure_active: bool = False
    stop_timed_out: bool = False
    stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS

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
            "cache_ready": self.ready_segments,
            "client_connected": self.client_connected,
            "client_buffered_seconds": self.client_buffered_seconds,
            "client_buffered_segments": self.client_buffered_segments,
            "backpressure_active": self.backpressure_active,
            "stop_timed_out": self.stop_timed_out,
            "stop_timeout_seconds": self.stop_timeout_seconds,
            "error": self.error,
            "finished": self.finished,
            "round_number": self.round_number,
            "looping": self.looping,
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
        candidate_pools: list[dict[str, Any]] | None = None,
        buffer_high_seconds: float = DEFAULT_BUFFER_HIGH_SECONDS,
        buffer_low_seconds: float = DEFAULT_BUFFER_LOW_SECONDS,
        stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS,
        context_project: dict | None = None,
    ) -> None:
        self.session_id = session_id
        self.voice = voice
        self.segments = segments
        self.candidate_pools = candidate_pools or []
        self.context_project = context_project
        self.variant_selection: list[dict] = []
        self.looping = bool(self.candidate_pools or self.context_project)
        self._synthesize = synthesize
        self._enqueue = enqueue
        self._cache_status = cache_status
        self.playback_speed = playback_speed
        self.volume = volume
        self.buffer_high_seconds, self.buffer_low_seconds = _buffer_thresholds(buffer_high_seconds, buffer_low_seconds)
        try:
            self.stop_timeout_seconds = float(stop_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("stop timeout must be numeric") from exc
        if not math.isfinite(self.stop_timeout_seconds) or self.stop_timeout_seconds <= 0:
            raise ValueError("stop timeout must be positive")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._thread = threading.Thread(target=self._run, name=f"live-{session_id}", daemon=True)
        self._random = random.Random()
        self._previous_candidate_indexes: list[int] | None = None
        self._sequence = 0
        self._backpressure_active = False
        self._client_session_seen = False
        self.status = "starting"
        self.generated_segments = 0
        self.total_segments = len(segments)
        self.round_number = 0
        self.error = ""
        self.finished = False
        self._stop_requested_at: float | None = None

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
            if self.status == "stopping":
                return
            self.status = "stopping"
            self._stop_requested_at = time.monotonic()
            self._stop.set()
            self._resume.set()

    def snapshot(self) -> LiveSnapshot:
        with self._lock:
            status = self.status
            generated = self.generated_segments
            total = self.total_segments
            error = self.error
            finished = self.finished
            round_number = self.round_number
            backpressure_active = self._backpressure_active
            stop_requested_at = self._stop_requested_at
            stop_timeout_seconds = self.stop_timeout_seconds
        stop_timed_out = bool(
            status == "stopping"
            and stop_requested_at is not None
            and time.monotonic() - stop_requested_at >= stop_timeout_seconds
        )
        cache = self._cache_status(self.session_id)
        client_state = cache.get("client_state") if isinstance(cache.get("client_state"), dict) else {}
        return LiveSnapshot(
            self.session_id,
            status,
            generated,
            total,
            int(cache.get("ready", 0) or 0),
            int(cache.get("processing", 0) or 0),
            int(cache.get("completed", 0) or 0),
            int(cache.get("failed", 0) or 0),
            bool(cache.get("client_connected", False)),
            error,
            finished,
            round_number,
            self.looping,
            float(client_state.get("buffered_seconds", 0) or 0),
            int(client_state.get("buffered_segments", 0) or 0),
            backpressure_active,
            stop_timed_out,
            stop_timeout_seconds,
        )

    def _finalize_thread(self) -> None:
        """Publish stopped only after the worker has actually returned."""
        with self._lock:
            if self.status == "stopping":
                self.status = "stopped"
                self.finished = False

    def _round_segments(self) -> list[dict[str, str]]:
        if self.context_project:
            groups = self.context_project["context_groups"]
            selected, indexes = choose_variant_round(self.context_project["paragraphs"], groups,
                                                     self._previous_candidate_indexes, self._random)
            self._previous_candidate_indexes = indexes
            self.variant_selection = [{"group_id": g["id"], "revision": g["revision"],
                                       "variant_id": g["variants"][index]["id"]}
                                      for g, index in zip(groups, indexes)]
            return semantic_blocks(selected)
        if not self.looping:
            if self.round_number:
                return []
            return self.segments
        selected, indexes = choose_candidate_round(
            self.candidate_pools,
            self._previous_candidate_indexes,
            self._random,
        )
        self._previous_candidate_indexes = indexes
        return prepare_live_segments(selected)

    def _wait_for_buffer_capacity(self) -> None:
        """Pause synthesis while the Windows player has a healthy local buffer."""
        while not self._stop.is_set():
            self._resume.wait()
            if self._stop.is_set():
                return
            cache = self._cache_status(self.session_id)
            client_state = cache.get("client_state") if isinstance(cache.get("client_state"), dict) else {}
            valid_state = client_state.get("session_id") == self.session_id
            if valid_state:
                self._client_session_seen = True
            elif not self._client_session_seen:
                # Bootstrap: Windows cannot report this session until the first
                # generated item reaches it, so allow initial synthesis.
                with self._lock:
                    self._backpressure_active = False
                return
            else:
                # Once Windows has acknowledged this session, a missing/stale
                # state means there is no trustworthy consumer state. Wait
                # rather than generating an unbounded backlog on the Mac.
                with self._lock:
                    self._backpressure_active = True
                self._stop.wait(BUFFER_POLL_SECONDS)
                continue

            try:
                buffered_seconds = max(0.0, float(client_state.get("buffered_seconds", 0) or 0))
            except (TypeError, ValueError):
                buffered_seconds = 0.0

            with self._lock:
                active = self._backpressure_active
            if active:
                if buffered_seconds <= self.buffer_low_seconds:
                    with self._lock:
                        self._backpressure_active = False
                    return
                self._stop.wait(BUFFER_POLL_SECONDS)
                continue
            if buffered_seconds >= self.buffer_high_seconds:
                with self._lock:
                    self._backpressure_active = True
                self._stop.wait(BUFFER_POLL_SECONDS)
                continue
            return

    def _synthesize_with_recovery(self, text: str, segment_id: str) -> bytes | None:
        """Retain the current segment until synthesis succeeds or Live is stopped."""
        if self.context_project and len(text) > MAX_LIVE_SEGMENT_CHARS:
            raise LiveSessionError("resolved context block exceeds Gateway text limit", 400)
        retry_count = 0
        while not self._stop.is_set():
            # The caller gates the first attempt before resolving dynamic text.
            if retry_count:
                self._resume.wait()
                self._wait_for_buffer_capacity()
            if self._stop.is_set():
                return None
            try:
                return self._synthesize(text, self.voice)
            except RetryableTTSClientError as exc:
                retry_count += 1
                delay = exc.retry_delay(retry_count)
                print(json.dumps({
                    "event": "TTS temporary error", "session_id": self.session_id,
                    "segment": segment_id, "round": self.round_number,
                    "sequence": self._sequence + 1, "status": exc.status_code,
                    "retryable": True, "retry_after": exc.retry_after,
                    "backoff_seconds": delay, "retry_count": retry_count,
                    "session_status": self.status, "error": str(exc),
                }), flush=True)
                # Stop wakes immediately; Pause gates every subsequent request.
                if self._stop.wait(delay):
                    return None
        return None

    def _run(self) -> None:
        try:
            with self._lock:
                if self.status == "starting":
                    self.status = "running"
            while not self._stop.is_set():
                round_segments = self._round_segments()
                if not round_segments:
                    break
                with self._lock:
                    self.round_number += 1
                    self.generated_segments = 0
                    self.total_segments = len(round_segments)
                for position, segment in enumerate(round_segments, 1):
                    if self._stop.is_set():
                        return
                    self._resume.wait()
                    if self._stop.is_set():
                        return
                    self._wait_for_buffer_capacity()
                    if self._stop.is_set():
                        return
                    speech_text = resolve_dynamic_time(segment["text"])
                    audio = self._synthesize_with_recovery(speech_text, segment["id"])
                    if audio is None or self._stop.is_set():
                        return
                    self._sequence += 1
                    item_id = f"{self.session_id}-r{self.round_number:06d}-s{position:04d}"
                    self._enqueue(item_id, self._sequence, speech_text, self.voice, audio)
                    with self._lock:
                        self.generated_segments += 1
                if not self.looping:
                    break
            with self._lock:
                if self.status != "failed" and not self._stop.is_set():
                    self.status = "stopped"
                    self.finished = True
        except Exception as exc:
            with self._lock:
                if not self._stop.is_set():
                    self.status = "failed"
                    self.error = str(exc)
        finally:
            self._finalize_thread()


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
        buffer_high_seconds: float = DEFAULT_BUFFER_HIGH_SECONDS,
        buffer_low_seconds: float = DEFAULT_BUFFER_LOW_SECONDS,
        stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.cache_url = cache_url.rstrip("/")
        self.cache_api_key = cache_api_key
        self._tts = TTSClient(self.gateway_url, tts_api_key or load_api_key())
        self._synthesize_impl = synthesize
        self._enqueue_impl = enqueue
        self._cache_status_impl = cache_status
        self._cache_cleanup_impl = cache_cleanup
        self.buffer_high_seconds, self.buffer_low_seconds = _buffer_thresholds(buffer_high_seconds, buffer_low_seconds)
        try:
            self.stop_timeout_seconds = float(stop_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("stop timeout must be numeric") from exc
        if not math.isfinite(self.stop_timeout_seconds) or self.stop_timeout_seconds <= 0:
            raise ValueError("stop timeout must be positive")
        self._lock = threading.RLock()
        self._session: LiveSession | None = None

    def start(self, voice: str, segments: Any, playback_speed: Any = 1.0, volume: Any = 100.0) -> dict[str, Any]:
        if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
            raise LiveSessionError("voice is invalid", 400)
        playback_speed, volume = _live_audio_settings(playback_speed, volume)
        try:
            context_project = prepare_context_live(segments) if isinstance(segments, dict) else None
            loop_mode = bool(
                isinstance(segments, list)
                and segments
                and all(isinstance(item, dict) and "candidates" in item for item in segments)
            )
            candidate_pools = prepare_candidate_pools(segments) if loop_mode else []
            prepared_segments = [] if loop_mode or context_project else prepare_live_segments(segments)
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
                prepared_segments,
                self._synthesize,
                self._enqueue,
                self._cache_status,
                playback_speed,
                volume,
                candidate_pools=candidate_pools,
                context_project=context_project,
                buffer_high_seconds=self.buffer_high_seconds,
                buffer_low_seconds=self.buffer_low_seconds,
                stop_timeout_seconds=self.stop_timeout_seconds,
            )
            self._session = session
            session.start()
            return {"session_id": session.session_id, "status": "starting", "looping": session.looping}

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
        if not session:
            return LiveSnapshot("", "idle", 0, 0, 0, 0, 0, 0, False).as_dict()
        result = session.snapshot().as_dict()
        if session.context_project:
            result["variant_selection"] = list(session.variant_selection)
        return result

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
        # Live owns retries so its Stop/Pause events control every backoff.
        audio, _latency = self._tts.synthesize(text, voice, retries=0)
        return audio

    def _enqueue(self, item_id: str, sequence: int, text: str, voice: str, audio: bytes) -> None:
        if self._enqueue_impl:
            self._enqueue_impl(item_id, sequence, text, voice, audio)
            return
        session = self._require_session()
        round_position = session.generated_segments + 1
        self._request_json("POST", "/audio/enqueue", {
            "id": item_id,
            "sequence": sequence,
            "source": "live_session",
            "session_id": session.session_id,
            "text": text,
            "voice": voice,
            "playback_speed": session.playback_speed,
            "volume": session.volume,
            "source_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "round_position": round_position,
            "round_total": session.total_segments,
            "looping": session.looping,
            "session_final": bool(not session.looping and session.total_segments and round_position >= session.total_segments),
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }, cache=True, timeout=CACHE_ENQUEUE_TIMEOUT_SECONDS)

    def _cache_status(self, session_id: str) -> dict[str, Any]:
        if self._cache_status_impl:
            return self._cache_status_impl(session_id)
        try:
            body = self._request_json("GET", f"/audio/session-status/{session_id}", cache=True)
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, RuntimeError):
            return {"ready": 0, "processing": 0, "completed": 0, "failed": 0, "client_connected": False, "client_state": {}}

    def _cache_cleanup(self, session_id: str) -> dict[str, Any]:
        if self._cache_cleanup_impl:
            return self._cache_cleanup_impl(session_id)
        try:
            body = self._request_json("POST", "/audio/cleanup", {"session_id": session_id}, cache=True)
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, RuntimeError) as exc:
            raise LiveSessionError(f"audio cache reset failed: {exc}", 502) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        cache: bool = False,
        timeout: float | None = None,
    ) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if cache and self.cache_api_key:
            headers["Authorization"] = f"Bearer {self.cache_api_key}"
        if not cache and self._tts.api_key:
            headers["Authorization"] = f"Bearer {self._tts.api_key}"
        base_url = self.cache_url if cache else self.gateway_url
        request = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout if timeout is not None else (3 if cache else 10)) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or f"HTTP {exc.code}") from exc


def build_live_manager(gateway_url: str, cache_url: str, tts_api_key: str = "", cache_api_key: str = "") -> LiveSessionManager:
    high = os.environ.get("LIVE_BUFFER_HIGH_SECONDS", str(DEFAULT_BUFFER_HIGH_SECONDS))
    low = os.environ.get("LIVE_BUFFER_LOW_SECONDS", str(DEFAULT_BUFFER_LOW_SECONDS))
    stop_timeout = os.environ.get("LIVE_STOP_TIMEOUT_SECONDS", str(STOP_TIMEOUT_SECONDS))
    return LiveSessionManager(
        gateway_url,
        cache_url,
        tts_api_key,
        cache_api_key,
        buffer_high_seconds=high,
        buffer_low_seconds=low,
        stop_timeout_seconds=stop_timeout,
    )
