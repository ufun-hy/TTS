"""Deterministic sentence exclusion at the Text Studio TTS boundary."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[1] / 'config/text-studio-prohibited.json'
RULES = json.loads(RULES_PATH.read_text(encoding='utf-8'))
_SENTENCES = re.compile(RULES['sentence_pattern'])
_NEGATION = re.compile(RULES['negation_pattern'])
_PATTERNS = [(rule, re.compile(rule['pattern'])) for rule in RULES['rules']]


def analyze(text: str) -> dict[str, Any]:
    blocked = []
    kept = []
    cursor = 0
    for sentence in _SENTENCES.finditer(text):
        raw = sentence.group()
        normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', raw))
        labels = []
        for rule, pattern in _PATTERNS:
            for hit in pattern.finditer(normalized):
                if rule.get('negation_sensitive') and _NEGATION.search(normalized[:hit.start()]):
                    continue
                labels.append(rule['label'])
                break
        if labels:
            kept.append(text[cursor:sentence.start()])
            cursor = sentence.end()
            blocked.append({'start': sentence.start(), 'end': sentence.end(), 'text': raw, 'labels': labels})
    kept.append(text[cursor:])
    safe = ''.join(kept).strip()
    if not any(char.isalnum() for char in safe):
        safe = ''
    return {'text': safe, 'blocked': blocked}


def broadcast_text(text: str) -> str:
    return analyze(text)['text']


def filter_segments(segments: Any) -> list[dict[str, Any]]:
    """Filter complete candidates before technical splitting or block merging."""
    if not isinstance(segments, list) or not segments:
        raise ValueError('segments must be a non-empty array')
    result = []
    seen = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('every segment must be an object')
        identifier = segment.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identifier):
            raise ValueError('every segment needs a valid id')
        if identifier in seen:
            raise ValueError(f'duplicate segment id: {identifier}')
        seen.add(identifier)
        item = dict(segment)
        if 'candidates' in item:
            if not isinstance(item['candidates'], list) or not item['candidates']:
                raise ValueError(f'segment {identifier} needs candidates')
            if any(not isinstance(value, str) or not value.strip() for value in item['candidates']):
                raise ValueError(f'segment {identifier} needs non-empty candidate strings')
            item['candidates'] = [safe for value in item['candidates'] if (safe := broadcast_text(value))]
            if item['candidates']:
                result.append(item)
        else:
            if not isinstance(item.get('text'), str) or not item['text'].strip():
                raise ValueError(f'segment {identifier} needs non-empty text')
            item['text'] = broadcast_text(item['text'])
            if item['text']:
                result.append(item)
    if not result:
        raise ValueError('全部文本均已屏蔽：没有可播报内容，请修改禁止播报话术。')
    return result
