"""Direct model-driven timeline speech generalization."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request

GENERALIZE_STRATEGY_VERSION = "direct-model-v1"


class TimelineError(RuntimeError):
    pass


class SegmentFailure(TimelineError):
    def __init__(self, reason: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.stage = stage


class LLMResponseError(SegmentFailure):
    pass


def estimate_duration(text: str, chars_per_second: float = 4.5) -> float:
    if chars_per_second <= 0:
        raise ValueError("chars_per_second must be positive")
    content = re.sub(r"\s+", "", text)
    if not content:
        return 0.0
    punctuation = len(re.findall(r"[，。！？；：、,.!?;:]", content))
    return len(content) / chars_per_second + punctuation * 0.12


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _json_from_response(raw: str) -> Dict[str, Any]:
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        decoded = None
        for match in re.finditer(r"\{", value):
            try:
                decoded, _ = decoder.raw_decode(value[match.start():])
                break
            except json.JSONDecodeError:
                continue
        if decoded is None:
            raise ValueError("Ollama returned no JSON object")
    if not isinstance(decoded, dict):
        raise ValueError("Ollama response must be a JSON object")
    return decoded


class Ollama:
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: int = 180, json_retry_count: int = 2) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.json_retry_count = max(0, json_retry_count)
        self.last_json_retry_count = 0
        self.last_latency_ms = 0.0

    def json(self, prompt: str, schema_hint: str = "", temperature: float = 0.7) -> Dict[str, Any]:
        started = time.monotonic()
        temperatures = [temperature, 0.3, 0.1][: self.json_retry_count + 1]
        last_error = "invalid JSON"
        for attempt, temp in enumerate(temperatures):
            retry_prompt = prompt
            if attempt:
                retry_prompt += f"\n只返回合法 JSON，不要解释。格式：{schema_hint}"
            payload = json.dumps({
                "model": self.model,
                "prompt": retry_prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": temp},
            }).encode("utf-8")
            req = request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    body = json.load(response)
                if not isinstance(body, dict) or not isinstance(body.get("response"), str):
                    raise ValueError("Ollama response has no text payload")
                result = _json_from_response(body["response"])
                self.last_json_retry_count = attempt
                self.last_latency_ms = (time.monotonic() - started) * 1000
                return result
            except ValueError as exc:
                last_error = str(exc)
                self.last_json_retry_count = attempt
            except (OSError, error.URLError) as exc:
                reason = "ollama_timeout" if "timed out" in str(exc).lower() else "ollama_connection_error"
                raise LLMResponseError(reason, "llm", str(exc)) from exc
        raise LLMResponseError("json_parse_error", "llm", last_error)


@dataclass
class Segment:
    id: str
    start: float
    end: float
    original_text: str
    candidates: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    speech_end: Optional[float] = None
    pause_after: float = 0.0

    @property
    def target_duration(self) -> float:
        return max(0.0, (self.speech_end if self.speech_end is not None else self.end) - self.start)


def _rewrite_prompt(segment: Segment, count: int) -> str:
    return f"""你是直播话术改写助手。
请把下面原话直接改写成 {count} 个自然、完整、可直接朗读的直播口语版本。

要求：
- 保持原话的意思和事实。
- 数字、价格、数量、规格、优惠条件、产品名等关键信息不要改错。
- 不要编造原话没有的信息。
- 不要为了凑时长加废话，也不要故意压缩成摘要。
- 每个版本都是完整的一段话。
- 直接改写，不要分析，不要输出标签、要点、评分或解释。

原话：
{segment.original_text}

