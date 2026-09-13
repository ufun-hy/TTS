#!/usr/bin/env python3
"""Generate checkpointed voice-dataset ASR candidates with local Qwen3-ASR-1.7B."""

import argparse
import fcntl
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recording_transcript.qwen_asr import validate_model
from voice_datasets.transcription import local_backend, prepare_speaker, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, default=ROOT / "runtime/voice-datasets")
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "runtime/models/asr/qwen3-asr-1.7b-bf16",
        help="local Qwen3-ASR-1.7B MLX model directory",
    )
    parser.add_argument("--speaker", choices=["a", "b", "c"], action="append")
    args = parser.parse_args()
    try:
        args.model = validate_model(args.model)
    except ValueError as exc:
        parser.error(str(exc))
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
