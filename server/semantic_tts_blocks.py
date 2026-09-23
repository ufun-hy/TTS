"""Continuous speech text and punctuation-first TTS blocks with source spans."""
from __future__ import annotations

import re

from server.prohibited_speech import analyze
from server.speech_units import PAIRS

TARGET_CHARS = 180
MAX_CHARS = 200
TOKEN = re.compile(r"\{\{(?:current_time|current_date|current_weekday)\}\}")
CLOSERS = set(PAIRS.values()) | set('”’」』）)]')


def continuous_text(segments: list[dict]) -> tuple[str, list[dict]]:
    text, spans = "", []
    for segment in segments:
        value = segment["text"].strip()
        if not value:
            continue
        # Preserve lexical boundaries when independently edited English/number units meet.
        separator = " " if text and re.search(r"[A-Za-z0-9]$", text) and re.match(r"[A-Za-z0-9]", value) else ""
        text += separator
        start = len(text)
        text += value
        spans.append({"paragraph_id": segment["id"], "group_id": segment.get("group_id"),
                      "variant_id": segment.get("variant_id"), "start": start, "end": len(text)})
    return text, spans


def safe_continuous_text(segments: list[dict]) -> tuple[str, list[dict], list[dict]]:
    text, spans = continuous_text(segments)
    blocked = analyze(text)["blocked"]
    if not blocked:
        return text, spans, []
    kept, cursor = [], 0
    for hit in blocked:
        kept.append((cursor, hit["start"]))
        cursor = hit["end"]
    kept.append((cursor, len(text)))
    output, mapped = "", []
    for start, end in kept:
        offset = len(output)
        output += text[start:end]
        for span in spans:
            left, right = max(start, span["start"]), min(end, span["end"])
            if left < right:
                mapped.append({**span, "start": offset + left - start, "end": offset + right - start})
    if not any(char.isalnum() for char in output):
        return "", [], blocked
    return output, mapped, blocked


def _boundaries(text: str) -> tuple[list[int], list[int]]:
    strong, weak, stack = [], [], []
    protected = {i for m in TOKEN.finditer(text) for i in range(m.start(), m.end())}
    for i, char in enumerate(text):
        if i in protected:
            continue
        if stack and char == stack[-1]:
            stack.pop()
        elif char in PAIRS:
            stack.append(PAIRS[char])
        if char not in '。！？!?；;，、, \n\t':
            continue
        end = i + 1
        closers = list(stack)
        while end < len(text) and text[end] in CLOSERS:
            if closers and text[end] == closers[-1]:
                closers.pop()
            end += 1
        if closers:
            continue
        if char in ',，' and i and i + 1 < len(text) and text[i - 1].isdigit() and text[i + 1].isdigit():
            continue
        (strong if char in '。！？!?；;' else weak).append(end)
    return strong, weak


def semantic_blocks(segments: list[dict], *, filter_prohibited: bool = True,
                    target: int = TARGET_CHARS, limit: int = MAX_CHARS) -> list[dict]:
    if not 1 <= target <= limit <= MAX_CHARS:
        raise ValueError("invalid TTS block limits")
    if filter_prohibited:
        text, spans, _ = safe_continuous_text(segments)
    else:
        text, spans = continuous_text(segments)
    strong, weak = _boundaries(text)
    tokens = list(TOKEN.finditer(text))
    blocks, start = [], 0
    while start < len(text):
        remaining = len(text) - start
        reason = "end"
        end = len(text)
        if remaining > target:
            for boundaries, kind in ((strong, "strong"), (weak, "weak")):
                candidates = [n for n in boundaries if start < n <= start + limit]
                if candidates:
                    # Prefer useful context, but accept a short natural ending over cutting a word.
                    useful = [n for n in candidates if n - start >= min(120, target)]
                    end = min(useful or candidates, key=lambda n: (abs(n - start - target), n))
                    reason = kind
                    break
            else:
                end = min(start + limit, len(text))
                reason = "hard_cut" if end < len(text) else "end"
                for token in tokens:
                    if token.start() < end < token.end():
                        end = token.start() if token.start() > start else token.end()
                if end <= start or end - start > limit:
                    raise ValueError("cannot fit protected token into TTS block")
        value = text[start:end]
        if value.strip():
            sources = [{**s, "start": max(s["start"], start) - start,
                        "end": min(s["end"], end) - start}
                       for s in spans if s["start"] < end and s["end"] > start]
            blocks.append({"id": f"b{len(blocks) + 1:05d}", "text": value,
                           "sources": sources, "boundary": reason})
        start = end
    return blocks
