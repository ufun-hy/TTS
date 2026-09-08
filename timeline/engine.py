"""Minimal, resumable text-layer pipeline for timeline speech generalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
import hashlib
import json
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
import statistics
import time
from typing import Any, Dict, List, Optional
from urllib import error, request

from .duration_policy import DurationPolicy, DurationWindow
from .semantic_coverage import coverage_report


SEGMENT_TYPES = {
    "opening", "welcome", "retention", "product_intro", "feature_explanation",
    "benefit", "pain_point", "usage_scenario", "comparison", "price",
    "promotion", "trust", "faq", "interaction", "cta", "transition",
    "recap", "closing", "other",
}
GENERIC_SEMANTIC_POINTS = {"entertainment", "promotion", "promote", "offer_discount", "product", "product_promotion", "product_intro", "price", "other", "interaction", "cta"}
GENERALIZE_STRATEGY_VERSION = "semantic-coverage-v1"


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

    def json(self, prompt: str, schema_hint: str = "", temperature: Optional[float] = None) -> Dict[str, Any]:
        started = time.monotonic()
        self.last_json_retry_count = 0
        temperatures = ([temperature, max(0.1, temperature - 0.2), 0.1] if temperature is not None else [0.8, 0.3, 0.1])[: self.json_retry_count + 1]
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
    hard_keep: List[str] = field(default_factory=list)
    semantic_keep: List[str] = field(default_factory=list)
    semantic_points: List[str] = field(default_factory=list)
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
    candidates: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    speech_end: Optional[float] = None
    pause_after: float = 0.0
    strategy: Optional[str] = None

    @property
    def target_duration(self) -> float:
        if self.speech_end is not None:
            return max(0.0, self.speech_end - self.start)
        return max(0.0, self.end - self.start)

    @property
    def mode(self) -> str:
        return self.strategy or (self.analysis.mode if self.analysis else "atomic")


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
    if mode == "atomic|composable":
        mode = "atomic"
    if mode not in {"atomic", "composable"}:
        if strict:
            raise SegmentFailure("schema_error", "analyze", f"invalid mode: {mode}")
        mode = "atomic"
    facts = _text_list(raw.get("facts"))
    must_keep = _text_list(raw.get("must_keep"))
    hard_keep = _text_list(raw.get("hard_keep"))
    if not hard_keep:
        hard_keep = [value for value in must_keep if re.search(r"\d|[元块斤克件折送满减售后]", value)]
    semantic_keep = _text_list(raw.get("semantic_keep")) or [value for value in (must_keep or facts) if value not in hard_keep]
    semantic_points = [point for point in (_text_list(raw.get("semantic_points")) or ([result for result in [_clean_text(raw.get("intent"))] if result] + semantic_keep)) if point.lower() not in GENERIC_SEMANTIC_POINTS]
    if not must_keep:
        must_keep = list(hard_keep)
    for fact in facts:
        if fact not in semantic_keep and fact not in hard_keep:
            semantic_keep.append(fact)
    return Analysis(
        segment_type=segment_type,
        intent=_clean_text(raw.get("intent")),
        facts=facts,
        must_keep=must_keep,
        hard_keep=hard_keep,
        semantic_keep=semantic_keep,
        semantic_points=semantic_points,
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
    if analysis_raw is None and any(key in raw for key in ("segment_type", "intent", "facts", "must_keep", "hard_keep", "semantic_keep", "semantic_points", "tone", "mode")):
        analysis_raw = {key: raw.get(key) for key in ("segment_type", "intent", "facts", "must_keep", "hard_keep", "semantic_keep", "semantic_points", "tone", "mode")}
    speech_end = float(raw.get("speech_end", end))
    pause_after = float(raw.get("pause_after", max(0.0, end - speech_end)))
    strategy = _clean_text(raw.get("strategy")) or None
    if strategy not in {None, "simple_candidate", "atomic", "compact_atomic", "composable", "compact_composable"}:
        strategy = None
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
        candidates=[_clean_text(x) for x in raw.get("candidates", []) if _clean_text(x)],
        metadata={key: raw[key] for key in ("parent_segment_id", "source_segment_id", "source_start", "source_end") if key in raw},
        speech_end=speech_end,
        pause_after=pause_after,
        strategy=strategy,
    )


def _analysis_prompt(segment: Segment) -> str:
    return f"""你是直播话术分析器。只分析原话，不改写，不补充原文没有的商品事实。
