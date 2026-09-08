"""Small rule-first vocabulary for live-stream semantic boundaries."""

from __future__ import annotations

import re
from typing import Dict, List, Optional


KEYWORDS: Dict[str, tuple[str, ...]] = {
    "price": ("价格", "到手", "直播价", "多少钱", "多少米", "几块", "块", "元", "块钱"),
    "promotion": ("活动", "赠品", "优惠券", "满减", "限时", "福利", "立减", "优惠"),
    "cta": ("直接拍", "赶紧拍", "点链接", "去下单", "需要的", "想要的", "拍下"),
    "interaction": ("评论区", "扣1", "公屏", "有没有", "告诉我", "姐妹们", "朋友们"),
    "transition": ("接下来", "再来看", "然后我们说", "下面", "再讲一个", "换一个"),
    "usage_scenario": ("适合", "用来", "使用", "做罐头", "送人", "老人", "小孩"),
    "feature_explanation": ("功能", "设计", "材质", "容量", "调节", "操作", "方便", "区别"),
    "trust": ("产地", "当天采摘", "冷链", "售后", "客服", "保证", "放心"),
}


def classify(text: str) -> str:
    scores = {kind: sum(text.count(word) for word in words) for kind, words in KEYWORDS.items()}
    best = max(scores, key=scores.get) if scores else "other"
    return best if scores.get(best, 0) else "other"


def boundary_type(left: str, right: str) -> Optional[str]:
    left_type = classify(left)
    right_type = classify(right)
    if left_type == right_type or left_type == "other" or right_type == "other":
        return None
    pairs = {
        ("feature_explanation", "price"): "feature_to_price",
        ("usage_scenario", "price"): "usage_to_price",
        ("benefit", "price"): "benefit_to_price",
        ("price", "promotion"): "price_to_promotion",
        ("promotion", "cta"): "promotion_to_cta",
        ("cta", "interaction"): "cta_to_interaction",
        ("interaction", "feature_explanation"): "interaction_to_feature",
        ("trust", "price"): "trust_to_price",
        ("transition", "feature_explanation"): "transition_to_feature",
    }
    return pairs.get((left_type, right_type), f"{left_type}_to_{right_type}")


def is_entity_boundary(left: str, right: str) -> bool:
    combined = left + right
    if re.search(r"\d\s*[元块斤克两件个折%]", combined) and not re.search(r"[，。！？；]", left[-2:] + right[:2]):
        return False
    return True
