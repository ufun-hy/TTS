"""Lossless, conservative speech units for text without ASR timestamps."""
import re

TOPICS = [
    re.compile(pattern) for pattern in (
        r'产地|来自|种植|果园|云南|四川|山东',
        r'口感|香甜|清甜|酸甜|脆|多汁|好吃',
        r'价格|到手|元|块|斤|公斤|规格|一箱|一单|数量',
        r'发货|快递|包邮|配送|送达',
        r'售后|包赔|运费险|赔付|客服',
        r'下单|赶紧|抓紧|拍下|链接',
    )
]


def speech_units(paragraph: str) -> list[str]:
    # ponytail: conservative punctuation/topic heuristic; unpunctuated clauses stay
    # intact. A semantic model can replace grouping if review shows poor boundaries.
    sentences = re.findall(r'.+?(?:[。！？!?；;\n]+[”’」』）)]*|$)', paragraph, re.DOTALL)
    units, current, topics, count = [], '', set(), 0
    for sentence in sentences:
        found = {i for i, pattern in enumerate(TOPICS) if pattern.search(sentence)}
        dependent = bool(re.match(r'\s*(?:所以|因此|也就是|其中|这样|否则|才|就能|的话)', sentence))
        boundary = current and not dependent and (
            len(current) + len(sentence) > 120 or count >= 3
            or (topics and found and topics.isdisjoint(found))
        )
        if boundary:
            units.append(current.strip())
            current, topics, count = '', set(), 0
        current += sentence
        topics |= found
        count += 1
    if current.strip():
        units.append(current.strip())
    return units or [paragraph]