输出严格 JSON：{{"segment_type":"...","intent":"...","facts":[],"must_keep":[],"hard_keep":[],"semantic_keep":[],"semantic_points":[],"tone":"...","mode":"atomic|composable"}}
hard_keep 只放必须原样保留的价格、数字、数量、规格、时间、优惠条件、售后期限和专有名词；semantic_keep 放只需保持意思的卖点；semantic_points 用简短条目描述完整语义骨架。
segment_type 只能从 opening,welcome,retention,product_intro,feature_explanation,benefit,pain_point,usage_scenario,comparison,price,promotion,trust,faq,interaction,cta,transition,recap,closing,other 中选择。
价格、规格、参数、规则、售后和明确产品事实用 atomic；欢迎、互动、过渡、情绪和促单可用 composable。
原话：{segment.original_text}
    """


def _has_complex_fact(analysis: Analysis) -> bool:
    return len(analysis.must_keep) > 1 or any(re.search(r"\d|[元块斤克件折送满减售后]", fact) for fact in analysis.must_keep)


def _strategy_for(segment: Segment, analysis: Analysis, simple_candidate: bool = True) -> str:
    if simple_candidate:
        return "simple_candidate"
    if segment.target_duration < 4 and analysis.segment_type in {"cta", "interaction", "transition", "welcome", "retention", "promotion", "benefit"} and not _has_complex_fact(analysis):
        return "compact_atomic"
    if segment.target_duration < 8 and analysis.mode == "composable":
        return "compact_composable"
    return analysis.mode


def _rewrite_prompt(segment: Segment, analysis: Analysis, count: int, attempt: int = 0, failure_context: str = "") -> str:
    strategy = segment.mode
    if strategy == "simple_candidate":
        hard_keep_prompt = "\n".join(f"[KEEP-{index:02d}] {value}" for index, value in enumerate(analysis.hard_keep, 1)) or "[KEEP-00] 无硬锚点"
        common = f"""你是直播话术重新创作者。只生成完整、自然、可直接播放的候选话术，不分析、不拆 slot。
语义类型：{analysis.segment_type}
语义意图：{analysis.intent}
语义骨架：{json.dumps(analysis.semantic_points, ensure_ascii=False)}
只需保持意思的卖点：{json.dumps(analysis.semantic_keep, ensure_ascii=False)}
以下 hard_keep 每个候选都必须原样出现：
{hard_keep_prompt}
不得新增原文没有的事实，不要照抄原句。
表达长度与原意大致相当即可，不要故意扩写、删减、重复句子或加入无意义填充。
"""
        if attempt:
            common += "上一轮候选未通过程序审核。" + failure_context + "\n"
        return common + f'只返回 JSON：{{"candidates":["完整候选1","完整候选2","完整候选3","完整候选4"]}}，生成 {count} 条。'
    if strategy == "compact_atomic":
        mode_instruction = "这是短 Segment，输出 3～5 条完整短句，不要拆 slot，不要重复填充。"
    elif strategy == "compact_composable":
        mode_instruction = "这是短 Segment，最多使用 1～2 个 slot，同时输出 3～5 条已经完整可播放的 candidates。"
    else:
        mode_instruction = "优先输出已经完整可播放的 candidates；slots 只服务于未来随机组合。"
    common = f"""你是直播话术重新创作者。根据语义骨架重新组织口语表达，不做逐词同义替换。
