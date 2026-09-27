#!/usr/bin/env python3
"""Measure cached segment switches without modifying the live client/cache.

Creates a retained diagnostic cache of silent PCM16 files. On Windows uses
real WinMM; --simulate is explicitly a software-only measurement. Run each
implementation with the same arguments and storage volume for comparison.
"""

import argparse
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_client import playback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Parent directory on the cache volume")
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--segments", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=.25)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--baseline-source", type=Path)
    args = parser.parse_args()
    if not 2 <= args.segments <= args.items or args.seconds <= 0:
        parser.error("require 2 <= segments <= items and seconds > 0")
    if os.name != "nt" and not args.simulate:
        parser.error("native measurement requires Windows; use --simulate for software only")
    module = playback
    if args.baseline_source:
        spec = importlib.util.spec_from_file_location("audio_client.probe_baseline", args.baseline_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    args.output.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="switch-", dir=args.output))
    cache = output / "cache"
    cache.mkdir()
    audio = cache / "00000.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0\0" * round(24000 * args.seconds))
    wav_bytes = audio.read_bytes()
    for index in range(args.items):
        item_id = f"{index:05d}"
        if index:
            (cache / f"{item_id}.wav").write_bytes(wav_bytes)
        (cache / f"{item_id}.json").write_text(json.dumps({
            "id": item_id, "status": "completed", "playback_status": "cached",
            "duration": args.seconds, "sequence": index,
            "downloaded_at": "2026-09-24T00:00:00+00:00",
            "server_metadata": {"session_id": "switch-probe", "sequence": index},
        }), encoding="utf-8")

    logger = logging.getLogger("switch-probe")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(output / "client.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    player = module.WinMMPlayer(logger=logger)
    native_mci = player._mci
    commands = []
    deadline = 0.0

    def mci(command, target, length, callback):
        nonlocal deadline
        before = time.perf_counter()
        if args.simulate:
            if command.startswith("play "):
                deadline = before + args.seconds
            if command.startswith("status "):
                target.value = "playing" if before < deadline else "stopped"
            code = 0
        else:
            code = native_mci(command, target, length, callback)
        after = time.perf_counter()
        if not command.startswith("status ") or target.value.strip().lower() == "stopped":
            commands.append({"command": command.split()[0], "before": before, "after": after, "code": code})
        return code

    player._mci = mci
    controller = module.PlaybackController(cache, callback=lambda stats: None, logger=logger, player=player)
    phases = []
    for name in ("_items", "_next_item", "_emit", "_set_status"):
        original = getattr(controller, name)

        def timed(*values, _name=name, _original=original, **kwargs):
            before = time.perf_counter()
            try:
                return _original(*values, **kwargs)
            finally:
                phases.append({"phase": _name, "thread": threading.current_thread().name,
                               "before": before, "after": time.perf_counter()})

        setattr(controller, name, timed)
    original_play = player.play
    completed = []

    def play(*values):
        original_play(*values)
        completed.append(time.perf_counter())
        if len(completed) >= args.segments:
            controller._stop.set()

    player.play = play
    controller.start()
    limit = time.monotonic() + max(120, args.segments * (args.seconds + 10))
    while controller.is_running() and time.monotonic() < limit:
        time.sleep(.1)
    controller.stop()
    starts = [event["after"] for event in commands if event["command"] == "play"]
    ends = [event["after"] for event in commands if event["command"] == "status"]
    gaps = [(starts[index + 1] - ends[index]) * 1000 for index in range(min(len(starts) - 1, len(ends)))]
    summary = {
        "platform": sys.platform, "device": "simulated" if args.simulate else "Windows WinMM",
        "source": str(args.baseline_source or Path(playback.__file__)),
        "source_sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
        "items": args.items, "segments_completed": len(completed), "output": str(output),
        "stopped_observed_to_next_play_ms": gaps,
        "median_ms": statistics.median(gaps) if gaps else None,
        "max_ms": max(gaps) if gaps else None,
        "phases_median_ms": {name: statistics.median([
            (event["after"] - event["before"]) * 1000 for event in phases if event["phase"] == name
        ]) for name in sorted({event["phase"] for event in phases})},
    }
    (output / "results.json").write_text(json.dumps({**summary, "commands": commands, "phases": phases}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    if len(completed) != args.segments or len(gaps) != args.segments - 1:
        raise SystemExit("Incomplete measurement; inspect client.log")


if __name__ == "__main__":
    main()
