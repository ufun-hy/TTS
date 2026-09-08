"""Advisory semantic-point coverage checks.

Coverage is intentionally diagnostic only. It may highlight likely omissions for
review/reporting, but it must not decide whether a generated candidate is safe to
play. Hard facts, similarity and the normal Generalize review remain responsible
for acceptance.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List


STOP_TERMS = {"强调", "说明", "介绍", "原文", "产品", "用户", "可以", "比较", "现在", "这个", "那个", "起来", "方面"}
ALIASES = {
    "软塌": ("软塌", "不会软", "不容易软", "不软塌", "不会软掉"),
    "成熟度": ("成熟度", "熟度", "刚好", "正好", "成熟"),
    "入口": ("入口", "好入口", "容易吃", "适合老人", "适合小孩"),
    "children": ("小孩", "孩子", "儿童", "老人", "children"),
    "汁水": ("汁水", "水分", "多汁", "汁足"),
}
NEGATIVE_MARKERS = ("不", "没", "无", "不会", "不容易")


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("，。！？!?；;、,:：")


def _terms(point: str) -> List[str]:
    value = _clean(point)
    terms = [value[index:index + 2] for index in range(len(value) - 1)]
    return [term for term in dict.fromkeys(terms) if term not in STOP_TERMS and not all(char in "的了是有很更一二三四五六七八九十" for char in term)]


def _has_negative(value: str) -> bool:
    return any(marker in value for marker in NEGATIVE_MARKERS)


def point_covered(point: str, text: str) -> bool:
    """Return a conservative lexical hint, not a semantic truth value."""
    point = _clean(point)
    text = _clean(text)
    if not point:
        return True
    if point in text:
        return True

    # Avoid treating an opposite-polarity phrase as equivalent merely because
    # both contain the same product term (for example 容易软塌 vs 不容易软塌).
    if _has_negative(point) != _has_negative(text):
        shared = [term for term in _terms(point) if term in text]
        if shared:
            return False

    for key, aliases in ALIASES.items():
        if key in point and any(alias in text for alias in aliases):
            return True
    terms = _terms(point)
    if not terms:
        return False
    hits = sum(term in text for term in terms)
    return hits >= max(1, math.ceil(len(terms) * 0.34))


def coverage_report(points: Iterable[str], text: str, hard_keep: Iterable[str] = ()) -> Dict[str, object]:
    """Report likely missing semantic points without blocking the candidate.

    ``hard_keep`` remains in the signature for API compatibility, but hard facts
    never stand in for unrelated semantic points. ``accepted`` is always True so
    this heuristic cannot create a Generalize failure; callers should use
    ``complete``/``missing`` as diagnostics only.
    """
    del hard_keep
    values = [_clean(point) for point in points if _clean(point)]
    missing = [point for point in values if not point_covered(point, text)]
    complete = not missing
    return {
        "covered": len(values) - len(missing),
        "total": len(values),
        "missing": missing,
        "complete": complete,
        "advisory": not complete,
        "accepted": True,
    }