必须保持原段的销售作用、商品事实、时间轴位置和口语直播感；不得新增全网最低、销量第一、官方认证、医学效果等事实。
原目标约 {segment.target_duration:.1f} 秒；自然表达优先，落在推荐区间即可，不要为了凑时长重复句子、卖点、CTA 或语气词。
分析结果：{json.dumps(analysis.__dict__, ensure_ascii=False)}
原话只用于理解语义，不要复用长句：{segment.original_text}
策略：{strategy}。{mode_instruction}
"""
    if attempt:
        common += "上一轮候选未通过审核。请根据失败原因重新组织，不要只重复上一轮。\n"
        common += failure_context + "\n"
    if strategy in {"composable", "compact_composable"}:
        return common + """输出严格 JSON：{"slots":[{"id":"opening","required":true,"variants":["..."]}],"candidates":["完整自然话术..."]}
每个 slot 是可以自然拼接的短句；只创建确实需要的 slot，optional slot 用 required:false；candidates 必须是模型已经检查过的完整可播放表达。"""
    return common + f"输出严格 JSON：{{\"variants\":[\"...\"]}}，只能包含 {count} 条完整话术。"


def _rescue_prompt(segment: Segment, analysis: Analysis, failure_context: str = "") -> str:
    window = DurationPolicy().window(segment.target_duration)
    return f"""你是直播话术最终修复器。请输出 3～5 条完整、自然、可直接播放的候选句。
