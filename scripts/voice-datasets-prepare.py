#!/usr/bin/env python3
"""Generate all three speakers' checkpointed ASR candidates using an installed local model."""

import argparse
import fcntl
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_datasets.transcription import local_backend, prepare_speaker, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, default=ROOT / "runtime/voice-datasets")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--speaker", choices=["a", "b", "c"], action="append")
    args = parser.parse_args()
    if not (args.model / "weights.safetensors").is_file() or not (args.model / "config.json").is_file():
        parser.error("An installed local MLX model directory is required; no downloads are performed")
    args.datasets.mkdir(parents=True, exist_ok=True)
    lock = (args.datasets / "asr-job.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another ASR batch already holds this dataset lock")
    transcribe = local_backend(ROOT)
    statuses = {}
    for letter in args.speaker or ["a", "b", "c"]:
        try:
            statuses[letter] = prepare_speaker(args.datasets / f"speaker-{letter}", args.model, transcribe)
        except Exception as exc:
            statuses[letter] = {"state": "failed", "error": str(exc)}
            traceback.print_exc()
        write_json(args.datasets / "asr-job-status.json", statuses)
    print(json.dumps(statuses, ensure_ascii=False), flush=True)
    return int(any(status["state"] == "failed" for status in statuses.values()))


if __name__ == "__main__":
    raise SystemExit(main())
