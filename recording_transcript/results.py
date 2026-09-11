"""Durable cleaned transcript results shared by the recording UI and Text Studio."""
from __future__ import annotations

import json
import os
import math
from pathlib import Path
import re
import time
from typing import Any

RESULT_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def _result_path(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not RESULT_ID_RE.fullmatch(job_id):
        raise ValueError('无效的文稿 ID')
    return root / 'runtime' / 'recording-transcript' / 'results' / f'{job_id}.json'


def save_result(root: Path, job_id: str, filename: str, size: int, text: str) -> dict[str, Any]:
    path = _result_path(root, job_id)
    if not isinstance(text, str) or not text.strip():
        raise ValueError('清理结果为空，无法保存')
    result = {'schema_version': 1, 'job_id': job_id, 'filename': filename,
              'size': size, 'stage': 'completed', 'text': text,
              'updated_at': time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)
    return result


def load_result(root: Path, job_id: str) -> dict[str, Any]:
    result = json.loads(_result_path(root, job_id).read_text(encoding='utf-8'))
    if (not isinstance(result, dict) or result.get('job_id') != job_id
            or result.get('stage') != 'completed'
            or not isinstance(result.get('text'), str) or not result['text'].strip()
            or not isinstance(result.get('filename'), str)
            or type(result.get('size')) is not int or result['size'] < 0
            or type(result.get('updated_at')) not in (int, float)
            or not math.isfinite(result['updated_at'])):
        raise ValueError('无效的清理结果')
    return result


def list_results(root: Path) -> list[dict[str, Any]]:
    results = []
    directory = root / 'runtime' / 'recording-transcript' / 'results'
    for path in directory.glob('*.json'):
        try:
            result = load_result(root, path.stem)
            results.append({key: result[key] for key in ('job_id', 'filename', 'updated_at')})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(results, key=lambda item: item['updated_at'], reverse=True)
