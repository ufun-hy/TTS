"""Minimal, resumable text-layer pipeline for timeline speech generalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
import json
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
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
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.S)
        if not match:
            raise TimelineError("Ollama returned no JSON object")
        decoded = json.loads(match.group(0))
    if not isinstance(decoded, dict):
        raise TimelineError("Ollama response must be a JSON object")
    return decoded


class Ollama:
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: int = 180) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def json(self, prompt: str) -> Dict[str, Any]:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.8},
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
        except (OSError, error.URLError, ValueError) as exc:
            raise TimelineError(f"Ollama request failed: {exc}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            raise TimelineError("Ollama returned an invalid response")
        return _json_from_response(body["response"])


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

    @property
    def target_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def mode(self) -> str:
        return self.analysis.mode if self.analysis else "atomic"


def _analysis_from_json(raw: Dict[str, Any]) -> Analysis:
    segment_type = _clean_text(raw.get("segment_type")) or "other"
    if segment_type not in SEGMENT_TYPES:
        segment_type = "other"
    mode = _clean_text(raw.get("mode")) or "atomic"
    if mode not in {"atomic", "composable"}:
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
    return Segment(
        id=_clean_text(raw.get("id")) or f"seg_{index:04d}",
        start=start,
        end=end,
        original_text=text,
        analysis=_analysis_from_json(raw["analysis"]) if isinstance(raw.get("analysis"), dict) else None,
        variants=[_clean_text(x) for x in raw.get("variants", []) if _clean_text(x)],
        slots=[Slot(
            id=_clean_text(slot.get("id")),
            required=bool(slot.get("required", True)),
            variants=[_clean_text(x) for x in slot.get("variants", []) if _clean_text(x)],
        ) for slot in raw.get("slots", []) if isinstance(slot, dict) and _clean_text(slot.get("id"))],
    )


def _analysis_prompt(segment: Segment) -> str:
    return f"""你是直播话术分析器。只分析原话，不改写，不补充原文没有的商品事实。
输出严格 JSON：{{"segment_type":"...","intent":"...","facts":[],"must_keep":[],"tone":"...","mode":"atomic|composable"}}
facts 和 must_keep 必须列出原话中的所有价格、数字、规格、功能、优惠条件和售后规则。
segment_type 只能从 opening,welcome,retention,product_intro,feature_explanation,benefit,pain_point,usage_scenario,comparison,price,promotion,trust,faq,interaction,cta,transition,recap,closing,other 中选择。
价格、规格、参数、规则、售后和明确产品事实用 atomic；欢迎、互动、过渡、情绪和促单可用 composable。
原话：{segment.original_text}
"""


def _rewrite_prompt(segment: Segment, analysis: Analysis, count: int, attempt: int = 0) -> str:
    common = f"""你是直播话术重新创作者。根据语义骨架重新组织口语表达，不做逐词同义替换。
必须保持原段的销售作用、商品事实、时间轴位置和口语直播感；不得新增全网最低、销量第一、官方认证、医学效果等事实。
目标时长约 {segment.target_duration:.1f} 秒，允许误差 ±15%。生成 {count} 个明显不同的表达。
分析结果：{json.dumps(analysis.__dict__, ensure_ascii=False)}
原话只用于理解语义，不要复用长句：{segment.original_text}
"""
    if attempt:
        common += "上一轮候选未通过时长审核。本轮必须明显增加完整信息和口播长度，不能只输出一句短标题。\n"
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

    def analyze(self, segment: Segment) -> Analysis:
        if segment.analysis:
            return segment.analysis
        result = _analysis_from_json(self.llm.json(_analysis_prompt(segment)))
        if not result.intent:
            raise TimelineError(f"{segment.id}: analyzer returned no intent")
        segment.analysis = result
        return result

    def rewrite(self, segment: Segment, analysis: Analysis, attempt: int = 0) -> None:
        if attempt == 0 and segment.mode == "atomic" and segment.variants:
            return
        if attempt == 0 and segment.mode == "composable" and segment.slots:
            return
        result = self.llm.json(_rewrite_prompt(segment, analysis, self.variant_count, attempt))
        if analysis.mode == "composable":
            segment.slots = [Slot(
                id=_clean_text(slot.get("id")),
                required=bool(slot.get("required", True)),
                variants=[_clean_text(x) for x in slot.get("variants", []) if _clean_text(x)],
            ) for slot in result.get("slots", []) if isinstance(slot, dict) and _clean_text(slot.get("id"))]
            if not segment.slots:
                raise TimelineError(f"{segment.id}: rewriter returned no slots")
        else:
            segment.variants = [_clean_text(x) for x in result.get("variants", []) if _clean_text(x)]
            if not segment.variants:
                raise TimelineError(f"{segment.id}: rewriter returned no variants")

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
                raise TimelineError(f"{segment.id}: all slot variants rejected")
            required_slots = [slot for slot in segment.slots if slot.required and slot.variants]
            sample = None
            for values in product(*(slot.variants for slot in required_slots)):
                candidate = _review("".join(values), segment, analysis, self.duration_tolerance)
                if candidate["accepted"]:
                    sample = candidate
                    break
            if sample is None:
                raise TimelineError(f"{segment.id}: composed variants outside target duration")
            return {"items": reviewed, "sample": sample}
        kept = []
        reviewed = []
        for variant in segment.variants:
            result = _review(variant, segment, analysis, self.duration_tolerance)
            reviewed.append({"text": variant, **result})
            if result["accepted"]:
                kept.append(variant)
        if not kept:
            raise TimelineError(f"{segment.id}: all variants rejected")
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


def _segment_to_json(segment: Segment) -> Dict[str, Any]:
    analysis = segment.analysis or Analysis()
    data: Dict[str, Any] = {
        "id": segment.id, "start": segment.start, "end": segment.end,
        "duration_target": segment.target_duration, "original_text": segment.original_text,
        "segment_type": analysis.segment_type, "intent": analysis.intent, "facts": analysis.facts,
        "must_keep": analysis.must_keep, "tone": analysis.tone, "mode": analysis.mode,
    }
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