必须保留所有事实：{json.dumps(analysis.must_keep, ensure_ascii=False)}
原意：{analysis.intent}
原话：{segment.original_text}
目标约 {window.target:.1f} 秒，推荐 {window.preferred_low:.1f}～{window.preferred_high:.1f} 秒，硬范围 {window.hard_low:.1f}～{window.hard_high:.1f} 秒。
自然表达优先，不要重复卖点、CTA、语气词或为了凑时长填充；不得新增原文没有的事实。
上一轮失败原因：{failure_context}
    只返回 JSON：{{"candidates":["完整候选1","完整候选2","完整候选3"]}}。"""


def _slot_candidates(raw_slots: Any) -> List[str]:
    if not isinstance(raw_slots, list):
        return []
    slots = []
    for raw in raw_slots:
        if not isinstance(raw, dict) or not isinstance(raw.get("variants"), list):
            continue
        values = [_clean_text(value) for value in raw["variants"] if isinstance(value, str) and _clean_text(value)]
        if values:
            slots.append((bool(raw.get("required", True)), values))
    if not slots:
        return []
    required = [values for is_required, values in slots if is_required]
    optional = [values for is_required, values in slots if not is_required]
    if not required:
        required = [[values[0]] for _, values in slots]
    candidates = ["".join(values) for values in product(*required)]
    for values in optional:
        candidates.extend(base + values[0] for base in list(candidates))
    return list(dict.fromkeys(candidates))[:20]


def _review(
    text: str,
    segment: Segment,
    analysis: Analysis,
    tolerance: float,
    check_duration: bool = True,
    check_must_keep: bool = True,
    duration_policy: Optional[DurationPolicy] = None,
) -> Dict[str, Any]:
    duration = estimate_duration(text)
    window: Optional[DurationWindow] = duration_policy.window(segment.target_duration) if duration_policy else None
    duration_status = window.status(duration) if window else ("preferred" if segment.target_duration * (1 - tolerance) <= duration <= segment.target_duration * (1 + tolerance) else ("too_short" if duration < segment.target_duration else "too_long"))
    surface = SequenceMatcher(None, segment.original_text, text).ratio()
    matcher = SequenceMatcher(None, segment.original_text, text)
    longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
    reasons = []
    required_keep = analysis.hard_keep if segment.mode == "simple_candidate" else analysis.must_keep
    semantic_coverage = coverage_report(analysis.semantic_points, text, analysis.hard_keep) if segment.mode == "simple_candidate" else {"covered": 0, "total": 0, "missing": [], "accepted": True}
    if check_must_keep and not all(not fact or fact in text for fact in required_keep):
        reasons.append("must_keep_missing")
    if check_must_keep and not semantic_coverage["accepted"]:
        reasons.append("semantic_coverage_missing")
    if check_duration and duration_status in {"extreme_too_short", "extreme_too_long"}:
        reasons.append(duration_status)
    if surface >= 0.86 or longest >= 18:
        reasons.append("surface_too_similar")
    return {
        "accepted": not reasons,
        "estimated_duration": round(duration, 3),
        "duration_target": round(segment.target_duration, 3),
        "duration_status": duration_status,
        "duration_warning": duration_status == "acceptable",
        "duration_ratio": round(duration / segment.target_duration, 3) if segment.target_duration else 0.0,
        "surface_similarity": round(surface, 3),
        "semantic_coverage": semantic_coverage,
        "reasons": reasons,
    }


class TimelineEngine:
    def __init__(self, llm: Any, variant_count: int = 4, duration_tolerance: float = 0.15, cooldown_window: int = 3, seed: Optional[int] = None, natural_duration: bool = True, simple_candidate: bool = True) -> None:
        if variant_count < 1 or not 0 < duration_tolerance < 1 or cooldown_window < 0:
            raise ValueError("invalid timeline configuration")
        self.llm = llm
        self.variant_count = variant_count
        self.duration_tolerance = duration_tolerance
        self.cooldown_window = cooldown_window
        self.random = random.Random(seed)
        self.duration_policy = DurationPolicy() if natural_duration else None
        self.simple_candidate = simple_candidate
        self.analysis_calls = 0
        self.rewrite_calls = 0
        self.retry_calls = 0
        self.json_retry_total = 0
        self.llm_latency_total_ms = 0.0

    def analyze(self, segment: Segment) -> Analysis:
        if segment.analysis:
            segment.strategy = _strategy_for(segment, segment.analysis, self.simple_candidate)
            return segment.analysis
        self.analysis_calls += 1
        result = _analysis_from_json(self._ask(_analysis_prompt(segment), '{"segment_type":"...","intent":"...","facts":[],"must_keep":[],"tone":"...","mode":"atomic|composable"}', temperature=0.1), strict=True)
        if not result.intent:
            raise SegmentFailure("analysis_failed", "analyze", f"{segment.id}: analyzer returned no intent")
        segment.analysis = result
        segment.strategy = _strategy_for(segment, result, self.simple_candidate)
        return result

    def rewrite(self, segment: Segment, analysis: Analysis, attempt: int = 0, failure_context: str = "") -> None:
        segment.strategy = segment.strategy or _strategy_for(segment, analysis, self.simple_candidate)
        if attempt == 0 and segment.mode == "atomic" and segment.variants:
            return
        if attempt == 0 and segment.mode in {"composable", "compact_composable"} and (segment.slots or segment.candidates):
            return
        self.rewrite_calls += 1
        if attempt:
            self.retry_calls += 1
        result = self._ask(_rewrite_prompt(segment, analysis, self.variant_count, attempt, failure_context), '{"candidates":["..."]}' if segment.mode == "simple_candidate" else '{"variants":["..."]} or {"slots":[]}', temperature=0.6)
        if not isinstance(result, dict):
            raise SegmentFailure("schema_error", "rewrite", "rewriter response must be an object")
        if segment.mode == "simple_candidate":
            candidates = result.get("candidates", result.get("variants")) if isinstance(result, dict) else None
            if not isinstance(candidates, list) or not all(isinstance(value, str) for value in candidates):
                raise SegmentFailure("schema_error", "rewrite", "simple candidate response needs candidates array")
            segment.candidates = [_clean_text(value) for value in candidates if _clean_text(value)]
            if not segment.candidates:
                raise SegmentFailure("schema_error", "rewrite", "simple candidate response is empty")
        elif segment.mode in {"composable", "compact_composable"}:
            has_candidates = isinstance(result.get("candidates"), list) and all(isinstance(value, str) for value in result.get("candidates", []))
            if "slots" not in result and has_candidates:
                result["slots"] = []
            if not isinstance(result.get("slots"), list):
                raise SegmentFailure("schema_error", "rewrite", "composable response needs slots array")
            if "candidates" in result and not has_candidates:
                raise SegmentFailure("schema_error", "rewrite", "composable candidates must be an array of strings")
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
            segment.candidates = [_clean_text(x) for x in result.get("candidates", []) if _clean_text(x)]
            if not segment.slots:
                raise SegmentFailure("empty_slots", "rewrite", f"{segment.id}: rewriter returned no slots")
        else:
            variants = result.get("variants")
            if not isinstance(variants, list) and isinstance(result.get("candidates"), list):
                variants = [value for value in result["candidates"] if isinstance(value, str)]
            if not isinstance(variants, list) and isinstance(result.get("candidates"), str):
                variants = [result["candidates"]]
            if not isinstance(variants, list) and isinstance(result.get("slots"), list):
                variants = _slot_candidates(result["slots"])
            if not isinstance(variants, list) or not all(isinstance(value, str) for value in variants):
                raise SegmentFailure("schema_error", "rewrite", "atomic response needs variants array")
            segment.variants = [_clean_text(x) for x in variants if _clean_text(x)]
            if not segment.variants:
                raise SegmentFailure("empty_variants", "rewrite", f"{segment.id}: rewriter returned no variants")

    def _ask(self, prompt: str, schema_hint: str, temperature: Optional[float] = None) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = self.llm.json(prompt, schema_hint=schema_hint, temperature=temperature)
        except TypeError:
            result = self.llm.json(prompt)
        self.json_retry_total += int(getattr(self.llm, "last_json_retry_count", 0))
        self.llm_latency_total_ms += float(getattr(self.llm, "last_latency_ms", (time.monotonic() - started) * 1000))
        return result

    def review(self, segment: Segment, analysis: Analysis) -> Dict[str, Any]:
        if segment.mode == "simple_candidate":
            reviewed = []
            kept = []
            for index, candidate in enumerate(segment.candidates, 1):
                result = _review(candidate, segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
                reviewed.append({"id": f"candidate_{index:02d}", "text": candidate, **result})
                if result["accepted"]:
                    kept.append(candidate)
            if kept:
                segment.candidates = kept
                sample = next(item for item in reviewed if item["accepted"])
                return {
                    "items": reviewed,
                    "sample": sample,
                    "strategy": "simple_candidate",
                    "candidate_count": len(segment.candidates),
                    "accepted_candidate_count": len(kept),
                    "precomposed_candidate_pass": True,
                    "slot_combination_pass": False,
                    "rescue_rewrite_pass": False,
                }
            reasons = [reason for item in reviewed for reason in item["reasons"]]
            if "must_keep_missing" in reasons:
                reason = "must_keep_missing"
            elif "semantic_coverage_missing" in reasons:
                reason = "semantic_coverage_missing"
            elif "surface_too_similar" in reasons:
                reason = "surface_too_similar"
            elif "extreme_too_short" in reasons:
                reason = "extreme_too_short"
            elif "extreme_too_long" in reasons:
                reason = "extreme_too_long"
            else:
                reason = "rewrite_failed"
            raise SegmentFailure(reason, "review", f"{segment.id}: all simple candidates rejected", {"items": reviewed})
        if segment.mode in {"composable", "compact_composable"}:
            candidate_items = []
            candidate_kept = []
            for candidate_text in segment.candidates:
                result = _review(candidate_text, segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
                candidate_items.append({"text": candidate_text, **result})
                if result["accepted"]:
                    candidate_kept.append(candidate_text)
            if candidate_kept:
                segment.candidates = candidate_kept
                sample = _review(candidate_kept[0], segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
                return {"items": candidate_items, "sample": sample, "strategy": segment.mode, "precomposed_candidate_pass": True, "slot_combination_pass": False, "rescue_rewrite_pass": False}
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
                required_slots = []
            else:
                required_slots = [slot for slot in segment.slots if slot.required and slot.variants]
            sample = None
            if required_slots and all(slot.variants for slot in required_slots):
                for values in product(*(slot.variants for slot in required_slots)):
                    candidate = _review("".join(values), segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
                    if candidate["accepted"]:
                        sample = candidate
                        break
            if sample is not None:
                return {"items": candidate_items + reviewed, "sample": sample, "strategy": segment.mode, "precomposed_candidate_pass": False, "slot_combination_pass": True, "rescue_rewrite_pass": False}
            rescue = self._ask(_rescue_prompt(segment, analysis, "slot combination failed"), '{"candidates":["..."]}')
            rescue_values = rescue.get("candidates", rescue.get("variants", [])) if isinstance(rescue, dict) else []
            if not isinstance(rescue_values, list) or not all(isinstance(value, str) for value in rescue_values):
                raise SegmentFailure("schema_error", "rescue", "rescue response needs candidates array")
            rescue_reviewed = []
            rescue_kept = []
            for value in rescue_values:
                cleaned = _clean_text(value)
                result = _review(cleaned, segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
                rescue_reviewed.append({"text": cleaned, **result})
                if result["accepted"]:
                    rescue_kept.append(cleaned)
            if rescue_kept:
                segment.candidates = rescue_kept
                sample = next(item for item in rescue_reviewed if item["accepted"])
                return {"items": candidate_items + reviewed + rescue_reviewed, "sample": sample, "strategy": segment.mode, "precomposed_candidate_pass": False, "slot_combination_pass": False, "rescue_rewrite_pass": True}
            raise SegmentFailure("composable_no_valid_combination", "review", f"{segment.id}: no valid candidate or slot combination", {"items": candidate_items + reviewed + rescue_reviewed})
        kept = []
        reviewed = []
        for variant in segment.variants:
            result = _review(variant, segment, analysis, self.duration_tolerance, duration_policy=self.duration_policy)
            reviewed.append({"text": variant, **result})
            if result["accepted"]:
                kept.append(variant)
        if not kept:
            reasons = [reason for item in reviewed for reason in item["reasons"]]
            if reasons and all(reason == "must_keep_missing" for reason in reasons):
                reason = "must_keep_missing"
            elif reasons and all(reason == "surface_too_similar" for reason in reasons):
                reason = "surface_too_similar"
            elif reasons and all(reason in {"extreme_too_short", "extreme_too_long", "duration_out_of_range"} for reason in reasons):
                durations = [item["estimated_duration"] for item in reviewed]
                reason = "extreme_too_short" if statistics.mean(durations) < segment.target_duration else "extreme_too_long"
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
        retry_limit = min(max_retries, 1) if self.simple_candidate else max_retries
        for attempt in range(retry_limit + 1):
            try:
                if attempt:
                    segment.variants = []
                    segment.slots = []
                    segment.candidates = []
                self.rewrite(segment, analysis, attempt, failure_context)
                review = self.review(segment, analysis)
                return {"segment": _segment_to_json(segment), "review": review, "attempts": attempt + 1, "analysis_attempts": analysis_attempts}
            except SegmentFailure as exc:
                last_error = exc
                failure_context = self._retry_context(exc, segment)
                if attempt < retry_limit:
                    continue
        if last_error:
            raise last_error
        raise SegmentFailure("unknown", "rewrite", f"{segment.id}: unknown failure")

    @staticmethod
    def _retry_context(error: SegmentFailure, segment: Segment) -> str:
        if error.reason == "extreme_too_short":
            return "上一版明显删减了内容。请保持语义完整，不要省略必要表达。"
        if error.reason == "extreme_too_long":
            return "上一版明显扩写过多。请保持原意，删除额外解释、重复和填充。"
        if error.reason == "must_keep_missing":
            analysis = segment.analysis or Analysis()
            return "上一轮遗漏了以下 hard_keep；每个候选都必须原样包含：\n" + "\n".join(f"- {value}" for value in analysis.hard_keep)
        if error.reason == "semantic_coverage_missing":
            analysis = segment.analysis or Analysis()
            return "上一轮遗漏了以下 semantic_points；请自然补回，不要新增事实：\n" + "\n".join(f"- {value}" for value in analysis.semantic_points)
        if error.reason == "surface_too_similar":
            return "上一轮与原文表层过于相似，请重组句式和表达顺序，不能只替换词语。"
        return f"上一轮失败原因：{error.reason}。请严格返回合法结构。"

    def choose(self, segment: Segment, history: Optional[List[str]] = None) -> str:
        history = history or []
        if segment.mode in {"composable", "compact_composable"}:
            candidates = [x for x in segment.candidates if x not in history] or segment.candidates
            if candidates:
                return self.random.choice(candidates)
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
        generated_durations: List[float] = []
        stats: Dict[str, Any] = {
            "generalize_strategy_version": GENERALIZE_STRATEGY_VERSION,
            "duration_policy_version": self.duration_policy.version if self.duration_policy else "legacy",
            "analysis_retry_count": 0, "rewrite_retry_count": 0, "json_retry_count": 0,
            "schema_retry_count": 0, "must_keep_failure_count": 0,
            "duration_failure_count": 0, "surface_failure_count": 0,
            "preferred_duration_pass": 0, "acceptable_duration_pass": 0,
            "duration_too_short": 0, "duration_too_long": 0,
            "compact_atomic_count": 0, "compact_composable_count": 0,
            "normal_composable_count": 0, "precomposed_candidate_pass": 0,
            "slot_combination_pass": 0, "rescue_rewrite_pass": 0,
            "simple_candidate_pass": 0, "candidate_count_total": 0,
            "accepted_candidate_count_total": 0,
            "analysis_calls": 0, "rewrite_calls": 0, "retry_calls": 0,
            "extreme_too_short": 0, "extreme_too_long": 0,
            "semantic_coverage_missing": 0,
            "ollama_timeout_count": 0, "failure_reasons": {},
        }
        for index, segment in enumerate(segments, 1):
            completed_item = completed.get(segment.id)
            if completed_item and completed_item.get("generalize_strategy_version") == GENERALIZE_STRATEGY_VERSION and completed_item.get("segment_fingerprint") == _segment_fingerprint(segment):
                processed[segment.id] = completed_item
                generated_durations.append(float(completed_item.get("generated_estimated_duration", segment.target_duration)))
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} RESUME")
                continue
            started = time.monotonic()
            try:
                result = self.process_one(segment, max_retries=max_retries)
                output = result["segment"]
                output["generalize_status"] = "completed"
                output["status"] = "completed"
                output["generated_estimated_duration"] = result["review"].get("sample", {}).get("estimated_duration", segment.target_duration)
                processed[segment.id] = output
                reviews[segment.id] = result["review"]
                generated_durations.append(float(output["generated_estimated_duration"]))
                stats["rewrite_retry_count"] += max(0, result["attempts"] - 1)
                stats["analysis_retry_count"] += result["analysis_attempts"]
                review = result["review"]
                duration_status = review.get("sample", {}).get("duration_status")
                if duration_status in {"preferred", "acceptable"}:
                    stats[f"{duration_status}_duration_pass"] += 1
                strategy = review.get("strategy", segment.mode)
                if strategy == "simple_candidate":
                    stats["simple_candidate_pass"] += 1
                    stats["candidate_count_total"] += review.get("candidate_count", 0)
                    stats["accepted_candidate_count_total"] += review.get("accepted_candidate_count", 0)
                elif strategy == "compact_atomic":
                    stats["compact_atomic_count"] += 1
                elif strategy == "compact_composable":
                    stats["compact_composable_count"] += 1
                elif strategy == "composable":
                    stats["normal_composable_count"] += 1
                for key in ("precomposed_candidate_pass", "slot_combination_pass", "rescue_rewrite_pass"):
                    if review.get(key):
                        stats[key] += 1
                if progress:
                    progress(f"[{index}/{len(segments)}] {segment.id} PASS {strategy}")
            except SegmentFailure as exc:
                output = _segment_to_json(segment)
                output.update({
                    "generalize_status": "failed", "status": "failed",
                    "fallback": "original", "fallback_text": segment.original_text,
                    "failure_reason": exc.reason,
                })
                processed[segment.id] = output
                generated_durations.append(segment.target_duration)
                failure = {
                    "segment_id": segment.id,
                    "parent_segment_id": segment.metadata.get("parent_segment_id"),
                    "stage": exc.stage,
                    "reason": exc.reason,
                    "attempts": (min(max_retries, 1) if self.simple_candidate else max_retries) + 1,
                    "last_error": str(exc),
                    "target_duration": segment.target_duration,
                }
                failures.append(failure)
                stats["failure_reasons"][exc.reason] = stats["failure_reasons"].get(exc.reason, 0) + 1
                if exc.reason == "schema_error":
                    stats["schema_retry_count"] += max_retries
                if exc.reason == "must_keep_missing":
                    stats["must_keep_failure_count"] += 1
                if exc.reason == "semantic_coverage_missing":
                    stats["semantic_coverage_missing"] += 1
                if exc.reason.startswith("extreme_"):
                    stats["duration_failure_count"] += 1
                    stats[exc.reason] += 1
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
        stats["analysis_calls"] = self.analysis_calls
        stats["rewrite_calls"] = self.rewrite_calls
        stats["retry_calls"] = self.retry_calls
        original_total = sum(segment.target_duration for segment in segments)
        generated_total = sum(generated_durations)
        stats["original_total_speech_duration"] = round(original_total, 3)
        stats["generated_total_estimated_duration"] = round(generated_total, 3)
        stats["global_duration_drift"] = round((generated_total - original_total) / original_total, 4) if original_total else 0.0
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
    output_mode = "composable" if segment.mode in {"composable", "compact_composable"} else "atomic"
    data: Dict[str, Any] = {
        "id": segment.id, "start": segment.start, "end": segment.end,
        "duration_target": segment.target_duration, "original_text": segment.original_text,
        "segment_type": analysis.segment_type, "intent": analysis.intent, "facts": analysis.facts,
        "must_keep": analysis.must_keep, "hard_keep": analysis.hard_keep,
        "semantic_keep": analysis.semantic_keep, "semantic_points": analysis.semantic_points,
        "tone": analysis.tone, "mode": output_mode,
        "strategy": segment.mode,
        "generalize_strategy_version": GENERALIZE_STRATEGY_VERSION,
        "segment_fingerprint": _segment_fingerprint(segment),
    }
    data["speech_end"] = segment.speech_end if segment.speech_end is not None else segment.end
    data["speech_duration"] = segment.target_duration
    data["pause_after"] = segment.pause_after
    data["timeline_duration"] = segment.end - segment.start
    data.update(segment.metadata)
    if segment.mode == "simple_candidate":
        data["candidates"] = segment.candidates
    elif segment.mode in {"composable", "compact_composable"}:
        data["slots"] = [{"id": slot.id, "required": slot.required, "variants": slot.variants} for slot in segment.slots]
        data["candidates"] = segment.candidates
    else:
        data["variants"] = segment.variants
    data["strategy_duration"] = segment.target_duration
    return data


def _segment_fingerprint(segment: Segment) -> str:
    value = json.dumps({"id": segment.id, "start": segment.start, "end": segment.end, "speech_duration": segment.target_duration, "text": segment.original_text}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


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
