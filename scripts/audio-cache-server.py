#!/usr/bin/env python3
"""Run the LAN audio cache API and connect it to the local TTS gateway."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audio_cache.manager import AudioCacheManager
from audio_cache.processing import AudioProcessingConfig, AudioProcessor
from audio_cache.server import serve
from timeline.tts_client import TTSClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root", type=Path, default=ROOT / "runtime/audio-cache")
    parser.add_argument("--config", type=Path, default=ROOT / "config/audio-cache.example.json")
    parser.add_argument("--tts-url", default="http://127.0.0.1:8765")
    parser.add_argument("--tts-api-key", default=os.environ.get("TTS_API_KEY", ""))
    parser.add_argument("--api-key", default=os.environ.get("AUDIO_CACHE_API_KEY", ""))
    args = parser.parse_args()
    try:
        raw = json.loads(args.config.read_text(encoding="utf-8")) if args.config.is_file() else {}
        if not isinstance(raw, dict):
            raise ValueError("audio cache config must be a JSON object")
        processing = AudioProcessingConfig.from_dict(raw)
        root = args.root if args.root.is_absolute() else ROOT / args.root
        manager = AudioCacheManager(root, AudioProcessor(processing).process)
        tts = TTSClient(args.tts_url, args.tts_api_key) if args.tts_url else None
        serve(manager, args.host, args.port, tts, args.api_key, int(raw.get("preload_segments", 5)))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"audio cache server failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
