"""Small, deterministic semantic-point coverage checks."""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List


STOP_TERMS = {"强调", "说明", "介绍", "原文", "产品", "用户", "可以", "比较", "现在", "这个", "那个", "起来", "方面"}
ALIASES = {
    "软塌": ("软塌", "软掉", "不会软", "不容易软", "硬度"),
    "成熟度": ("成熟度", "熟度", "刚好", "正好", "成熟"),
    "入口": ("入口", "好入口", "容易吃", "好吃", "适合老人", "适合小孩"),
    "children": ("小孩", "孩子", "儿童", "老人", "children"),
    "汁水": ("汁水", "水分", "多汁", "汁足"),
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("，。！？!?；;、,:：")


def _terms(point: str) -> List[str]:
    value = _clean(point)
    terms = [value[index:index + 2] for index in range(len(value) - 1)]
    return [term for term in dict.fromkeys(terms) if term not in STOP_TERMS and not all(char in "的了是有很更一二三四五六七八九十" for char in term)]


def point_covered(point: str, text: str) -> bool:
    point = _clean(point)
    text = _clean(text)
    if not point:
        return True
    if point in text:
        return True
    for key, aliases in ALIASES.items():
        if key in point and any(alias in text for alias in aliases):
            return True
    terms = _terms(point)
    if not terms:
        return False
    hits = sum(term in text for term in terms)
    return hits >= max(1, math.ceil(len(terms) * 0.34))


def coverage_report(points: Iterable[str], text: str, hard_keep: Iterable[str] = ()) -> Dict[str, object]:
    values = [_clean(point) for point in points if _clean(point)]
    hard_values = [_clean(value) for value in hard_keep if _clean(value)]
    missing = []
    for point in values:
        if point_covered(point, text):
            continue
        if any(value in text for value in hard_values) and re.search(r"价格|数量|规格|优惠|时间|售后|包装", point):
            continue
        missing.append(point)
    return {"covered": len(values) - len(missing), "total": len(values), "missing": missing, "accepted": not missing}
