"""Live-session synthesis blocks that reduce repeated TTS onset artifacts."""

from __future__ import annotations

import os
import uuid
from typing import Any

if __package__:
    from .live_session import (
        DEFAULT_BUFFER_HIGH_SECONDS,
        DEFAULT_BUFFER_LOW_SECONDS,
        VOICE_ID,
        LiveSession,
        LiveSessionError,
        LiveSessionManager,
        _live_audio_settings,
        prepare_candidate_pools,
        prepare_live_segments,
    )
else:
    from live_session import (
        DEFAULT_BUFFER_HIGH_SECONDS,
        DEFAULT_BUFFER_LOW_SECONDS,
        VOICE_ID,
        LiveSession,
        LiveSessionError,
        LiveSessionManager,
        _live_audio_settings,
        prepare_candidate_pools,
        prepare_live_segments,
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
                    audio = self._synthesize(block["text"], self.voice)
                    if self._stop.is_set():
                        return
                    self._sequence += 1
                    item_id = f"{self.session_id}-r{self.round_number:06d}-s{position:04d}"
                    self._enqueue(item_id, self._sequence, block["text"], self.voice, audio)
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

        with self._lock:
            if self._session:
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
            )
            self._session = session
            session.start()
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
) -> SynthesisBlockLiveSessionManager:
    high = os.environ.get("LIVE_BUFFER_HIGH_SECONDS", str(DEFAULT_BUFFER_HIGH_SECONDS))
    low = os.environ.get("LIVE_BUFFER_LOW_SECONDS", str(DEFAULT_BUFFER_LOW_SECONDS))
    return SynthesisBlockLiveSessionManager(
        gateway_url,
        cache_url,
        tts_api_key,
        cache_api_key,
        buffer_high_seconds=high,
        buffer_low_seconds=low,
    )
