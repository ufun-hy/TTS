"""Live-session synthesis blocks that reduce repeated TTS onset artifacts."""

from __future__ import annotations

import os
import uuid
from io import BytesIO
import time
import wave
from typing import Any, Callable

if __package__:
    from .live_session import (
        BUFFER_POLL_SECONDS,
        DEFAULT_BUFFER_HIGH_SECONDS,
        DEFAULT_BUFFER_LOW_SECONDS,
        VOICE_ID,
        LiveSession,
        LiveSessionError,
        LiveSessionManager,
        _live_audio_settings,
        prepare_candidate_pools,
        prepare_live_segments,
        resolve_dynamic_time,
        STOP_TIMEOUT_SECONDS,
        RuntimeManager,
        RuntimeBusyError,
        RuntimeStage,
    )
else:
    from live_session import (
        BUFFER_POLL_SECONDS,
        DEFAULT_BUFFER_HIGH_SECONDS,
        DEFAULT_BUFFER_LOW_SECONDS,
        VOICE_ID,
        LiveSession,
        LiveSessionError,
        LiveSessionManager,
        _live_audio_settings,
        prepare_candidate_pools,
        prepare_live_segments,
        resolve_dynamic_time,
        STOP_TIMEOUT_SECONDS,
        RuntimeManager,
        RuntimeBusyError,
        RuntimeStage,
    )


MAX_SYNTHESIS_BLOCK_CHARS = 180
SYNTHESIS_BLOCK_SEPARATOR = "\n"


