#!/usr/bin/env python3
"""Authenticated FIFO gateway for the local CosyVoice WebUI API."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Callable

try:
    from server.engine_runtime import EngineRuntimeError, ManagedEngine
except ModuleNotFoundError:  # direct execution: python server/tts_gateway.py
    from engine_runtime import EngineRuntimeError, ManagedEngine


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

    def load_local(self) -> None:
        """Refresh prompt metadata without waking a sleeping engine."""
        with self._lock:
            mtime = self.config_path.stat().st_mtime_ns
            if self._voices and mtime == self._mtime:
                return
            self._voices = self._load_config()
            self._mtime = mtime
            self._initialized = False

    def invalidate_registration(self) -> None:
        with self._lock:
            self._initialized = False

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
        self.load_local()
        with self._lock:
            return sorted(self._voices)

    def cache_fingerprint(self, voice_id: str) -> str:
        """Fingerprint the current prompt so changed voices miss old cache entries."""
        self.load_local()
        with self._lock:
            prompt = self._voices.get(voice_id)
            config_mtime = self._mtime
        if prompt is None:
            raise ValueError(f"voice not found: {voice_id}")
        stat = prompt.stat()
        return f"{voice_id}|{prompt.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{config_mtime}"


class TTSResultCache:
    """Persistent synthesis cache keyed by text, voice prompt, and cache revision."""

    def __init__(self, root: Path, voice_store: VoiceStore, revision: str = "v1") -> None:
        self.root = Path(root)
        self.voice_store = voice_store
        self.revision = revision
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, text: str, voice: str) -> Path:
        payload = json.dumps(
            {
                "revision": self.revision,
                "voice": voice,
                "voice_fingerprint": self.voice_store.cache_fingerprint(voice),
                "text": text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return self.root / digest[:2] / f"{digest}.wav"

    def get_or_create(self, text: str, voice: str, producer) -> tuple[bytes, bool]:
        path = self._path(text, voice)
        with self._lock:
            if path.is_file():
                try:
                    audio = path.read_bytes()
                except OSError:
                    audio = b""
                if audio.startswith(b"RIFF"):
                    self._hits += 1
                    return audio, True
                try:
                    path.unlink()
                except OSError:
                    pass

            audio = producer()
            if not isinstance(audio, (bytes, bytearray)) or not bytes(audio).startswith(b"RIFF"):
                raise ValueError("TTS returned a non-WAV response")
            audio = bytes(audio)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(audio)
            os.replace(temporary, path)
            self._misses += 1
            return audio, False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses}


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


def _engine_request_text(text: str, voice: str, engine_url: str) -> bytes:
    payload = json.dumps({
        "text": text,
        "voice": voice,
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


def synthesize_and_play(
    job: Job,
    synthesize_text: Callable[[str, str], bytes],
    result_cache: TTSResultCache | None = None,
) -> None:
    started = time.monotonic()
    queue_wait_ms = (started - job.enqueued_at) * 1000
    cache_hit = False
    try:
        if result_cache:
            audio, cache_hit = result_cache.get_or_create(
                job.text,
                job.voice,
                lambda: synthesize_text(job.text, job.voice),
            )
        else:
            audio = synthesize_text(job.text, job.voice)
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
            "cache_hit": cache_hit,
            "result": "ok",
        }), flush=True)
    except (EngineRuntimeError, OSError, ValueError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
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
    engine_runtime: ManagedEngine,
    synthesize_text: Callable[[str, str], bytes],
    max_chars: int,
    result_cache: TTSResultCache | None = None,
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

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/health", "/healthz"):
                worker_ready = self.server.worker_thread.is_alive()  # type: ignore[attr-defined]
                engine = engine_runtime.stats()
                engine_state = engine["state"]
                engine_available = engine_state in ("ready", "sleeping", "starting") if engine_runtime.managed else engine_state == "ready"
                ready = worker_ready and engine_available
                self._json(200 if ready else 503, {
                    "status": "ok" if ready else "error",
                    "tts": engine_state,
                    "queue": "ready" if worker_ready else "not_ready",
                    "queue_depth": queue.qsize(),
                    "engine": engine,
                    "tts_cache": result_cache.stats() if result_cache else {"hits": 0, "misses": 0},
                })
                return
            if self.path == "/voices":
                if not self._authorized():
                    self._json(401, {"success": False, "error": "unauthorized"})
                    return
                try:
                    voice_store.load_local()
                    self._json(200, {"voices": [{"id": voice_id, "available": True} for voice_id in voice_store.ids()]})
                except (OSError, ValueError, RuntimeError) as exc:
                    self._json(503, {"voices": [], "error": str(exc)})
                return
            self._json(404, {"success": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/speak", "/synthesize"):
                self._json(404, {"success": False, "error": "not_found"})
                return
            if not self._authorized():
                self._json(401, {"success": False, "error": "unauthorized"})
                return
            if self.path == "/synthesize" and self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json(403, {"success": False, "error": "local_only"})
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
                voice_store.load_local()
                if voice not in voice_store.ids():
                    self._json(404, {"success": False, "error": "voice_not_found"})
                    return
            except (ValueError, TypeError, json.JSONDecodeError, OSError, RuntimeError) as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return

            if self.path == "/synthesize":
                try:
                    if result_cache:
                        audio, cache_hit = result_cache.get_or_create(
                            text,
                            voice,
                            lambda: synthesize_text(text, voice),
                        )
                    else:
                        audio = synthesize_text(text, voice)
                        cache_hit = False
                except (EngineRuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
                    self._json(502, {"success": False, "error": "tts_failed", "detail": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("X-TTS-Cache", "HIT" if cache_hit else "MISS")
                self.end_headers()
                self.wfile.write(audio)
                return

            job = Job(text, voice, self.client_address[0], self.server.audio_dir)  # type: ignore[attr-defined]
            queue.put(job)
            job.done.wait()
            if job.error:
                self._json(502, {"success": False, "job_id": job.job_id, "error": "tts_failed"})
            else:
                self._json(200, {"success": True, "job_id": job.job_id})

        def log_message(self, fmt: str, *args) -> None:
            if os.environ.get("TTS_HTTP_LOG", "0") == "1":
                print(f"gateway {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def worker_loop(
    queue: Queue[Job],
    synthesize_text: Callable[[str, str], bytes],
    result_cache: TTSResultCache | None = None,
) -> None:
    while True:
        job = queue.get()
        try:
            synthesize_and_play(job, synthesize_text, result_cache)
        finally:
            queue.task_done()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--engine-url", default="http://127.0.0.1:8766")
    parser.add_argument("--engine-bin", type=Path)
    parser.add_argument("--engine-model", type=Path)
    parser.add_argument("--engine-backend", default="auto")
    parser.add_argument("--engine-log", type=Path, default=Path("runtime/logs/cosyvoice-server.log"))
    parser.add_argument("--engine-idle-seconds", type=float, default=600.0)
    parser.add_argument("--engine-startup-timeout", type=float, default=180.0)
    parser.add_argument("--engine-verbose", action="store_true")
    parser.add_argument("--audio-dir", type=Path, default=Path("runtime/audio"))
    parser.add_argument("--tts-cache-dir", type=Path, default=Path("runtime/tts-cache"))
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
    args.tts_cache_dir = args.tts_cache_dir if args.tts_cache_dir.is_absolute() else root / args.tts_cache_dir
    args.engine_log = args.engine_log if args.engine_log.is_absolute() else root / args.engine_log
    args.audio_dir.mkdir(parents=True, exist_ok=True)

    voice_store = VoiceStore(root, args.voices_config, args.engine_url)
    try:
        voice_store.load_local()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"voice configuration is not ready: {exc}", file=sys.stderr)
        return 1

    engine_command: list[str] = []
    if args.engine_bin or args.engine_model:
        if not args.engine_bin or not args.engine_model:
            print("--engine-bin and --engine-model must be provided together", file=sys.stderr)
            return 2
        engine_bin = args.engine_bin if args.engine_bin.is_absolute() else root / args.engine_bin
        engine_model = args.engine_model if args.engine_model.is_absolute() else root / args.engine_model
        engine_command = [
            str(engine_bin),
            "--model", str(engine_model),
            "--served-model-name", "cosyvoice-3",
            "--backend", args.engine_backend,
            "--host", "127.0.0.1",
            "--port", args.engine_url.rsplit(":", 1)[-1],
            "--concurrency", "1",
        ]
        if args.engine_verbose:
            engine_command.append("--verbose")

    engine_runtime = ManagedEngine(
        args.engine_url,
        command=engine_command,
        log_path=args.engine_log if engine_command else None,
        idle_seconds=args.engine_idle_seconds,
        startup_timeout=args.engine_startup_timeout,
    )

    def synthesize_text(text: str, voice: str) -> bytes:
        def perform(restarted: bool) -> bytes:
            if restarted:
                voice_store.invalidate_registration()
            voice_store.sync()
            return _engine_request_text(text, voice, args.engine_url)

        return engine_runtime.run(perform)

    # External-engine mode preserves the previous deployment contract and
    # validates readiness at startup. Managed mode intentionally starts cold.
    if not engine_runtime.managed:
        try:
            engine_runtime.run(lambda _restarted: voice_store.sync())
        except (EngineRuntimeError, OSError, ValueError, RuntimeError) as exc:
            print(f"voice configuration is not ready: {exc}", file=sys.stderr)
            return 1

    result_cache = TTSResultCache(
        args.tts_cache_dir,
        voice_store,
        os.environ.get("TTS_CACHE_REVISION", "v1"),
    )
    queue: Queue[Job] = Queue()
    worker_thread = threading.Thread(
        target=lambda: worker_loop(queue, synthesize_text, result_cache),
        name="tts-playback",
        daemon=True,
    )
    worker_thread.start()
    server = Gateway((args.host, args.port), make_handler(
        queue,
        voice_store,
        RateLimiter(args.rate_limit_per_minute),
        api_key,
        engine_runtime,
        synthesize_text,
        args.max_chars,
        result_cache,
    ))
    server.audio_dir = args.audio_dir  # type: ignore[attr-defined]
    server.worker_thread = worker_thread  # type: ignore[attr-defined]

    def shutdown_handler(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, name="tts-shutdown", daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

    print(f"API: http://{args.host}:{args.port}/speak", flush=True)
    print(f"Voices: {', '.join(voice_store.ids())}", flush=True)
    print(f"TTS cache: {args.tts_cache_dir}", flush=True)
    print(f"Engine idle sleep: {args.engine_idle_seconds:g}s" if engine_runtime.managed else "Engine mode: external", flush=True)
    print("Authentication: Bearer API key", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine_runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
