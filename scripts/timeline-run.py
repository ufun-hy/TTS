#!/usr/bin/env python3
"""Run a generated timeline with runtime selection and lookahead TTS."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from timeline.cache import AudioCache
from timeline.player import Player
from timeline.runtime_selector import RuntimeSelector
from timeline.scheduler import LookaheadScheduler
from timeline.session import RuntimeSession
from timeline.tts_client import TTSClient, load_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timeline", type=Path)
    parser.add_argument("--voice", default="default")
    parser.add_argument("--lookahead", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-segments", type=int)
    parser.add_argument("--tts-url", default="http://127.0.0.1:8765")
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "runtime")
    args = parser.parse_args()
    try:
        timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
        segments = timeline.get("segments") if isinstance(timeline, dict) else None
        if not isinstance(segments, list) or not segments:
            raise ValueError("timeline must contain a non-empty segments array")
        if args.max_segments:
            timeline["segments"] = segments[:args.max_segments]
        timeline_id = str(timeline.get("timeline_id") or hashlib.sha256(args.timeline.read_bytes()).hexdigest()[:12])
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{timeline_id[:6]}"
        session = RuntimeSession(session_id, timeline_id)
        session_path = args.runtime_root / "sessions" / f"{session_id}.json"
        report_path = args.runtime_root / "reports" / f"{session_id}.json"
        cache = AudioCache(args.runtime_root / "audio-cache", timeline_id, session_id)
        tts = None if args.dry_run else TTSClient(args.tts_url, load_api_key())
        if not args.dry_run and not tts.api_key:
            raise ValueError("TTS_API_KEY is required for non-dry runs")
        scheduler = LookaheadScheduler(
            timeline,
            session,
            session_path,
            report_path,
            RuntimeSelector(seed=args.seed),
            cache,
            tts,
            Player(dry_run=args.dry_run),
            args.voice,
            lookahead=args.lookahead,
            dry_run=args.dry_run,
        )
        report = scheduler.run()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"timeline runtime failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
