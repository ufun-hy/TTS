"""Conservative cleanup for raw Chinese Whisper text."""

from __future__ import annotations

import re
from typing import Any, Sequence


STRONG_BREAK = "\x1e"
WEAK_BREAK = "\x1f"
PARAGRAPH_BREAK = "\x1d"
_PUNCTUATION = str.maketrans({
    ",": "，",
    ".": "。",
    "?": "？",
    "!": "！",
    ";": "；",
    ":": "：",
})
_STUTTER_WORDS = (
    "这个", "那个", "我们", "你们", "咱们", "大家", "就是", "然后", "所以",
    "但是", "不过", "今天", "主要", "现在", "如果", "一个",
)


def _text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).translate(_PUNCTUATION).strip()


def _remove_fillers(text: str) -> str:
    # These are only speech fillers in this V1; content words such as "其实"
    # are handled by a separate, narrower normalization below.
    text = re.sub(r"(?:嗯|呃|额|啊|那个)(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"^(?:嗯|呃|额|啊|那个)+", "", text)
    text = re.sub(r"呢(?=[\u4e00-\u9fff])", "", text)
    text = text.replace("最大的一个特点", "最大的特点")
    text = text.replace("其实就是", "是")
    return text


def _remove_obvious_repetition(text: str) -> str:
    # Keep this deliberately narrow: only common function/discourse words and
    # single repeated pronouns are treated as stutters. Content-word repeats
    # such as "好吃好吃" remain untouched because they may be emphasis.
    repeated = re.compile(r"(" + "|".join(map(re.escape, _STUTTER_WORDS)) + r")\1+")
    for _ in range(3):
        changed = repeated.sub(r"\1", text)
        if changed == text:
            break
        text = changed
    return re.sub(r"([我你他她它这那])\1+", r"\1", text)


def _insert_discourse_breaks(text: str) -> str:
    text = re.sub(r"然后(?=(?:这个|那|产品|果王|大果))", PARAGRAPH_BREAK, text)
    text = re.sub(r"然后(?=(?:大家|我们|你们|咱们|使用|现在|接下来))", WEAK_BREAK, text)
    text = re.sub(r"(?<!^)(?<![。！？])(?=(?:但是|不过|所以|因此|而且|另外))", WEAK_BREAK, text)
    return text.replace("然后", "，然后")


def _segment_boundary(current: str, following: str, gap: float) -> str:
    if gap >= 2.0:
        return PARAGRAPH_BREAK
    if gap >= 0.75:
        return STRONG_BREAK
    if current.endswith(("吗", "呀", "什么", "啥", "好不好", "是不是", "对不对", "甜不甜")):
        return "？"
    new_thought = re.match(
        r"(?:姐妹们|好姐妹们|来|现在|如果|咱们|咱家|你们|我们|有的|但是|不过|所以|另外|说句实在话|这么跟你说)",
        following,
    )
    if new_thought:
        if current.endswith(("的", "之后", "如果", "的话", "是", "有", "和", "跟", "把", "给", "去", "再", "还", "在", "因为", "所以", "而且", "以及", "到", "从", "让", "能", "会", "就", "可能", "应该")):
            return WEAK_BREAK
        return STRONG_BREAK if len(current) >= 6 else WEAK_BREAK
    if len(current) <= 5:
        return WEAK_BREAK
    return ""


def _join_segments(segments: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    previous: dict[str, Any] | None = None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        value = _text(segment.get("text"))
        if not value:
            continue
        if previous is not None:
            try:
                gap = max(0.0, float(segment.get("start", 0)) - float(previous.get("end", 0)))
            except (TypeError, ValueError):
                gap = 0.0
            parts.append(_segment_boundary(str(previous.get("text") or ""), value, gap))
        parts.append(value)
        previous = segment
    return "".join(parts)


def _punctuate(text: str) -> str:
    text = re.sub(r"[，、；：]+([。！？])", r"\1", text)
    text = re.sub(r"([。！？]){2,}", r"\1", text)
    text = re.sub(r"[，、；：]+", "，", text)
    text = text.replace(PARAGRAPH_BREAK, "。\n\n")
    text = text.replace(STRONG_BREAK, "。")
    text = text.replace(WEAK_BREAK, "，")
    text = re.sub(r"([。！？])，+", r"\1", text)
    text = re.sub(r"，+", "，", text)
    text = text.strip("，、；：")
    if text and text[-1] not in "。！？":
        text += "。"
    return text


def _paragraphs(text: str, max_chars: int = 260) -> str:
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n+", text.strip()):
        sentences = [part.strip() for part in re.findall(r"[^。！？]+[。！？]", block) if part.strip()]
        if not sentences:
            if block.strip():
                paragraphs.append(block.strip())
            continue
        current: list[str] = []
        current_chars = 0
        for sentence in sentences:
            if current and current_chars + len(sentence) > max_chars:
                paragraphs.append("".join(current))
                current = []
                current_chars = 0
            current.append(sentence)
            current_chars += len(sentence)
        if current:
            paragraphs.append("".join(current))
    return "\n\n".join(paragraphs)


def clean_transcript(text: str, segments: Sequence[dict[str, Any]] | None = None) -> str:
    """Return readable Chinese while retaining source wording and order."""
    if not isinstance(text, str):
        raise ValueError("transcript text must be a string")
    source = _join_segments(segments) if segments else _text(text)
    if not source:
        return ""
    source = _remove_fillers(source)
    source = _remove_obvious_repetition(source)
    source = _insert_discourse_breaks(source)
    return _paragraphs(_punctuate(source))
