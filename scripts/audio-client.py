#!/usr/bin/env python3
"""Pull one audio item or run the LAN audio client loop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audio_client.client import AudioClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/audio-client.example.json")
    parser.add_argument("--once", action="store_true", help="download one item and leave it awaiting consumption")
    parser.add_argument("--ack", action="store_true", help="ack the downloaded item as completed; useful only for smoke tests")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        poll_interval = float(config.get("poll_interval", 1))
        if poll_interval > 60:
            poll_interval /= 1000
        client = AudioClient(
            str(config["server"]),
            Path(config.get("cache_dir", config.get("cache", "cache"))),
            poll_interval,
            str(config.get("api_key", "")),
            int(config.get("timeout", 15)),
        )
        if args.once:
            item = client.fetch_next()
            if not item:
                print("no ready audio")
                return 0
            print(json.dumps({"id": item.id, "path": str(item.path), "duration": item.duration}, ensure_ascii=False))
            if args.ack:
                client.ack(item.id)
            return 0

        def consume(item):
            print(json.dumps({"id": item.id, "path": str(item.path), "duration": item.duration}, ensure_ascii=False), flush=True)
            return None

        client.run(consume)
        return 0
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"audio client failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
