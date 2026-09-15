"""One-shot Windows CUDA ASR worker; process exit releases model memory."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from .qwen_asr_windows import _transcribe_in_process


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m recording_transcript.windows_worker AUDIO MODEL", file=sys.stderr)
        return 2
    try:
        result = _transcribe_in_process(Path(argv[1]), Path(argv[2]))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
