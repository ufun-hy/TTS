"""Lookahead generation and strictly ordered playback."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import statistics
import threading
import time
from typing import Any, Dict, List, Optional

from .cache import AudioCache
from .player import Player
from .runtime_selector import RuntimeSelector, Selection
from .session import RuntimeSession, utc_now
from .tts_client import TTSClient, TTSClientError


@dataclass
class Artifact:
    index: int
    segment_id: str
    selection: Selection
    path: Optional[Path]
    audio_duration: Optional[float]
    tts_latency_ms: float
    cache_hit: bool


class LookaheadScheduler:
    def __init__(
        self,
        timeline: Dict[str, Any],
        session: RuntimeSession,
        session_path: Path,
        report_path: Path,
        selector: RuntimeSelector,
        cache: AudioCache,
        tts: Optional[TTSClient],
        player: Player,
        voice: str,
        lookahead: int = 3,
        dry_run: bool = False,
        max_duration_retry: int = 2,
    ) -> None:
        self.timeline = timeline
        self.segments = timeline["segments"]
        self.session = session
        self.session_path = session_path
        self.report_path = report_path
        self.selector = selector
        self.cache = cache
        self.tts = tts
        self.player = player
        self.voice = voice
        self.lookahead = max(1, lookahead)
        self.dry_run = dry_run
        self.max_duration_retry = max(0, max_duration_retry)
        self.futures: Dict[int, Future[Artifact]] = {}
        self.started_at = time.monotonic()
        self.state_lock = threading.RLock()

    def run(self) -> Dict[str, Any]:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="timeline-tts") as executor:
            while self.session.current_index < len(self.segments):
                current = self.session.current_index
                for index in range(current, min(len(self.segments), current + self.lookahead)):
                    self._schedule(index, executor)
                self._print_state(current)
                future = self.futures[current]
                if current > 0 and not self.dry_run and not future.done():
                    self.session.buffer_underrun_count += 1
                artifact = future.result()
                self.session.current_segment = artifact.segment_id
                self.session.statuses[artifact.segment_id] = "playing"
                failed = artifact.path is None and not self.dry_run
                if failed:
                    play_start = play_end = time.monotonic()
                    self.session.fallback_count += 1
                else:
                    play_start, play_end = self.player.play(artifact.path)
                actual = artifact.audio_duration
                if actual is None:
                    actual = artifact.selection.estimated_duration
                pause_after = float(self.segments[current].get("pause_after", 0.0))
                record = {
                    "segment_id": artifact.segment_id,
                    "selected_mode": artifact.selection.mode,
                    "selected_variant_ids": artifact.selection.variant_ids,
                    "combination_id": artifact.selection.combination_id,
                    "final_text": artifact.selection.final_text,
                    "estimated_duration": artifact.selection.estimated_duration,
                    "audio_duration": actual,
                    "pause_after": pause_after,
                    "tts_latency_ms": round(artifact.tts_latency_ms),
                    "duration_deviation": round((actual - artifact.selection.target_duration) / artifact.selection.target_duration, 4) if artifact.selection.target_duration else 0,
                    "cache_hit": artifact.cache_hit,
                    "fallback": artifact.selection.fallback,
                    "play_start": play_start,
                    "play_end": play_end,
                    "status": "failed" if failed else "played",
                }
                self.session.played_segments.append(record)
                self.session.statuses[artifact.segment_id] = "failed" if failed else "played"
                self.session.current_index += 1
                self.session.current_segment = None
                self.session.ready_audio = [segment_id for segment_id, status in self.session.statuses.items() if status == "ready"]
                self.session.generating = [segment_id for segment_id, status in self.session.statuses.items() if status == "generating"]
                self._save_session()
                if current < len(self.segments) - 1 and pause_after > 0:
                    time.sleep(pause_after)
        report = self._report()
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        with self.report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return report

    def _schedule(self, index: int, executor: ThreadPoolExecutor) -> None:
        if index in self.futures:
            return
        segment_id = str(self.segments[index]["id"])
        self.session.statuses[segment_id] = "generating"
        self.session.generating.append(segment_id)
        self._save_session()
        self.futures[index] = executor.submit(self._generate, index)

    def _generate(self, index: int) -> Artifact:
        with self.state_lock:
            return self._generate_impl(index)

    def _generate_impl(self, index: int) -> Artifact:
        segment = self.segments[index]
        segment_id = str(segment["id"])
        target = float(segment.get("speech_duration", segment.get("duration_target", float(segment["end"]) - float(segment["start"]))))
        last: Optional[Artifact] = None
        for _ in range(self.max_duration_retry + 1):
            selection = self.selector.select(segment, self.session)
            if selection.fallback:
                self.session.fallback_count += 1
            if self.dry_run:
                artifact = Artifact(index, segment_id, selection, None, None, 0.0, False)
                self.session.statuses[segment_id] = "ready"
                return artifact
            if self.tts is None:
                raise RuntimeError("TTS client is required unless --dry-run is used")
            cached = self.cache.lookup(segment_id, selection.final_text, self.voice)
            if cached:
                artifact = Artifact(index, segment_id, selection, cached.path, float(cached.metadata["audio_duration"]), 0.0, True)
            else:
                try:
                    audio, latency = self.tts.synthesize(selection.final_text, self.voice)
                    cached = self.cache.store(segment_id, selection.final_text, self.voice, audio)
                    artifact = Artifact(index, segment_id, selection, cached.path, float(cached.metadata["audio_duration"]), latency, False)
                except (TTSClientError, OSError, ValueError):
                    if _ == self.max_duration_retry:
                        fallback_text = str(segment.get("fallback_text") or "").strip()
                        if fallback_text and fallback_text != selection.final_text:
                            selection = Selection(
                                segment_id,
                                selection.mode,
                                ["fallback"],
                                "fallback",
                                fallback_text,
                                selection.estimated_duration,
                                selection.target_duration,
                                True,
                            )
                            try:
                                audio, latency = self.tts.synthesize(fallback_text, self.voice)
                                cached = self.cache.store(segment_id, fallback_text, self.voice, audio)
                                self.session.fallback_count += 1
                                artifact = Artifact(index, segment_id, selection, cached.path, float(cached.metadata["audio_duration"]), latency, False)
                                self.session.statuses[segment_id] = "ready"
                                return artifact
                            except (TTSClientError, OSError, ValueError):
                                pass
                        self.session.statuses[segment_id] = "failed"
                        # Keep the session moving; the report records the fallback.
                        return Artifact(index, segment_id, selection, None, None, 0.0, False)
                    continue
            last = artifact
            if target <= 0 or abs(artifact.audio_duration - target) / target <= self.selector.tolerance:
                self.session.statuses[segment_id] = "ready"
                return artifact
        if last is None:
            raise RuntimeError(f"{segment_id}: no generated artifact")
        self.session.statuses[segment_id] = "ready"
        return last

    def _save_session(self) -> None:
        with self.state_lock:
            self.session.save(self.session_path)

    def _print_state(self, current: int) -> None:
        ready = [segment_id for segment_id, status in self.session.statuses.items() if status == "ready"]
        generating = [segment_id for segment_id, status in self.session.statuses.items() if status == "generating"]
        print(f"[PLAYING] {self.segments[current]['id']}")
        print(f"[READY] {' '.join(ready) if ready else '-'}")
        print(f"[GENERATING] {' '.join(generating) if generating else '-'}")
        print(f"[BUFFER] {len(ready)} ready")

    def _report(self) -> Dict[str, Any]:
        records = self.session.played_segments
        played = [item for item in records if item["status"] == "played"]
        deviations = [abs(float(item["duration_deviation"])) for item in records]
        latencies = [float(item["tts_latency_ms"]) for item in records if item["tts_latency_ms"]]
        combinations = [item["combination_id"] for item in records]
        atomic = sum(item["selected_mode"] == "atomic" for item in records)
        composable = sum(item["selected_mode"] == "composable" for item in records)
        hits = sum(bool(item["cache_hit"]) for item in records)
        return {
            "session_id": self.session.session_id,
            "timeline_id": self.session.timeline_id,
            "segment_count": len(self.segments),
            "played_count": len(played),
            "atomic_count": atomic,
            "composable_count": composable,
            "unique_combination_rate": round(len(set(combinations)) / len(combinations), 4) if combinations else 0,
            "average_tts_latency": round(statistics.mean(latencies) / 1000, 3) if latencies else 0,
            "average_duration_deviation": round(statistics.mean(deviations), 4) if deviations else 0,
            "cache_hit_rate": round(hits / len(records), 4) if records else 0,
            "buffer_underrun_count": self.session.buffer_underrun_count,
            "fallback_count": self.session.fallback_count,
            "finished_at": utc_now(),
        }
