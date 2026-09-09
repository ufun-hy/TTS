#!/usr/bin/env python3
"""Generate model-written candidate speech for timestamped segments."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from timeline.engine import GENERALIZE_STRATEGY_VERSION, Ollama, TimelineEngine, TimelineError, load_segments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON with a timestamped segments array")
    parser.add_argument("output", type=Path, help="generated timeline JSON")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--variants", type=int, default=5)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-segments", type=int)
    args = parser.parse_args()

    try:
        segments = load_segments(args.input)
        if args.max_segments:
            segments = segments[: args.max_segments]
        engine = TimelineEngine(Ollama(args.model, args.ollama_url), variant_count=args.variants, seed=args.seed)
        completed = {}
        if args.resume and args.output.is_file() and not args.force:
            with args.output.open(encoding="utf-8") as handle:
                previous = json.load(handle)
            for item in previous.get("segments", []):
                if item.get("generalize_status") == "completed":
                    completed[item["id"]] = item

        def checkpoint(snapshot):
            payload = {
                "schema_version": 1,
                "generalize_strategy_version": GENERALIZE_STRATEGY_VERSION,
                "source": str(args.input),
                "model": args.model,
                "segments": snapshot["segments"],
                "failed_segments": snapshot["failed_segments"],
                "stats": snapshot["stats"],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")

        result = engine.process_batch(
            segments,
            max_retries=max(0, args.max_retries),
            completed=completed,
            checkpoint=checkpoint,
            progress=print,
        )
        checkpoint(result)

        report_path = args.report or args.output.parent / "reports" / f"generalize-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "source": str(args.input),
            "output": str(args.output),
            "model": args.model,
            "generalize_strategy_version": GENERALIZE_STRATEGY_VERSION,
            "total_segments": result["total"],
            "success_segments": result["success"],
            "failed_segments": result["failed"],
            **result["stats"],
        }
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, TimelineError, ValueError) as exc:
        print(f"timeline generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"completed={result['success']} failed={result['failed']} total={result['total']} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
