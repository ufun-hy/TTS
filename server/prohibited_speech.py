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


def validate_reviews(reviews: Any) -> list[dict]:
    if not isinstance(reviews, list) or len(reviews) > 1000:
        raise ValueError('prohibited_reviews must be an array of at most 1000 confirmations')
    for review in reviews:
        if not isinstance(review, dict) or type(review.get('candidate_index')) is not int or review['candidate_index'] < 0:
            raise ValueError('invalid prohibited review candidate')
        if type(review.get('start')) is not int or type(review.get('rules_version')) is not int:
            raise ValueError('invalid prohibited review position or rule version')
        for field in ('candidate_text', 'text', 'confirmed_at'):
            if not isinstance(review.get(field), str) or not review[field] or len(review[field]) > 100000:
                raise ValueError(f'invalid prohibited review {field}')
        if not isinstance(review.get('labels'), list) or not review['labels'] or any(not isinstance(v, str) for v in review['labels']):
            raise ValueError('invalid prohibited review labels')
    return reviews


def candidate_reviews(item: dict, index: int, text: str) -> list[dict]:
    return [r for r in validate_reviews(item.get('prohibited_reviews', []))
            if r['candidate_index'] == index and r['candidate_text'] == text.strip()]


def is_confirmed(text: str, hit: dict, reviews: list[dict]) -> bool:
    """Exact candidate and sentence occurrence, never a keyword/global allow-list."""
    return any(r.get('candidate_text') == text and r.get('start') == hit['start']
               and r.get('text') == hit['text'] and r.get('labels') == hit['labels']
               and r.get('rules_version') == RULES['version'] for r in reviews)


def analyze(text: str, reviews: list[dict] | None = None) -> dict[str, Any]:
    blocked = []
    approved = []
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
            hit = {'start': sentence.start(), 'end': sentence.end(), 'text': raw, 'labels': labels}
            if is_confirmed(text, hit, reviews or []):
                approved.append(hit)
                continue
            kept.append(text[cursor:sentence.start()])
            cursor = sentence.end()
            blocked.append(hit)
    kept.append(text[cursor:])
    safe = ''.join(kept).strip()
    if not any(char.isalnum() for char in safe):
        safe = ''
    return {'text': safe, 'blocked': blocked, 'approved': approved}


def broadcast_text(text: str, reviews: list[dict] | None = None) -> str:
    return analyze(text, reviews)['text']


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
            item['candidates'] = [safe for index, value in enumerate(item['candidates'])
                                  if (safe := broadcast_text(value.strip(), candidate_reviews(item, index, value)))]
            if item['candidates']:
                result.append(item)
        else:
            if not isinstance(item.get('text'), str) or not item['text'].strip():
                raise ValueError(f'segment {identifier} needs non-empty text')
            item['text'] = broadcast_text(item['text'].strip(), candidate_reviews(item, 0, item['text']))
            if item['text']:
                result.append(item)
    if not result:
        raise ValueError('全部文本均已屏蔽：没有可播报内容，请修改或人工确认命中的话术。')
    return result
