#!/usr/bin/env python3
"""Group timestamped ASR fragments into simple context blocks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from timeline.resegmenter import ResegmentConfig, load_input, resegment, save_output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-context-duration", type=float, default=15.0)
    parser.add_argument("--pause-threshold", type=float, default=0.05)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        source = load_input(args.input)
        children, report = resegment(
            source,
            ResegmentConfig(
                max_context_duration=args.max_context_duration,
                pause_threshold=args.pause_threshold,
            ),
        )
        if not args.dry_run:
            save_output(args.output, args.input, children, report)
            report_path = args.report or args.output.parent / "reports" / "resegment-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"resegmentation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
