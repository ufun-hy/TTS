from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

FILLER_RE = re.compile(r"(姐妹们|姐姐们|哥姐们|家人们|宝贝们|对不对|是不是|真的|这个|那个|咱们|我们|你们|啊|呀|呢|吧)")
PUNCT_RE = re.compile(r"[\s，。！？!?；;、：:“”‘’（）()【】\[\]…·—-]+")


def normalize_unit(text: str) -> str:
    value = str(text or "").strip().lower()
    value = value.replace("九块九", "9.9").replace("9块9", "9.9").replace("九块九毛九", "9.99")
    value = FILLER_RE.sub("", value)
    return PUNCT_RE.sub("", value)


def unit_similarity(left: str, right: str) -> float:
    a, b = normalize_unit(left), normalize_unit(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    sa, sb = set(a), set(b)
    jaccard = len(sa & sb) / max(1, len(sa | sb))
    return 0.72 * ratio + 0.28 * jaccard


def split_units(text: str) -> list[dict[str, Any]]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = [part.strip() for part in re.split(r"(?<=[。！？!?])|\n+", normalized) if part.strip()]
    units = []
    for index, item in enumerate(raw):
        norm = normalize_unit(item)
        if norm:
            units.append({"index": index, "text": item, "norm": norm})
    return units


def _window_similarity(units: list[dict[str, Any]], start_a: int, start_b: int, width: int) -> float:
    scores = []
    for offset in range(width):
        a = start_a + offset
        b = start_b + offset
        if a >= len(units) or b >= len(units):
            break
        scores.append(unit_similarity(units[a]["norm"], units[b]["norm"]))
    return sum(scores) / len(scores) if scores else 0.0


def detect_rounds(text: str, window: int = 6) -> dict[str, Any]:
    units = split_units(text)
    if len(units) < window * 3:
        return {"units": units, "rounds": [], "confidence": 0.0, "message": "文本太短，未检测到重复朗读轮次"}

    probe = min(max(window, 6), 12)
    candidates: list[tuple[int, float]] = []
    min_gap = max(probe * 3, len(units) // 8)
    for start in range(min_gap, len(units) - probe + 1):
        score = _window_similarity(units, 0, start, probe)
        if score >= 0.60:
            if not candidates or start - candidates[-1][0] > probe:
                candidates.append((start, score))
            elif score > candidates[-1][1]:
                candidates[-1] = (start, score)

    if not candidates:
        return {"units": units, "rounds": [], "confidence": 0.0, "message": "未发现明显的整稿重复起点"}

    starts = [0] + [item[0] for item in candidates]
    starts = sorted(set(starts))
    rounds = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(units)
        if end - start < probe * 2:
            continue
        rounds.append({"index": len(rounds) + 1, "start": start, "end": end, "length": end - start})

    confidence = sum(score for _, score in candidates[: max(1, len(rounds) - 1)]) / max(1, min(len(candidates), max(1, len(rounds) - 1)))
    return {"units": units, "rounds": rounds, "confidence": round(confidence, 3), "message": "ok" if len(rounds) >= 2 else "未形成两个完整轮次"}


def align_round(skeleton: list[dict[str, Any]], other: list[dict[str, Any]]) -> dict[str, Any]:
    cursor = 0
    matches = []
    extras = []
    for i, base in enumerate(skeleton):
        best_j, best_score = -1, 0.0
        for j in range(cursor, min(len(other), cursor + 10)):
            score = unit_similarity(base["norm"], other[j]["norm"])
            if score > best_score:
                best_j, best_score = j, score
        if best_j >= 0 and best_score >= 0.44:
            for j in range(cursor, best_j):
                extras.append({"before": i, "text": other[j]["text"], "score": 0.0})
            matches.append({"base": i, "other": best_j, "score": round(best_score, 3)})
            cursor = best_j + 1
    for j in range(cursor, len(other)):
        extras.append({"before": len(skeleton), "text": other[j]["text"], "score": 0.0})
    return {
        "coverage": round(len(matches) / max(1, len(skeleton)), 3),
        "mean_similarity": round(sum(item["score"] for item in matches) / max(1, len(matches)), 3),
        "matches": matches,
        "extras": extras,
    }


def restore_script(text: str) -> dict[str, Any]:
    detected = detect_rounds(text)
    units = detected["units"]
    rounds = detected["rounds"]
    if len(rounds) < 2:
        return {
            "detected": False,
            "confidence": detected["confidence"],
            "rounds": rounds,
            "standard_text": text.strip(),
            "suspected_insertions": [],
            "message": detected["message"],
        }

    round_units = [units[item["start"]:item["end"]] for item in rounds]
    skeleton_index = max(range(len(round_units)), key=lambda i: len(round_units[i]))
    skeleton = round_units[skeleton_index]
    alignments = []
    insertion_votes: dict[tuple[int, str], int] = {}
    for idx, current in enumerate(round_units):
        if idx == skeleton_index:
            continue
        aligned = align_round(skeleton, current)
        alignments.append({"round": idx + 1, **aligned})
        for extra in aligned["extras"]:
            key = (extra["before"], normalize_unit(extra["text"]))
            if key[1]:
                insertion_votes[key] = insertion_votes.get(key, 0) + 1

    # Only merge an insertion when it appears in at least two non-skeleton rounds.
    merged = list(skeleton)
    additions: dict[int, list[str]] = {}
    for (before, norm), votes in insertion_votes.items():
        if votes < 2:
            continue
        text_value = ""
        for idx, current in enumerate(round_units):
            if idx == skeleton_index:
                continue
            for extra in align_round(skeleton, current)["extras"]:
                if extra["before"] == before and normalize_unit(extra["text"]) == norm:
                    text_value = extra["text"]
                    break
            if text_value:
                break
        if text_value:
            additions.setdefault(before, []).append(text_value)

    output_units: list[str] = []
    for i, item in enumerate(merged):
        output_units.extend(additions.get(i, []))
        output_units.append(item["text"])
    output_units.extend(additions.get(len(merged), []))

    suspected = []
    for idx, current in enumerate(round_units):
        if idx == skeleton_index:
            continue
        aligned = align_round(skeleton, current)
        for extra in aligned["extras"]:
            key = (extra["before"], normalize_unit(extra["text"]))
            if insertion_votes.get(key, 0) < 2:
                suspected.append({"round": idx + 1, "before": extra["before"], "text": extra["text"]})

    standard_text = "\n".join(output_units).strip()
    coverage = [item["coverage"] for item in alignments]
    confidence = sum(coverage) / len(coverage) if coverage else detected["confidence"]
    return {
        "detected": True,
        "confidence": round(confidence, 3),
        "rounds": rounds,
        "skeleton_round": skeleton_index + 1,
        "standard_text": standard_text,
        "original_units": len(units),
        "restored_units": len(output_units),
        "suspected_insertions": suspected,
        "alignments": alignments,
        "message": "检测到重复朗读轮次，已生成标准原稿候选",
    }
