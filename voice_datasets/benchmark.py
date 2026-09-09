"""Reproducible zero-shot audio generation from reviewed references, without voice registration."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .review import read_jsonl, validate_review
from .transcription import sha256, write_json


def audio_info(path: Path):
    # CosyVoice writes IEEE float WAV; Python's wave module only accepts PCM WAV.
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                               "format=duration:stream=codec_name,sample_rate,channels",
                                               "-of", "json", str(path)], text=True))
    seconds = float(probe["format"]["duration"])
    if seconds <= 0:
        raise ValueError("Empty output audio")
    stream = probe["streams"][0]
    return {"output_duration": seconds, "sample_rate": int(stream["sample_rate"]),
            "channels": stream["channels"], "codec": stream["codec_name"]}


def inputs(root: Path):
    config = json.loads((root / "config/voice-benchmark-texts.json").read_text(encoding="utf-8"))
    snapshot = root / config["snapshot_file"]
    if sha256(snapshot) != config["source_sha256"]:
        raise ValueError("Benchmark source snapshot changed")
    original_text = snapshot.read_bytes().decode("utf-8-sig")
    ids = set()
    for test in config["tests"]:
        if test["id"] not in {"short", "normal", "long"} or test["id"] in ids:
            raise ValueError("Invalid or duplicate benchmark test ID")
        ids.add(test["id"])
        if original_text[test["source_start_character"]:test["source_end_character"]] != test["text"]:
            raise ValueError("Benchmark text is not an exact original excerpt")
    if ids != {"short", "normal", "long"}:
        raise ValueError("Short, normal and long texts are required")
    references = []
    for letter in "abc":
        folder = root / "runtime/voice-datasets" / f"speaker-{letter}"
        reference = json.loads((folder / "references/review-001.json").read_text(encoding="utf-8"))
        validate_review([reference], read_jsonl(folder / "sources.jsonl"))
        if reference["quality_status"] != "accepted" or not Path(reference["audio_path"]).is_file():
            raise ValueError("Reference must be reviewed and have real audio")
        reference["audio_sha256"] = sha256(Path(reference["audio_path"]))
        references.append(reference)
    return config, references


def run(root: Path, output: Path):
    config, references = inputs(root)
    if output.exists():
        raise ValueError("Use a new run directory; previous audio and results are preserved")
    binary = root / "runtime/bin/cosyvoice-cli"
    model = root / "runtime/models/CosyVoice3-2512_Q8_0.gguf"
    tokenizer = root / "runtime/models/speech_tokenizer_v3.int8.onnx"
    campplus = root / "runtime/models/campplus.int8.onnx"
    for required in [binary, model, tokenizer, campplus]:
        if not required.is_file():
            raise ValueError(f"Missing local runtime asset: {required}")
    output.mkdir(parents=True)
    environment = dict(os.environ)
    environment["DYLD_LIBRARY_PATH"] = f"/opt/homebrew/opt/icu4c/lib:{root / 'runtime/bin'}" + (
        ":" + environment["DYLD_LIBRARY_PATH"] if environment.get("DYLD_LIBRARY_PATH") else "")
    parameters = ["--mode", "zero-shot", "--backend", "auto", "--speed", "1", "--seed", "42",
                  "--seed-policy", "fixed", "--threads", "4", "--max-llm-len", "4096",
                  "--llm-kv-cache-type", "f16", "--llm-flash-attn", "0", "--flow-flash-attn", "0"]
    write_json(output / "run.json", {"created_at": datetime.now().astimezone().isoformat(),
                                   "config": config, "references": references, "parameters": parameters,
                                   "asset_sha256": {p.name: sha256(p) for p in [binary, model, tokenizer, campplus]},
                                   "text_splitting": "engine default; same for all speakers",
                                   "latency_scope": "CLI wall time includes model startup; not warm Runtime latency",
                                   "resource_conditions": "Local ASR batch may run concurrently; timing is diagnostic, not isolated performance measurement"})
    results = []
    for reference in references:
        speaker = reference["speaker_id"]
        folder = output / speaker
        folder.mkdir()
        prompt = folder / "prompt_speech.gguf"
        frontend_started = time.monotonic()
        try:
            with (folder / "frontend.log").open("w") as log:
                subprocess.run([str(binary), "--frontend-only", "--speech-tokenizer", str(tokenizer),
                                "--campplus", str(campplus), "--prompt-audio", reference["audio_path"],
                                "--prompt-text", reference["text"], "--prompt-speech-output", str(prompt)],
                               env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
            if not prompt.is_file() or not prompt.stat().st_size:
                raise RuntimeError("Frontend returned no prompt")
        except Exception as exc:
            results.append({"speaker_id": speaker, "stage": "frontend", "state": "failed", "error": str(exc)})
            write_json(output / "results.json", results)
            continue
        frontend_seconds = time.monotonic() - frontend_started
        for test in config["tests"]:
            audio_path = folder / f"{test['id']}.wav"
            result = {"speaker_id": speaker, "reference_id": reference["candidate_id"],
                      "test_id": test["id"], "text": test["text"], "audio_path": str(audio_path.resolve()),
                      "frontend_seconds": frontend_seconds, "perceptual_review": "pending"}
            started = time.monotonic()
            print(json.dumps({"speaker": speaker, "test": test["id"], "state": "synthesizing"}), flush=True)
            try:
                with (folder / f"{test['id']}.log").open("w") as log:
                    subprocess.run([str(binary), "--model", str(model), "--prompt-speech", str(prompt),
                                    "--text", test["text"], "--output", str(audio_path), *parameters],
                                   env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
                elapsed = time.monotonic() - started
                metadata = audio_info(audio_path)
                result.update(metadata)
                seconds = metadata["output_duration"]
                result.update(state="generated_pending_review", cli_wall_seconds=elapsed,
                              output_duration=seconds, cold_wall_rtf=elapsed / seconds)
            except Exception as exc:
                result.update(state="failed", error=str(exc), cli_wall_seconds=time.monotonic() - started)
            results.append(result)
            write_json(output / "results.json", results)
            print(json.dumps({key: value for key, value in result.items() if key not in {"text", "audio_path"}}, ensure_ascii=False), flush=True)
    return summarize(output, results)


def summarize(output: Path, results):
    lines = ["# A/B/C 首轮 Zero-shot 试听", "", "相同原文、参数、模型。音频生成成功不代表音色或长话术稳定性通过。", "",
             "耗时含 CLI 模型启动，且可能与 ASR 并行运行，不能直接用作线上 Runtime 延迟结论。", ""]
    for result in results:
        lines += [f"## {result['speaker_id']} / {result.get('test_id', 'frontend')}", ""]
        if result["state"] == "generated_pending_review":
            lines += [f"![试听]({result['audio_path']})", "",
                      f"输出 {result['output_duration']:.2f} 秒；CLI 总耗时 {result['cli_wall_seconds']:.2f} 秒。", ""]
        else:
            lines += [f"失败：{result.get('error')}", ""]
    (output / "listen.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    complete = len(results) == 9 and all(r["state"] == "generated_pending_review" for r in results)
    write_json(output / "status.json", {"state": "generated_pending_review" if complete else "partial",
                                       "completed_audio": sum(r["state"] == "generated_pending_review" for r in results),
                                       "primary_speaker": None, "fine_tune_recommended": None})
    return complete


def refresh_float_wav_results(output: Path):
    """Repair validation-only failures from older runs; never synthesize or alter audio."""
    path = output / "results.json"
    results = json.loads(path.read_text(encoding="utf-8"))
    backup = output / "results-before-wav-validation-fix.json"
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    for result in results:
        if result.get("error") == "unknown format: 3":
            metadata = audio_info(Path(result["audio_path"]))
            result.update(metadata, state="generated_pending_review",
                          cold_wall_rtf=result["cli_wall_seconds"] / metadata["output_duration"],
                          resolved_validation_error=result.pop("error"))
        log = output / result["speaker_id"] / f"{result.get('test_id', 'frontend')}.log"
        if log.is_file():
            contents = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.read_text(encoding="utf-8"))
            for field in ("model_load", "tts_generate", "total"):
                match = re.search(rf"\b{field}\s*:\s*([\d.]+) ms", contents)
                if match:
                    result[f"engine_{field}_seconds"] = float(match[1]) / 1000
    write_json(path, results)
    return summarize(output, results)
