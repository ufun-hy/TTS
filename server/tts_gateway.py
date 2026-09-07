#!/usr/bin/env python3
"""Small /speak gateway for cosyvoice-server.

The upstream server keeps the model warm. This process adds the task-specific
contract: validate text, serialize requests, play each WAV, then acknowledge.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Event, Thread


class Job:
    def __init__(self, text: str, audio_dir: Path) -> None:
        self.text = text
        self.audio_path = audio_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.wav"
        self.done = Event()
        self.error: str | None = None
        self.timings: tuple[float, float, float] | None = None


def synthesize_and_play(job: Job, engine_url: str) -> None:
    started = time.monotonic()
    payload = json.dumps({
        "model": "cosyvoice-3",
        "voice": "alloy",
        "input": job.text,
        "response_format": "wav",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{engine_url}/v1/audio/speech",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            audio = response.read()
        job.audio_path.write_bytes(audio)
        synthesized = time.monotonic()
        subprocess.run(["/usr/bin/afplay", str(job.audio_path)], check=True)
        finished = time.monotonic()
        job.timings = (synthesized - started, finished - synthesized, finished - started)
        print(
            f"played chars={len(job.text)} synthesis={job.timings[0]:.2f}s "
            f"playback={job.timings[1]:.2f}s total={job.timings[2]:.2f}s file={job.audio_path}",
            flush=True,
        )
    except (OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        job.error = str(exc)
    finally:
        job.done.set()


class Gateway(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(queue: Queue[Job], max_chars: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "local-tts/1.0"

        def _json(self, status: int, body: dict) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(200, {"status": "ok", "queue": queue.qsize()})
            else:
                self._json(404, {"success": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/speak":
                self._json(404, {"success": False, "error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_048_576:
                    raise ValueError("request body must be between 1 byte and 1 MiB")
                body = json.loads(self.rfile.read(length))
                text = body.get("text") if isinstance(body, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text is required and must not be empty")
                text = text.strip()
                if len(text) > max_chars:
                    raise ValueError(f"text is too long; maximum is {max_chars} characters")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return

            job = Job(text, self.server.audio_dir)  # type: ignore[attr-defined]
            queue.put(job)
            job.done.wait()
            if job.error:
                self._json(502, {"success": False, "error": job.error})
            else:
                self._json(200, {"success": True})

        def log_message(self, fmt: str, *args) -> None:
            print(f"gateway {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--engine-url", default="http://127.0.0.1:8766")
    parser.add_argument("--audio-dir", type=Path, default=Path("runtime/audio"))
    parser.add_argument("--max-chars", type=int, default=200)
    args = parser.parse_args()
    args.audio_dir.mkdir(parents=True, exist_ok=True)

    queue: Queue[Job] = Queue()

    def worker() -> None:
        while True:
            job = queue.get()
            try:
                synthesize_and_play(job, args.engine_url)
            finally:
                queue.task_done()

    Thread(target=worker, name="tts-playback", daemon=True).start()
    server = Gateway((args.host, args.port), make_handler(queue, args.max_chars))
    server.audio_dir = args.audio_dir  # type: ignore[attr-defined]
    print(f"API: http://{args.host}:{args.port}/speak", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
