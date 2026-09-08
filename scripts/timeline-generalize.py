#!/usr/bin/env python3
"""Generate a reusable, time-ordered speech template from timestamped text."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from timeline.engine import Ollama, TimelineEngine, TimelineError, load_segments, save_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON with a timestamped segments array")
    parser.add_argument("output", type=Path, help="generated timeline JSON")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--variants", type=int, default=5)
    parser.add_argument("--duration-tolerance", type=float, default=0.15)
    parser.add_argument("--cooldown-window", type=int, default=3)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    try:
        segments = load_segments(args.input)
        engine = TimelineEngine(Ollama(args.model, args.ollama_url), args.variants, args.duration_tolerance, args.cooldown_window, args.seed)
        result = engine.process(segments)
        save_result(args.output, result, args.input, args.model)
    except (OSError, TimelineError, ValueError) as exc:
        print(f"timeline generation failed: {exc}", file=sys.stderr)
        return 1
    print(f"generated {len(result['segments'])} segments -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