def prepare_synthesis_blocks(segments: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge adjacent live segments without changing their upstream logical segmentation.

    The TTS Gateway accepts at most 200 characters. We target 180 characters so
    paragraph separators still fit comfortably while reducing how often
    CosyVoice has to start a fresh utterance. A single technical chunk may be
    longer than the target (up to the Gateway limit) and remains a block by
    itself.
    """
    blocks: list[dict[str, str]] = []
    current_texts: list[str] = []
    current_ids: list[str] = []
    current_length = 0

    def flush() -> None:
        nonlocal current_texts, current_ids, current_length
        if not current_texts:
            return
        blocks.append({
            "id": current_ids[0],
            "text": SYNTHESIS_BLOCK_SEPARATOR.join(current_texts),
        })
        current_texts = []
        current_ids = []
        current_length = 0

    for segment in segments:
        text = segment["text"]
        separator_length = len(SYNTHESIS_BLOCK_SEPARATOR) if current_texts else 0
        proposed_length = current_length + separator_length + len(text)
        if current_texts and proposed_length > MAX_SYNTHESIS_BLOCK_CHARS:
            flush()
            separator_length = 0

        current_ids.append(segment["id"])
        current_texts.append(text)
        current_length += separator_length + len(text)

        # A single technical chunk can be 181-200 chars. Keep it intact but do
        # not append another logical segment to it.
        if current_length > MAX_SYNTHESIS_BLOCK_CHARS:
            flush()

    flush()
    return blocks


class SynthesisBlockLiveSession(LiveSession):
    """LiveSession that synthesizes adjacent short segments as one utterance."""

    def _wait_for_buffer_capacity(self) -> None:
        """Resume synthesis when a connected Windows client reports no session buffer.

        The base LiveSession treats a missing current-session state as unsafe
        after that session has been seen once. In practice the Windows client
        can temporarily report no current session once its local buffer drains
        or cached session files disappear, while it is still actively polling.
        Keeping backpressure active in that state deadlocks synthesis forever.

        For the synthesis-block live path, an actively connected client with no
        current-session state means there is no trustworthy positive buffer to
        protect, so release backpressure and refill. A truly disconnected client
        still pauses generation to avoid an unbounded Mac-side backlog.
        """
        while not self._stop.is_set():
            self._resume.wait()
            if self._stop.is_set():
                return

            cache = self._cache_status(self.session_id)
            client_state = cache.get("client_state") if isinstance(cache.get("client_state"), dict) else {}
            client_connected = bool(cache.get("client_connected", False))
            valid_state = client_state.get("session_id") == self.session_id

            if valid_state:
                self._client_session_seen = True
            elif not self._client_session_seen:
                # Bootstrap: Windows cannot report this session until the first
                # generated item reaches it, so allow initial synthesis.
                with self._lock:
                    self._backpressure_active = False
                return
            elif client_connected:
                # Windows is still polling but no longer reports buffered audio
                # for this session. Treat that as an empty buffer and refill
                # immediately instead of leaving backpressure stuck forever.
                with self._lock:
                    self._backpressure_active = False
                return
            else:
                # Once Windows has acknowledged this session, only a genuine
                # disconnect/stale client state should stop generation.
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

    def _run(self) -> None:
        try:
            with self._lock:
                if self.status == "starting":
                    self.status = "running"
            while not self._stop.is_set():
                round_segments = self._round_segments()
                if not round_segments:
                    break
                synthesis_blocks = prepare_synthesis_blocks(round_segments)
                with self._lock:
                    self.round_number += 1
                    self.generated_segments = 0
                    self.total_segments = len(synthesis_blocks)
                for position, block in enumerate(synthesis_blocks, 1):
                    if self._stop.is_set():
                        return
                    self._resume.wait()
                    if self._stop.is_set():
                        return
                    self._wait_for_buffer_capacity()
                    if self._stop.is_set():
                        return
                    speech_text = resolve_dynamic_time(block["text"])
                    synthesis_started = time.monotonic()
                    audio = self._synthesize(speech_text, self.voice)
                    synthesis_seconds = time.monotonic() - synthesis_started
                    try:
                        with wave.open(BytesIO(audio), "rb") as handle:
                            audio_seconds = handle.getnframes() / max(1, handle.getframerate())
                    except (OSError, EOFError, wave.Error):
                        audio_seconds = 0.0
                    with self._lock:
                        self._synthesis_seconds += synthesis_seconds
                        self._audio_seconds += audio_seconds
                    self._sequence += 1
                    item_id = f"{self.session_id}-r{self.round_number:06d}-s{position:04d}"
                    self._enqueue(item_id, self._sequence, speech_text, self.voice, audio)
                    with self._lock:
                        self.generated_segments += 1
                    if self._stop.is_set():
                        return
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


class SynthesisBlockLiveSessionManager(LiveSessionManager):
    """Create synthesis-block sessions while retaining the existing cache bridge."""

    def start(
        self,
        voice: str,
        segments: Any,
        playback_speed: Any = 1.0,
        volume: Any = 100.0,
    ) -> dict[str, Any]:
        if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
            raise LiveSessionError("voice is invalid", 400)
        playback_speed, volume = _live_audio_settings(playback_speed, volume)
        try:
            loop_mode = bool(
                isinstance(segments, list)
                and segments
                and all(isinstance(item, dict) and "candidates" in item for item in segments)
            )
            candidate_pools = prepare_candidate_pools(segments) if loop_mode else []
            prepared_segments = [] if loop_mode else prepare_live_segments(segments)
        except ValueError as exc:
            raise LiveSessionError(str(exc), 400) from exc

        runtime_lease = None
        if self.runtime:
            try:
                runtime_lease = self.runtime.acquire(RuntimeStage.LIVE, "live-session", "CosyVoice3")
            except RuntimeBusyError as exc:
                raise LiveSessionError(str(exc), 409) from exc
        with self._lock:
            if self._session:
                if runtime_lease and self.runtime:
                    self.runtime.release(runtime_lease)
                if self._session.status in ("starting", "running", "paused"):
                    raise LiveSessionError("a live session is already running")
                raise LiveSessionError("reset the previous live session before starting a new one")
            session = SynthesisBlockLiveSession(
                uuid.uuid4().hex[:12],
                voice.strip(),
                prepared_segments,
                self._synthesize,
                self._enqueue,
                self._cache_status,
                playback_speed,
                volume,
                candidate_pools=candidate_pools,
                buffer_high_seconds=self.buffer_high_seconds,
                buffer_low_seconds=self.buffer_low_seconds,
                stop_timeout_seconds=self.stop_timeout_seconds,
            )
            self._session = session
            self._runtime_lease = runtime_lease
            session.start()
            if runtime_lease:
                import threading
                threading.Thread(target=self._release_runtime_after_session, args=(session, runtime_lease), daemon=True, name="live-runtime-release").start()
            return {
                "session_id": session.session_id,
                "status": "starting",
                "looping": session.looping,
            }


def build_live_manager(
    gateway_url: str,
    cache_url: str,
    tts_api_key: str = "",
    cache_api_key: str = "",
    runtime: RuntimeManager | None = None,
    confirm_tts_release: Callable[[], bool] | None = None,
) -> SynthesisBlockLiveSessionManager:
    high = os.environ.get("LIVE_BUFFER_HIGH_SECONDS", str(DEFAULT_BUFFER_HIGH_SECONDS))
    low = os.environ.get("LIVE_BUFFER_LOW_SECONDS", str(DEFAULT_BUFFER_LOW_SECONDS))
    stop_timeout = os.environ.get("LIVE_STOP_TIMEOUT_SECONDS", str(STOP_TIMEOUT_SECONDS))
    return SynthesisBlockLiveSessionManager(
        gateway_url,
        cache_url,
        tts_api_key,
        cache_api_key,
        buffer_high_seconds=high,
        buffer_low_seconds=low,
        stop_timeout_seconds=stop_timeout,
        runtime=runtime,
        confirm_tts_release=confirm_tts_release,
    )
