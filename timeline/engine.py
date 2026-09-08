"""Minimal, resumable text-layer pipeline for timeline speech generalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
import json
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
import statistics
import time
from typing import Any, Dict, List, Optional
from urllib import error, request


SEGMENT_TYPES = {
    "opening", "welcome", "retention", "product_intro", "feature_explanation",
    "benefit", "pain_point", "usage_scenario", "comparison", "price",
    "promotion", "trust", "faq", "interaction", "cta", "transition",
    "recap", "closing", "other",
}


class TimelineError(RuntimeError):
    """Raised when a timeline cannot be safely processed."""


class SegmentFailure(TimelineError):
    def __init__(self, reason: str, stage: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.stage = stage
        self.details = details or {}


class LLMResponseError(SegmentFailure):
    pass


def estimate_duration(text: str, chars_per_second: float = 4.5) -> float:
    """Estimate Mandarin live-speech duration without calling TTS."""
    if chars_per_second <= 0:
        raise ValueError("chars_per_second must be positive")
    content = re.sub(r"\s+", "", text)
    if not content:
        return 0.0
    punctuation = len(re.findall(r"[，。！？；：、,.!?;:]", content))
    return len(content) / chars_per_second + punctuation * 0.12


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _text_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _json_from_response(raw: str) -> Dict[str, Any]:
    value = raw.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
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
            raise ValueError("Ollama returned no safe JSON object")
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

    def json(self, prompt: str, schema_hint: str = "") -> Dict[str, Any]:
        started = time.monotonic()
        self.last_json_retry_count = 0
        temperatures = [0.8, 0.3, 0.1][: self.json_retry_count + 1]
        last_error = "invalid JSON"
        for attempt, temperature in enumerate(temperatures):
            retry_prompt = prompt
            if attempt:
                retry_prompt += f"\n上一轮返回不是合法 JSON。不要解释，不要 Markdown，不要代码块，只返回一个 JSON Object。Schema：{schema_hint}"
            payload = json.dumps({
                "model": self.model,
                "prompt": retry_prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature},
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
                decoded = _json_from_response(body["response"])
                self.last_json_retry_count = attempt
                self.last_latency_ms = (time.monotonic() - started) * 1000
                return decoded
            except ValueError as exc:
                last_error = str(exc)
                self.last_json_retry_count = attempt
                continue
            except (OSError, error.URLError) as exc:
                reason = "ollama_timeout" if "timed out" in str(exc).lower() else "ollama_connection_error"
                raise LLMResponseError(reason, "llm", str(exc)) from exc
        self.last_latency_ms = (time.monotonic() - started) * 1000
        raise LLMResponseError("json_parse_error", "llm", last_error)


@dataclass
class Analysis:
    segment_type: str = "other"
    intent: str = ""
    facts: List[str] = field(default_factory=list)
    must_keep: List[str] = field(default_factory=list)
    tone: str = "口语、自然、适合直播朗读"
    mode: str = "atomic"


@dataclass
class Slot:
    id: str
    required: bool
    variants: List[str]


@dataclass
class Segment:
    id: str
    start: float
    end: float
    original_text: str
    analysis: Optional[Analysis] = None
    variants: List[str] = field(default_factory=list)
    slots: List[Slot] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def target_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def mode(self) -> str:
        return self.analysis.mode if self.analysis else "atomic"


def _analysis_from_json(raw: Dict[str, Any], strict: bool = False) -> Analysis:
    if strict:
        required = {"segment_type", "intent", "facts", "must_keep", "tone", "mode"}
        if not required.issubset(raw) or any(raw.get(key) is None for key in required):
            raise SegmentFailure("schema_error", "analyze", "analyzer response is missing required fields")
        if not isinstance(raw.get("facts"), list) or not isinstance(raw.get("must_keep"), list):
            raise SegmentFailure("schema_error", "analyze", "facts and must_keep must be arrays")
        if not all(isinstance(raw.get(key), str) for key in ("segment_type", "intent", "tone", "mode")):
            raise SegmentFailure("schema_error", "analyze", "analysis fields must be strings")
    segment_type = _clean_text(raw.get("segment_type")) or "other"
    if segment_type not in SEGMENT_TYPES:
        if strict:
            raise SegmentFailure("schema_error", "analyze", f"invalid segment_type: {segment_type}")
        segment_type = "other"
    mode = _clean_text(raw.get("mode")) or "atomic"
    if mode not in {"atomic", "composable"}:
        if strict:
            raise SegmentFailure("schema_error", "analyze", f"invalid mode: {mode}")
        mode = "atomic"
    facts = _text_list(raw.get("facts"))
    must_keep = _text_list(raw.get("must_keep"))
    for fact in facts:
        if fact not in must_keep:
            must_keep.append(fact)
    return Analysis(
        segment_type=segment_type,
        intent=_clean_text(raw.get("intent")),
        facts=facts,
        must_keep=must_keep,
        tone=_clean_text(raw.get("tone")) or "口语、自然、适合直播朗读",
        mode=mode,
    )


def _segment_from_json(raw: Dict[str, Any], index: int) -> Segment:
    text = _clean_text(raw.get("original_text", raw.get("text")))
    if not text:
        raise TimelineError(f"segment {index} has empty text")
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineError(f"segment {index} needs numeric start and end") from exc
    if start < 0 or end <= start:
        raise TimelineError(f"segment {index} has invalid timestamps")
    analysis_raw = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else None
    if analysis_raw is None and any(key in raw for key in ("segment_type", "intent", "facts", "must_keep", "tone", "mode")):
        analysis_raw = {key: raw.get(key) for key in ("segment_type", "intent", "facts", "must_keep", "tone", "mode")}
    return Segment(
        id=_clean_text(raw.get("id")) or f"seg_{index:04d}",
        start=start,
        end=end,
        original_text=text,
        analysis=_analysis_from_json(analysis_raw) if analysis_raw else None,
        variants=[_clean_text(x) for x in raw.get("variants", []) if _clean_text(x)],
        slots=[Slot(
            id=_clean_text(slot.get("id")),
            required=bool(slot.get("required", True)),
            variants=[_clean_text(x) for x in slot.get("variants", []) if _clean_text(x)],
        ) for slot in raw.get("slots", []) if isinstance(slot, dict) and _clean_text(slot.get("id"))],
        metadata={key: raw[key] for key in ("parent_segment_id", "source_segment_id", "source_start", "source_end") if key in raw},
    )


def _analysis_prompt(segment: Segment) -> str:
    return f"""你是直播话术分析器。只分析原话，不改写，不补充原文没有的商品事实。
