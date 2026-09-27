"""Prepare startup inventory, then continuously synthesize new random rounds."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

if __package__:
    from .live_session import LiveSessionError, _DYNAMIC_TIME_TOKEN
    from .live_session_blocks import SynthesisBlockLiveSession, prepare_synthesis_blocks
else:
    from live_session import LiveSessionError, _DYNAMIC_TIME_TOKEN
    from live_session_blocks import SynthesisBlockLiveSession, prepare_synthesis_blocks


PREPARED_ROUND_COUNT = 1
PREPARED_AUDIO_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "prepared-live"


@dataclass
class PreparedBlock:
    text: str
    path: Path


@dataclass
class PreparedRound:
    blocks: list[PreparedBlock]
    variant_selection: list[dict]


class PreparedLiveSession(SynthesisBlockLiveSession):
    """Prepare the initial inventory, then keep synthesizing new random rounds."""

    def __init__(self, *args, audio_root: Path | None = None, first_round: dict | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.first_round = first_round
        self.audio_root = (audio_root or PREPARED_AUDIO_ROOT) / self.session_id
        self.phase = "preparing"
        self.prepared_segments = 0
        self.preparation_total_segments = 0
        self.prepared_rounds = 0
        self.prepared_round_number = 0

    def snapshot(self):
        result = super().snapshot()
        with self._lock:
            result.phase = self.phase
            result.prepared_segments = self.prepared_segments
            result.preparation_total_segments = self.preparation_total_segments
            result.prepared_rounds = self.prepared_rounds
            result.preparation_total_rounds = PREPARED_ROUND_COUNT
            result.prepared_round_number = self.prepared_round_number
        return result

    def _prepare(self) -> list[PreparedRound] | None:
        plans = []
        for _ in range(PREPARED_ROUND_COUNT):
            if self.first_round:
                blocks = self.first_round["blocks"]
                self._previous_candidate_indexes = list(self.first_round["candidate_indexes"])
                self.variant_selection = list(self.first_round["variant_selection"])
            else:
                segments = self._round_segments()
                blocks = segments if self.context_project else prepare_synthesis_blocks(segments)
            if not blocks:
                raise LiveSessionError("没有可准备的播报内容", 400)
            if any(_DYNAMIC_TIME_TOKEN.search(block["text"]) for block in blocks):
                raise LiveSessionError(
                    "预生成循环暂不支持实时日期/时间占位符；请将其改为不依赖当前时间的话术后重新开始。", 400
                )
            plans.append((blocks, list(self.variant_selection)))
        with self._lock:
            self.variant_selection = []
            self.preparation_total_segments = sum(len(blocks) for blocks, _ in plans)
        self.audio_root.mkdir(parents=True, exist_ok=True)
        rounds = []
        for blocks, selection in plans:
            prepared = []
            for block in blocks:
                self._resume.wait()
                if self._stop.is_set():
                    return None
                text = block["text"]
                path = self.audio_root / (hashlib.sha256(text.encode("utf-8")).hexdigest() + ".wav")
                ready = (self.first_round or {}).get("audio_paths", {}).get(text)
                if ready is not None and ready.is_file():
                    path = ready
                if not path.is_file():
                    audio = self._synthesize_with_recovery(text, block["id"])
                    if audio is None or self._stop.is_set():
                        return None
                    # Reuse across sessions is handled by the Gateway cache;
                    # these files hold the first round until it is complete.
                    path.write_bytes(audio)
                prepared.append(PreparedBlock(text, path))
                with self._lock:
                    self.prepared_segments += 1
            rounds.append(PreparedRound(prepared, selection))
            with self._lock:
                self.prepared_rounds += 1
        return rounds

    def _run(self) -> None:
        try:
            rounds = self._prepare()
            if rounds is None:
                return
            self._resume.wait()
            if self._stop.is_set():
                return
            with self._lock:
                self.phase = "playing"
                if self.status == "starting":
                    self.status = "running"
            current = rounds[0]
            with self._lock:
                self.round_number = 1
                self.prepared_round_number = 1
                self.generated_segments = 0
                self.total_segments = len(current.blocks)
                self.variant_selection = list(current.variant_selection)
            for position, block in enumerate(current.blocks, 1):
                self._resume.wait()
                if self._stop.is_set():
                    return
                audio = block.path.read_bytes()
                if self._stop.is_set():
                    return
                self._sequence += 1
                item_id = f"{self.session_id}-r{self.round_number:06d}-s{position:04d}"
                self._enqueue(item_id, self._sequence, block.text, self.voice, audio)
                with self._lock:
                    self.generated_segments += 1
            with self._lock:
                self.prepared_round_number = 0
            # Continue the existing ordered synthesis loop with the same
            # sequence counter and candidate-selection history. No replay.
            super()._run()
        except Exception as exc:
            with self._lock:
                if not self._stop.is_set():
                    self.status = "failed"
                    self.error = str(exc)
        finally:
            self._finalize_thread()