只返回：
{{"candidates":["版本1","版本2","版本3"]}}
"""


class TimelineEngine:
    def __init__(self, llm: Any, variant_count: int = 5, cooldown_window: int = 3, seed: Optional[int] = None) -> None:
        if variant_count < 1 or cooldown_window < 0:
            raise ValueError("invalid timeline configuration")
        self.llm = llm
        self.variant_count = variant_count
        self.cooldown_window = cooldown_window
        self.random = random.Random(seed)
        self.rewrite_calls = 0
        self.retry_calls = 0
        self.json_retry_total = 0

    def _ask(self, prompt: str) -> Dict[str, Any]:
        try:
            result = self.llm.json(prompt, schema_hint='{"candidates":["..."]}', temperature=0.7)
        except TypeError:
            result = self.llm.json(prompt)
        self.json_retry_total += int(getattr(self.llm, "last_json_retry_count", 0))
        return result

    def rewrite(self, segment: Segment) -> None:
        self.rewrite_calls += 1
        result = self._ask(_rewrite_prompt(segment, self.variant_count))
        candidates = result.get("candidates") if isinstance(result, dict) else None
        if not isinstance(candidates, list) or not all(isinstance(value, str) for value in candidates):
            raise SegmentFailure("schema_error", "rewrite", "model response needs candidates array")
        segment.candidates = list(dict.fromkeys(_clean_text(value) for value in candidates if _clean_text(value)))
        if not segment.candidates:
            raise SegmentFailure("empty_candidates", "rewrite", "model returned no usable candidate text")

    def process_one(self, segment: Segment, max_retries: int = 2) -> Dict[str, Any]:
        last_error: Optional[SegmentFailure] = None
        for attempt in range(max(0, max_retries) + 1):
            try:
                if attempt:
                    self.retry_calls += 1
                    segment.candidates = []
                self.rewrite(segment)
                return {"segment": _segment_to_json(segment), "attempts": attempt + 1}
            except SegmentFailure as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise SegmentFailure("unknown", "rewrite", f"{segment.id}: unknown failure")

    def process(self, segments: List[Segment]) -> Dict[str, Any]:
        return {"segments": [self.process_one(segment)["segment"] for segment in segments]}

    def choose(self, segment: Segment, history: Optional[List[str]] = None) -> str:
        history = history or []
        available = [value for value in segment.candidates if value not in history] or segment.candidates
        if not available:
            raise TimelineError(f"{segment.id}: no candidate")
        return self.random.choice(available)

    def process_batch(self, segments: List[Segment], max_retries: int = 2, completed=None, checkpoint=None, progress=None) -> Dict[str, Any]:
        completed = completed or {}
        processed: Dict[str, Dict[str, Any]] = {}
        failures = []
        stats = {"rewrite_calls": 0, "retry_calls": 0, "json_retry_count": 0, "failure_reasons": {}}

        for index, segment in enumerate(segments, 1):
            previous = completed.get(segment.id)
            if previous and previous.get("generalize_strategy_version") == GENERALIZE_STRATEGY_VERSION and previous.get("segment_fingerprint") == _segment_fingerprint(segment):
                processed[segment.id] = previous
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} RESUME")
                continue

            try:
                output = self.process_one(segment, max_retries=max_retries)["segment"]
                output.update({"generalize_status": "completed", "status": "completed"})
                processed[segment.id] = output
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} PASS direct_model")
            except SegmentFailure as exc:
                output = _segment_to_json(segment)
                output.update({
                    "generalize_status": "failed",
                    "status": "failed",
                    "fallback": "original",
                    "fallback_text": segment.original_text,
                    "failure_reason": exc.reason,
                })
                processed[segment.id] = output
                failures.append({"segment_id": segment.id, "stage": exc.stage, "reason": exc.reason, "last_error": str(exc)})
                stats["failure_reasons"][exc.reason] = stats["failure_reasons"].get(exc.reason, 0) + 1
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} FAIL {exc.reason}")

            stats.update({
                "rewrite_calls": self.rewrite_calls,
                "retry_calls": self.retry_calls,
                "json_retry_count": self.json_retry_total,
            })
            if checkpoint:
                checkpoint({
                    "segments": [processed[item.id] for item in segments if item.id in processed],
                    "failed_segments": failures,
                    "stats": stats,
                })

        return {
            "segments": [processed[item.id] for item in segments],
            "total": len(segments),
            "success": len(segments) - len(failures),
            "failed": len(failures),
            "failed_segments": failures,
            "stats": stats,
        }


def _segment_to_json(segment: Segment) -> Dict[str, Any]:
    data = {
        "id": segment.id,
        "start": segment.start,
        "end": segment.end,
        "original_text": segment.original_text,
        "candidates": segment.candidates,
        "mode": "atomic",
        "strategy": "direct_model",
        "generalize_strategy_version": GENERALIZE_STRATEGY_VERSION,
        "segment_fingerprint": _segment_fingerprint(segment),
        "speech_end": segment.speech_end if segment.speech_end is not None else segment.end,
        "speech_duration": segment.target_duration,
        "pause_after": segment.pause_after,
        "timeline_duration": segment.end - segment.start,
        "fallback_text": segment.original_text,
    }
    data.update(segment.metadata)
    return data


def _segment_fingerprint(segment: Segment) -> str:
    value = json.dumps({
        "id": segment.id,
        "start": segment.start,
        "end": segment.end,
        "speech_duration": segment.target_duration,
        "text": segment.original_text,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def load_segments(path: Path) -> List[Segment]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    values = raw.get("segments") if isinstance(raw, dict) else raw
    if not isinstance(values, list) or not values:
        raise TimelineError("input must contain a non-empty segments array")

    segments = []
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise TimelineError("every segment must be an object")
        text = _clean_text(value.get("original_text", value.get("text")))
        if not text:
            raise TimelineError(f"segment {index} has empty text")
        try:
            start = float(value["start"])
            end = float(value["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineError(f"segment {index} needs numeric start and end") from exc
        if start < 0 or end <= start:
            raise TimelineError(f"segment {index} has invalid timestamps")

        speech_end = float(value.get("speech_end", end))
        pause_after = float(value.get("pause_after", max(0.0, end - speech_end)))
        metadata = {
            key: value[key]
            for key in ("parent_segment_id", "source_segment_id", "source_segment_ids", "source_start", "source_end")
            if key in value
        }
        segments.append(Segment(
            id=_clean_text(value.get("id")) or f"seg_{index:04d}",
            start=start,
            end=end,
            original_text=text,
            candidates=[_clean_text(item) for item in value.get("candidates", []) if _clean_text(item)],
            metadata=metadata,
            speech_end=speech_end,
            pause_after=pause_after,
        ))
    return segments
