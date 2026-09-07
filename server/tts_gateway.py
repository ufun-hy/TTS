#!/usr/bin/env python3
"""Authenticated FIFO gateway for the local CosyVoice WebUI API."""

from __future__ import annotations

import argparse
from collections import deque
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue


VOICE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class VoiceStore:
    def __init__(self, root: Path, config_path: Path, engine_url: str) -> None:
        self.root = root
        self.config_path = config_path if config_path.is_absolute() else root / config_path
        self.engine_url = engine_url.rstrip("/")
        self._lock = threading.RLock()
        self._voices: dict[str, Path] = {}
        self._mtime: int | None = None
        self._initialized = False

    def _load_config(self) -> dict[str, Path]:
        with self.config_path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or not raw:
            raise ValueError("voices.json must contain at least one voice")

        voices: dict[str, Path] = {}
        for voice_id, entry in raw.items():
            if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
                raise ValueError(f"invalid voice id: {voice_id!r}")
            if not isinstance(entry, dict) or not isinstance(entry.get("prompt_speech"), str):
                raise ValueError(f"voice {voice_id!r} needs prompt_speech")
            prompt = Path(entry["prompt_speech"])
            if not prompt.is_absolute():
                prompt = self.root / prompt
            if prompt.is_file():
                voices[voice_id] = prompt
        if "default" not in voices:
            raise ValueError("default voice is not available")
        return voices

    def _request(self, method: str, path: str, body: dict | None = None) -> int:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.engine_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def sync(self) -> None:
        with self._lock:
            mtime = self.config_path.stat().st_mtime_ns
            if self._initialized and mtime == self._mtime:
                return
            voices = self._load_config()
            old_ids = set(self._voices)
            for voice_id in old_ids - set(voices):
                self._request("DELETE", f"/speaker/{voice_id}")
            for voice_id, prompt in voices.items():
                status = self._request(
                    "POST",
                    "/speaker",
                    {"type": "gguf", "name": voice_id, "path": str(prompt)},
                )
                if status not in (200, 409):
                    raise RuntimeError(f"could not register voice {voice_id!r}: HTTP {status}")
            self._voices = voices
            self._mtime = mtime
            self._initialized = True

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._voices)


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] >= 60:
                self._hits.popleft()
            if len(self._hits) >= self.per_minute:
                return False
            self._hits.append(now)
            return True


class Job:
    def __init__(self, text: str, voice: str, source: str, audio_dir: Path) -> None:
        self.job_id = uuid.uuid4().hex[:12]
        self.text = text
        self.voice = voice
        self.source = source
        self.audio_path = audio_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.job_id}.wav"
        self.enqueued_at = time.monotonic()
        self.done = threading.Event()
        self.error: str | None = None


def _engine_request(job: Job, engine_url: str) -> bytes:
    payload = json.dumps({
        "text": job.text,
        "voice": job.voice,
        "response_format": "wav",
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/tts",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return response.read()


def synthesize_and_play(job: Job, engine_url: str) -> None:
    started = time.monotonic()
    queue_wait_ms = (started - job.enqueued_at) * 1000
    try:
        audio = _engine_request(job, engine_url)
        job.audio_path.write_bytes(audio)
        synthesized = time.monotonic()
        subprocess.run(["/usr/bin/afplay", str(job.audio_path)], check=True)
        finished = time.monotonic()
        print(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "job_id": job.job_id,
            "source": job.source,
            "voice": job.voice,
            "text_length": len(job.text),
            "queue_wait_ms": round(queue_wait_ms),
            "synthesis_ms": round((synthesized - started) * 1000),
            "playback_ms": round((finished - synthesized) * 1000),
            "result": "ok",
        }), flush=True)
    except (OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        job.error = str(exc)
        print(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "job_id": job.job_id,
            "source": job.source,
            "voice": job.voice,
            "text_length": len(job.text),
            "queue_wait_ms": round(queue_wait_ms),
            "result": "error",
        }), flush=True)
    finally:
        job.done.set()