输出严格 JSON：{{"segment_type":"...","intent":"...","facts":[],"must_keep":[],"tone":"...","mode":"atomic|composable"}}
facts 和 must_keep 必须列出原话中的所有价格、数字、规格、功能、优惠条件和售后规则。
segment_type 只能从 opening,welcome,retention,product_intro,feature_explanation,benefit,pain_point,usage_scenario,comparison,price,promotion,trust,faq,interaction,cta,transition,recap,closing,other 中选择。
价格、规格、参数、规则、售后和明确产品事实用 atomic；欢迎、互动、过渡、情绪和促单可用 composable。
原话：{segment.original_text}
"""


def _rewrite_prompt(segment: Segment, analysis: Analysis, count: int, attempt: int = 0, failure_context: str = "") -> str:
    common = f"""你是直播话术重新创作者。根据语义骨架重新组织口语表达，不做逐词同义替换。
必须保持原段的销售作用、商品事实、时间轴位置和口语直播感；不得新增全网最低、销量第一、官方认证、医学效果等事实。
目标时长约 {segment.target_duration:.1f} 秒，允许误差 ±15%。生成 {count} 个明显不同的表达。
分析结果：{json.dumps(analysis.__dict__, ensure_ascii=False)}
原话只用于理解语义，不要复用长句：{segment.original_text}
"""
    if attempt:
        common += "上一轮候选未通过审核。请根据失败原因重新组织，不要只重复上一轮。\n"
        common += failure_context + "\n"
    if analysis.mode == "composable":
        return common + """输出严格 JSON：{"slots":[{"id":"opening","required":true,"variants":["..."]}]}
