"""Sentence-first speech units, preserving source text and fact combinations."""
import re

STRONG_SENTENCE = re.compile(r'.+?(?:[。！？!?；;\n]+[”’」』）)]*|$)', re.DOTALL)
# Adjacent clauses in one fact combination stay together, even beyond 40 chars.
FACT_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r'到手|价格|售价|规格|净重|每(?:个|箱|份|斤)|赠品|赠送|'
    r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)\s*(?:元|块|斤|公斤|克|个|件|箱|袋|份|两|毫升|升)',
    r'发货|包邮|运费|配送|送达|快递|偏远地区',
    r'售后|坏果|破损|包赔|赔付|退款|退货|联系客服',
))
CONDITION = re.compile(r'如果|只要|只有|凡是|若|收到.*(?:问题|破损|坏果)')
CONTINUATION = re.compile(r'\s*(?:每个|其中|分别|也就是|否则|才|就|所以|因此|而且|并且|以及|再|还会|同时)')
UNFINISHED = re.compile(r'(?:包括|分别是|比如|例如|赠送|需要|按照|以|把|给|与|和|或者|以及|的话)[，,\s]*$')
PAIRS = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')', '【': '】', '[': ']', '"': '"'}


def _comma_clauses(sentence: str) -> list[str]:
    """Only expose pauses outside quotes/brackets and numeric separators."""
    clauses, closers = [], []
    start = 0
    for index, char in enumerate(sentence):
        if closers and char == closers[-1]:
            closers.pop()
        elif char in PAIRS:
            closers.append(PAIRS[char])
        elif char in '，,' and not closers:
            if index and index + 1 < len(sentence) and sentence[index - 1].isdigit() and sentence[index + 1].isdigit():
                continue
            clauses.append(sentence[start:index + 1])
            start = index + 1
    if start < len(sentence):
        clauses.append(sentence[start:])
    return clauses


def _linked(left: str, right: str) -> bool:
    return bool(CONDITION.search(left) or UNFINISHED.search(left) or CONTINUATION.match(right)
                or any(pattern.search(left) and pattern.search(right) for pattern in FACT_PATTERNS))


def _split_long_sentence(sentence: str) -> list[str]:
    clauses = _comma_clauses(sentence)
    groups = []
    for index, clause in enumerate(clauses):
        if index and _linked(clauses[index - 1], clause):
            groups[-1] += clause
        else:
            groups.append(clause)

    units, current = [], ''
    remaining = sum(len(group) for group in groups)
    for group in groups:
        remaining -= len(group)
        # Leave a preceding clause with a short tail instead of isolating it.
        short_tail = 0 < remaining < 15 and len(current.strip()) >= 15
        if current and (len((current + group).strip()) > 35 or short_tail):
            units.append(current.strip())
            current = ''
        current += group
    if current.strip():
        units.append(current.strip())
    return units


def speech_units(paragraph: str) -> list[str]:
    units = []
    for match in STRONG_SENTENCE.finditer(paragraph):
        sentence = match.group().strip()
        if not sentence:
            continue
        # A terminated sentence is never merged with the next, however short.
        units.extend(_split_long_sentence(sentence) if len(sentence) > 40 else [sentence])
    return units