class Gateway(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(
    queue: Queue[Job],
    voice_store: VoiceStore,
    rate_limiter: RateLimiter,
    api_key: str,
    engine_url: str,
    max_chars: int,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "local-tts/2.0"

        def _json(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return supplied.startswith(prefix) and hmac.compare_digest(supplied[len(prefix):], api_key)

        def _engine_ready(self) -> bool:
            try:
                request = urllib.request.Request(f"{engine_url.rstrip('/')}/status")
                with urllib.request.urlopen(request, timeout=2) as response:
                    status = json.load(response)
                return status.get("status") == "ok" and status.get("model_loaded") is True
            except (OSError, ValueError, urllib.error.URLError):
                return False

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/health", "/healthz"):
                engine_ready = self._engine_ready()
                worker_ready = self.server.worker_thread.is_alive()  # type: ignore[attr-defined]
                ready = engine_ready and worker_ready
                self._json(200 if ready else 503, {
                    "status": "ok" if ready else "error",
                    "tts": "ready" if engine_ready else "not_ready",
                    "queue": "ready" if worker_ready else "not_ready",
                    "queue_depth": queue.qsize(),
                })
                return
            if self.path == "/voices":
                if not self._authorized():
                    self._json(401, {"success": False, "error": "unauthorized"})
                    return
                try:
                    voice_store.sync()
                    self._json(200, {"voices": [{"id": voice_id, "available": True} for voice_id in voice_store.ids()]})
                except (OSError, ValueError, RuntimeError) as exc:
                    self._json(503, {"voices": [], "error": str(exc)})
                return
            self._json(404, {"success": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/speak":
                self._json(404, {"success": False, "error": "not_found"})
                return
            if not self._authorized():
                self._json(401, {"success": False, "error": "unauthorized"})
                return
            if not rate_limiter.allow():
                self._json(429, {"success": False, "error": "rate_limited"}, {"Retry-After": "60"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                self._json(415, {"success": False, "error": "content_type_must_be_application_json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_048_576:
                    raise ValueError("request body must be between 1 byte and 1 MiB")
                body = json.loads(self.rfile.read(length))
                text = body.get("text") if isinstance(body, dict) else None
                voice = body.get("voice", "default") if isinstance(body, dict) else "default"
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text is required and must not be empty")
                if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
                    self._json(400, {"success": False, "error": "voice_not_found"})
                    return
                text = text.strip()
                if len(text) > max_chars:
                    raise ValueError(f"text is too long; maximum is {max_chars} characters")
                voice_store.sync()
                if voice not in voice_store.ids():
                    self._json(404, {"success": False, "error": "voice_not_found"})
                    return
            except (ValueError, TypeError, json.JSONDecodeError, OSError, RuntimeError) as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return

            job = Job(text, voice, self.client_address[0], self.server.audio_dir)  # type: ignore[attr-defined]
            queue.put(job)
            job.done.wait()
            if job.error:
                self._json(502, {"success": False, "job_id": job.job_id, "error": "tts_failed"})
            else:
                self._json(200, {"success": True, "job_id": job.job_id})

        def log_message(self, fmt: str, *args) -> None:
            print(f"gateway {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def worker_loop(queue: Queue[Job], engine_url: str) -> None:
    while True:
        job = queue.get()
        try:
            synthesize_and_play(job, engine_url)
        finally:
            queue.task_done()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--engine-url", default="http://127.0.0.1:8766")
    parser.add_argument("--audio-dir", type=Path, default=Path("runtime/audio"))
    parser.add_argument("--voices-config", type=Path, default=Path("voices.json"))
    parser.add_argument("--max-chars", type=int, default=200)
    parser.add_argument("--rate-limit-per-minute", type=int, default=30)
    args = parser.parse_args()

    api_key = os.environ.get("TTS_API_KEY", "")
    if not api_key and os.environ.get("TTS_ALLOW_NO_AUTH") != "1":
        print("TTS_API_KEY is required; use the launchd service or explicitly set TTS_ALLOW_NO_AUTH=1", file=sys.stderr)
        return 2
    if not api_key:
        api_key = uuid.uuid4().hex

    root = Path(__file__).resolve().parents[1]
    args.audio_dir = args.audio_dir if args.audio_dir.is_absolute() else root / args.audio_dir
    args.audio_dir.mkdir(parents=True, exist_ok=True)
    voice_store = VoiceStore(root, args.voices_config, args.engine_url)
    try:
        voice_store.sync()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"voice configuration is not ready: {exc}", file=sys.stderr)
        return 1

    queue: Queue[Job] = Queue()
    worker_thread = threading.Thread(
        target=lambda: worker_loop(queue, args.engine_url),
        name="tts-playback",
        daemon=True,
    )
    worker_thread.start()
    server = Gateway((args.host, args.port), make_handler(
        queue, voice_store, RateLimiter(args.rate_limit_per_minute), api_key, args.engine_url, args.max_chars,
    ))
    server.audio_dir = args.audio_dir  # type: ignore[attr-defined]
    server.worker_thread = worker_thread  # type: ignore[attr-defined]
    print(f"API: http://{args.host}:{args.port}/speak", flush=True)
    print(f"Voices: {', '.join(voice_store.ids())}", flush=True)
    print("Authentication: Bearer API key", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