每个 slot 是可以自然拼接的短句；只创建确实需要的 slot，optional slot 用 required:false。"""
    return common + f"输出严格 JSON：{{\"variants\":[\"...\"]}}，只能包含 {count} 条完整话术。"


def _review(
    text: str,
    segment: Segment,
    analysis: Analysis,
    tolerance: float,
    check_duration: bool = True,
    check_must_keep: bool = True,
) -> Dict[str, Any]:
    duration = estimate_duration(text)
    low = segment.target_duration * (1 - tolerance)
    high = segment.target_duration * (1 + tolerance)
    surface = SequenceMatcher(None, segment.original_text, text).ratio()
    matcher = SequenceMatcher(None, segment.original_text, text)
    longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
    reasons = []
    if check_must_keep and not all(not fact or fact in text for fact in analysis.must_keep):
        reasons.append("must_keep_missing")
    if check_duration and not low <= duration <= high:
        reasons.append("duration_out_of_range")
    if surface >= 0.86 or longest >= 18:
        reasons.append("surface_too_similar")
    return {"accepted": not reasons, "estimated_duration": round(duration, 3), "surface_similarity": round(surface, 3), "reasons": reasons}


class TimelineEngine:
    def __init__(self, llm: Any, variant_count: int = 5, duration_tolerance: float = 0.15, cooldown_window: int = 3, seed: Optional[int] = None) -> None:
        if variant_count < 1 or not 0 < duration_tolerance < 1 or cooldown_window < 0:
            raise ValueError("invalid timeline configuration")
        self.llm = llm
        self.variant_count = variant_count
        self.duration_tolerance = duration_tolerance
        self.cooldown_window = cooldown_window
        self.random = random.Random(seed)
        self.json_retry_total = 0
        self.llm_latency_total_ms = 0.0

    def analyze(self, segment: Segment) -> Analysis:
        if segment.analysis:
            return segment.analysis
        result = _analysis_from_json(self._ask(_analysis_prompt(segment), '{"segment_type":"...","intent":"...","facts":[],"must_keep":[],"tone":"...","mode":"atomic|composable"}'), strict=True)
        if not result.intent:
            raise SegmentFailure("analysis_failed", "analyze", f"{segment.id}: analyzer returned no intent")
        segment.analysis = result
        return result

    def rewrite(self, segment: Segment, analysis: Analysis, attempt: int = 0, failure_context: str = "") -> None:
        if attempt == 0 and segment.mode == "atomic" and segment.variants:
            return
        if attempt == 0 and segment.mode == "composable" and segment.slots:
            return
        result = self._ask(_rewrite_prompt(segment, analysis, self.variant_count, attempt, failure_context), '{"variants":["..."]} or {"slots":[]}')
        if not isinstance(result, dict):
            raise SegmentFailure("schema_error", "rewrite", "rewriter response must be an object")
        if analysis.mode == "composable":
            if not isinstance(result.get("slots"), list):
                raise SegmentFailure("schema_error", "rewrite", "composable response needs slots array")
            if any(
                not isinstance(slot, dict)
                or not isinstance(slot.get("variants"), list)
                or not _clean_text(slot.get("id"))
                or not isinstance(slot.get("required"), bool)
                or not all(isinstance(value, str) for value in slot.get("variants", []))
                for slot in result["slots"]
            ):
                raise SegmentFailure("schema_error", "rewrite", "invalid composable slot schema")
            segment.slots = [Slot(
                id=_clean_text(slot.get("id")),
                required=bool(slot.get("required", True)),
                variants=[_clean_text(x) for x in slot.get("variants", []) if _clean_text(x)],
            ) for slot in result.get("slots", []) if isinstance(slot, dict) and _clean_text(slot.get("id"))]
            if not segment.slots:
                raise SegmentFailure("empty_slots", "rewrite", f"{segment.id}: rewriter returned no slots")
        else:
            if not isinstance(result.get("variants"), list) or not all(isinstance(value, str) for value in result["variants"]):
                raise SegmentFailure("schema_error", "rewrite", "atomic response needs variants array")
            segment.variants = [_clean_text(x) for x in result.get("variants", []) if _clean_text(x)]
            if not segment.variants:
                raise SegmentFailure("empty_variants", "rewrite", f"{segment.id}: rewriter returned no variants")

    def _ask(self, prompt: str, schema_hint: str) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = self.llm.json(prompt, schema_hint=schema_hint)
        except TypeError:
            result = self.llm.json(prompt)
        self.json_retry_total += int(getattr(self.llm, "last_json_retry_count", 0))
        self.llm_latency_total_ms += float(getattr(self.llm, "last_latency_ms", (time.monotonic() - started) * 1000))
        return result

    def review(self, segment: Segment, analysis: Analysis) -> Dict[str, Any]:
        if segment.mode == "composable":
            reviewed = []
            for slot in segment.slots:
                kept = []
                for variant in slot.variants:
                    result = _review(
                        variant, segment, analysis, self.duration_tolerance,
                        check_duration=False, check_must_keep=False,
                    )
                    if result["accepted"]:
                        kept.append(variant)
                    reviewed.append({"slot": slot.id, "text": variant, **result})
                slot.variants = kept
            if not any(slot.variants for slot in segment.slots):
                raise SegmentFailure("rewrite_failed", "review", f"{segment.id}: all slot variants rejected")
            required_slots = [slot for slot in segment.slots if slot.required and slot.variants]
            sample = None
            for values in product(*(slot.variants for slot in required_slots)):
                candidate = _review("".join(values), segment, analysis, self.duration_tolerance)
                if candidate["accepted"]:
                    sample = candidate
                    break
            if sample is None:
                raise SegmentFailure("composable_no_valid_combination", "review", f"{segment.id}: no valid slot combination", {"items": reviewed})
            return {"items": reviewed, "sample": sample}
        kept = []
        reviewed = []
        for variant in segment.variants:
            result = _review(variant, segment, analysis, self.duration_tolerance)
            reviewed.append({"text": variant, **result})
            if result["accepted"]:
                kept.append(variant)
        if not kept:
            reasons = [reason for item in reviewed for reason in item["reasons"]]
            if reasons and all(reason == "must_keep_missing" for reason in reasons):
                reason = "must_keep_missing"
            elif reasons and all(reason == "surface_too_similar" for reason in reasons):
                reason = "surface_too_similar"
            elif reasons and all(reason == "duration_out_of_range" for reason in reasons):
                durations = [item["estimated_duration"] for item in reviewed]
                reason = "duration_too_short" if statistics.mean(durations) < segment.target_duration else "duration_too_long"
            else:
                reason = "rewrite_failed"
            raise SegmentFailure(reason, "review", f"{segment.id}: all variants rejected", {"items": reviewed})
        segment.variants = kept
        return {"items": reviewed}

    def process(self, segments: List[Segment]) -> Dict[str, Any]:
        reports = []
        for segment in segments:
            analysis = self.analyze(segment)
            last_error = None
            for attempt in range(3):
                try:
                    self.rewrite(segment, analysis, attempt)
                    reports.append(self.review(segment, analysis))
                    last_error = None
                    break
                except TimelineError as exc:
                    last_error = exc
                    if attempt < 2:
                        segment.variants = []
                        segment.slots = []
            if last_error:
                raise last_error
        return {"segments": [_segment_to_json(segment) for segment in segments], "review": reports}

    def process_one(self, segment: Segment, max_retries: int = 2) -> Dict[str, Any]:
        """Process one segment with isolated, reason-aware rewrite retries."""
        analysis_attempts = 0
        while True:
            try:
                analysis = self.analyze(segment)
                break
            except SegmentFailure as exc:
                analysis_attempts += 1
                if analysis_attempts > max_retries:
                    raise exc
        last_error: Optional[SegmentFailure] = None
        failure_context = ""
        for attempt in range(max_retries + 1):
            try:
                if attempt:
                    segment.variants = []
                    segment.slots = []
                self.rewrite(segment, analysis, attempt, failure_context)
                review = self.review(segment, analysis)
                return {"segment": _segment_to_json(segment), "review": review, "attempts": attempt + 1, "analysis_attempts": analysis_attempts}
            except SegmentFailure as exc:
                last_error = exc
                failure_context = self._retry_context(exc, segment)
                if attempt < max_retries:
                    continue
        if last_error:
            raise last_error
        raise SegmentFailure("unknown", "rewrite", f"{segment.id}: unknown failure")

    @staticmethod
    def _retry_context(error: SegmentFailure, segment: Segment) -> str:
        if error.reason == "duration_too_short":
            return f"上一轮候选明显过短。目标约 {segment.target_duration:.1f} 秒，请扩展到目标区间，不要只输出标题。"
        if error.reason == "duration_too_long":
            return f"上一轮候选明显过长。目标约 {segment.target_duration:.1f} 秒，请压缩表达，保留全部事实。"
        if error.reason == "must_keep_missing":
            return "上一轮缺少必须保留事实：" + "、".join(segment.analysis.must_keep if segment.analysis else [])
        if error.reason == "surface_too_similar":
            return "上一轮与原文表层过于相似，请重组句式和表达顺序，不能只替换词语。"
        return f"上一轮失败原因：{error.reason}。请严格返回合法结构。"

    def choose(self, segment: Segment, history: Optional[List[str]] = None) -> str:
        history = history or []
        if segment.mode == "composable":
            pieces = []
            for slot in segment.slots:
                candidates = [x for x in slot.variants if x not in history] or slot.variants
                if not candidates:
                    if slot.required:
                        raise TimelineError(f"{segment.id}: required slot {slot.id} has no variants")
                    continue
                pieces.append(self.random.choice(candidates))
            return "".join(pieces)
        candidates = [x for x in segment.variants if x not in history] or segment.variants
        if not candidates:
            raise TimelineError(f"{segment.id}: no variants")
        return self.random.choice(candidates)

    def process_batch(
        self,
        segments: List[Segment],
        max_retries: int = 2,
        completed: Optional[Dict[str, Dict[str, Any]]] = None,
        checkpoint: Optional[Any] = None,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        completed = completed or {}
        processed: Dict[str, Dict[str, Any]] = {}
        reviews: Dict[str, Any] = {}
        failures: List[Dict[str, Any]] = []
        timings: List[float] = []
        stats: Dict[str, Any] = {
            "analysis_retry_count": 0, "rewrite_retry_count": 0, "json_retry_count": 0,
            "schema_retry_count": 0, "must_keep_failure_count": 0,
            "duration_failure_count": 0, "surface_failure_count": 0,
            "ollama_timeout_count": 0, "failure_reasons": {},
        }
        for index, segment in enumerate(segments, 1):
            if segment.id in completed:
                processed[segment.id] = completed[segment.id]
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} RESUME")
                continue
            started = time.monotonic()
            try:
                result = self.process_one(segment, max_retries=max_retries)
                output = result["segment"]
                output["generalize_status"] = "completed"
                output["status"] = "completed"
                processed[segment.id] = output
                reviews[segment.id] = result["review"]
                stats["rewrite_retry_count"] += max(0, result["attempts"] - 1)
                stats["analysis_retry_count"] += result["analysis_attempts"]
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} PASS")
            except SegmentFailure as exc:
                output = _segment_to_json(segment)
                output.update({
                    "generalize_status": "failed", "status": "failed",
                    "fallback": "original", "fallback_text": segment.original_text,
                    "failure_reason": exc.reason,
                })
                processed[segment.id] = output
                failure = {
                    "segment_id": segment.id,
                    "parent_segment_id": segment.metadata.get("parent_segment_id"),
                    "stage": exc.stage,
                    "reason": exc.reason,
                    "attempts": max_retries + 1,
                    "last_error": str(exc),
                    "target_duration": segment.target_duration,
                }
                failures.append(failure)
                stats["failure_reasons"][exc.reason] = stats["failure_reasons"].get(exc.reason, 0) + 1
                if exc.reason == "schema_error":
                    stats["schema_retry_count"] += max_retries
                if exc.reason == "must_keep_missing":
                    stats["must_keep_failure_count"] += 1
                if exc.reason.startswith("duration_") or exc.reason == "composable_no_valid_combination":
                    stats["duration_failure_count"] += 1
                if exc.reason == "surface_too_similar":
                    stats["surface_failure_count"] += 1
                if exc.reason == "ollama_timeout":
                    stats["ollama_timeout_count"] += 1
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} FAIL {exc.reason}")
            finally:
                timings.append(time.monotonic() - started)
                stats["json_retry_count"] = self.json_retry_total
                if checkpoint:
                    checkpoint({
                        "segments": [processed[item.id] for item in segments if item.id in processed],
                        "review": [reviews[item.id] for item in segments if item.id in reviews],
                        "failed_segments": failures,
                        "stats": stats,
                    })
        stats["average_segment_processing_time"] = statistics.mean(timings) if timings else 0.0
        stats["total_processing_time"] = sum(timings)
        return {
            "segments": [processed[item.id] for item in segments],
            "review": [reviews[item.id] for item in segments if item.id in reviews],
            "total": len(segments),
            "success": len(segments) - len(failures),
            "failed": len(failures),
            "failed_segments": failures,
            "stats": stats,
        }


def _segment_to_json(segment: Segment) -> Dict[str, Any]:
    analysis = segment.analysis or Analysis()
    data: Dict[str, Any] = {
        "id": segment.id, "start": segment.start, "end": segment.end,
        "duration_target": segment.target_duration, "original_text": segment.original_text,
        "segment_type": analysis.segment_type, "intent": analysis.intent, "facts": analysis.facts,
        "must_keep": analysis.must_keep, "tone": analysis.tone, "mode": analysis.mode,
    }
    data.update(segment.metadata)
    if analysis.mode == "composable":
        data["slots"] = [{"id": slot.id, "required": slot.required, "variants": slot.variants} for slot in segment.slots]
    else:
        data["variants"] = segment.variants
    return data


def load_segments(path: Path) -> List[Segment]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    values = raw.get("segments") if isinstance(raw, dict) else raw
    if not isinstance(values, list) or not values:
        raise TimelineError("input must contain a non-empty segments array")
    if any(not isinstance(value, dict) for value in values):
        raise TimelineError("every segment must be an object")
    return [_segment_from_json(value, index + 1) for index, value in enumerate(values)]


def save_result(path: Path, result: Dict[str, Any], source: Path, model: str) -> None:
    output = {"schema_version": 1, "source": str(source), "model": model, "segments": result["segments"], "review": result["review"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
