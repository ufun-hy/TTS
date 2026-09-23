#!/usr/bin/env python3
"""Prepare reproducible A/B/C text plans, then synthesize without entering Live queues."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.context_variants import build_context_groups, build_variant_prompt, validate_variant_result
from server.live_session import choose_candidate_round, prepare_candidate_pools, prepare_live_segments, resolve_dynamic_time
from server.live_session_blocks import prepare_synthesis_blocks
from server.prohibited_speech import filter_segments
from server.semantic_tts_blocks import semantic_blocks
from server.text_studio import _run_provider
from timeline.tts_client import TTSClient, load_api_key


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(args) -> Path:
    project = json.loads(args.project.read_text(encoding="utf-8"))
    units = project["paragraphs"][args.start - 1:args.start - 1 + args.units]
    if not units or len(units) != args.units:
        raise ValueError("requested unit range is not present in project")
    output = ROOT / "runtime/ab/context-variants" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    write_json(output / "source.json", {"project_id": project.get("project_id"), "paragraphs": units})
    now = datetime.now().astimezone()
    source = [{"id": p["id"], "text": resolve_dynamic_time(p["original_text"], now)} for p in units]
    cases = [{"label": "A-original", "blocks": semantic_blocks(source)}]
    provider, model = args.provider or project.get("provider", "codex"), args.model or project.get("model", "")
    groups = build_context_groups(units)
    results = []
    by_id = {p["id"]: p for p in units}
    for group in groups:
        members = [by_id[i] for i in group["paragraph_ids"]]
        start = units.index(members[0])
        end = start + len(members)
        prompt = build_variant_prompt(members, args.variants, project.get("instruction", ""),
                                      units[start - 1]["original_text"] if start else "",
                                      units[end]["original_text"] if end < len(units) else "")
        (output / f"{group['id']}-prompt.txt").write_text(prompt, encoding="utf-8")
        print(f"Generating {group['id']} ({len(members)} units) with {provider}/{model or 'default'}", flush=True)
        diagnostic = {"response_path": str(output / f"{group['id']}-response.txt"),
                      "stderr_path": str(output / f"{group['id']}-stderr.txt")}
        raw = _run_provider(provider, prompt, args.timeout, diagnostic=diagnostic, model=model)
        result = validate_variant_result(raw, members, args.variants, group["id"], 1)
        write_json(output / f"{group['id']}-result.json", result)
        results.append(result)
    generated = {p["id"]: p for r in results for p in r["paragraphs"]}
    pools = prepare_candidate_pools(filter_segments([{"id": p["id"], "candidates": p["candidates"]} for p in units]))
    for index in range(args.variants):
        selected, indexes = choose_candidate_round(pools, rng=random.Random(args.seed + index))
        cases.append({"label": f"B{index + 1}-independent", "indexes": indexes,
                      "blocks": prepare_synthesis_blocks(prepare_live_segments(selected))})
        segments = [{"id": p["id"], "text": resolve_dynamic_time(generated[p["id"]]["candidates"][index], now)} for p in units]
        cases.append({"label": f"C{index + 1}-context", "variant_index": index,
                      "blocks": semantic_blocks(segments)})
    ui_segments = [{"id": p["id"], "text": p.get("editedText") or p["candidates"][p.get("selectedIndex", 0)]} for p in units]
    cases.append({"label": "B-ui-selected", "blocks": prepare_synthesis_blocks(prepare_live_segments(filter_segments(ui_segments)))})
    manifest = {"source_project": project.get("project_id"), "source_ids": [p["id"] for p in units],
                "voice": args.voice or project.get("voice", "speaker_c"), "provider": provider, "model": model,
                "seed": args.seed, "dynamic_time": now.isoformat(), "groups": [r["group"] for r in results],
                "A_single_request": len(cases[0]["blocks"]) == 1,
                "notes": ["TTS randomness is not fixed by the Gateway API.",
                          "Identical text may reuse TTS Result Cache; cache hit is not exposed in WAV response.",
                          "B uses existing candidates; C is newly generated. No Windows queue writes.",
                          "Audio is untrimmed PCM concatenation at speed 1 and original gain."], "cases": cases}
    write_json(output / "manifest.json", manifest)
    print(output / "manifest.json", flush=True)
    return output / "manifest.json"


def synthesize(path: Path, gateway: str, labels: list[str] | None = None) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    client = TTSClient(gateway, load_api_key())
    if not client.api_key:
        raise RuntimeError("TTS API key is unavailable")
    output = path.parent
    cases = [c for c in manifest["cases"] if not labels or c["label"] in labels]
    if labels and set(labels) != {c["label"] for c in cases}:
        raise ValueError("unknown case label")
    for case in cases:
        pcm_paths = []
        for index, block in enumerate(case["blocks"], 1):
            text = block["text"]
            if not 0 < len(text) <= 200:
                raise ValueError("prepared block exceeds Gateway limit")
            name = f"{case['label']}-{index:03d}"
            wav_path = output / f"{name}.wav"
            identity = hashlib.sha256((manifest["voice"] + '\0' + text).encode()).hexdigest()
            if not wav_path.exists():
                print(f"Synthesizing {name}: {len(text)} chars", flush=True)
                audio, latency = client.synthesize(text, manifest["voice"])
                wav_path.write_bytes(audio)
                block.update(audio=wav_path.name, latency_ms=round(latency, 1), identity=identity)
                write_json(path, manifest)
            elif block.get("identity") != identity:
                raise ValueError(f"refusing to reuse audio without matching manifest identity: {wav_path}")
            pcm = output / f"{name}-pcm.wav"
            if not pcm.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(wav_path),
                                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(pcm)], check=True)
            pcm_paths.append(pcm)
        combined = output / f"{case['label']}-continuous.wav"
        with wave.open(str(combined), "wb") as dest:
            dest.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            elapsed = 0.0
            for pcm, block in zip(pcm_paths, case["blocks"]):
                with wave.open(str(pcm), "rb") as source:
                    dest.writeframes(source.readframes(source.getnframes()))
                    duration = source.getnframes() / source.getframerate()
                block.update(start_seconds=round(elapsed, 3), duration_seconds=round(duration, 3))
                elapsed += duration
        case.update(audio=combined.name, duration_seconds=round(elapsed, 3))
        write_json(path, manifest)
    order = [c for c in manifest["cases"] if c.get("audio")]
    random.Random(manifest["seed"]).shuffle(order)
    mapping = {f"Sample {i}": case["label"] for i, case in enumerate(order, 1)}
    write_json(output / "listening-key.json", mapping)
    html = '<!doctype html><meta charset="utf-8"><title>连续话术试听</title><style>body{max-width:760px;margin:40px auto;font:16px system-ui}audio{width:100%}textarea{width:100%;height:70px}section{margin:28px 0}</style><h1>连续话术试听</h1><p>逐项比较承接、语气、重起调、停顿、重音、情绪与直播感（1–5 分）；记录问题时间点。</p>'
    for i, case in enumerate(order, 1):
        html += f'<section><h2>Sample {i}</h2><audio controls src="{case["audio"]}"></audio><textarea placeholder="评分和时间点"></textarea></section>'
    html += '<button onclick="const a=document.createElement(\'a\');a.href=URL.createObjectURL(new Blob([JSON.stringify([...document.querySelectorAll(\'textarea\')].map((e,i)=>({sample:i+1,notes:e.value})),null,2)],{type:\'application/json\'}));a.download=\'listening-scores.json\';a.click()">下载试听记录</button>'
    (output / "listen.html").write_text(html, encoding="utf-8")
    print(output / "listen.html", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--start", type=int, default=3, help="1-based first unit")
    parser.add_argument("--units", type=int, default=7)
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--voice", default="")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--synthesize", type=Path, help="synthesize an existing manifest, without regenerating text")
    parser.add_argument("--gateway", default="http://127.0.0.1:8765")
    parser.add_argument("--labels", nargs="+", help="optional cases to synthesize from a prepared plan")
    args = parser.parse_args()
    if args.synthesize:
        synthesize(args.synthesize, args.gateway, args.labels)
    elif args.project and args.start > 0 and args.units > 0:
        prepare(args)
    else:
        parser.error("use --project with positive --start/--units, or --synthesize manifest.json")


if __name__ == "__main__":
    main()
