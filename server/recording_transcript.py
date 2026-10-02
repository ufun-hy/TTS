#!/usr/bin/env python3
"""Standalone local web server for recording-to-readable-transcript V1."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import platform
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Put the package root before server/ even when PYTHONPATH already contains it.
sys.path.insert(0, str(ROOT))

from recording_transcript.results import save_result, load_result, list_results
from recording_transcript.pipeline import TranscriptError, transcribe_recording
from recording_transcript.qwen_asr import MODEL_DIRECTORY, readiness
from recording_transcript.qwen_asr_windows import MODEL_DIRECTORY as WINDOWS_MODEL_DIRECTORY, WorkerLifecycleError
from local_runtime import FileGpuLease


SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# Preserve the existing import/API surface for callers and diagnostic tests.
from recording_transcript.jobs import TranscriptJob, TranscriptJobStore as BaseJobStore, _worker_exit_confirmed


class TranscriptJobStore(BaseJobStore):
    def _transcribe(self, *args, **kwargs):
        return transcribe_recording(*args, **kwargs)


def _safe_filename(raw: str) -> str:
    decoded = urllib.parse.unquote(raw or "")
    name = re.split(r"[\\/]", decoded)[-1].strip()
    if not name or name in {".", ".."}:
        raise ValueError("请提供录音文件名")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("仅支持 wav、mp3、m4a、mp4 录音")
    return name[:255]


def _write_upload(handler: BaseHTTPRequestHandler) -> tuple[str, int, Path]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("上传大小无效") from exc
    if length <= 0:
        raise ValueError("请选择一个录音文件")
    if length > MAX_UPLOAD_BYTES:
        raise ValueError("录音文件不能超过 2 GiB")
    filename = _safe_filename(handler.headers.get("X-Filename", ""))
    directory = Path(tempfile.mkdtemp(prefix="recording-transcript-upload-"))
    target = directory / f"input{Path(filename).suffix.lower()}"
    remaining = length
    try:
        with target.open("wb") as stream:
            while remaining:
                chunk = handler.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("录音上传未完成，请重试")
                stream.write(chunk)
                remaining -= len(chunk)
    except Exception:
        try:
            target.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass
        raise
    return filename, length, target


class RecordingTranscriptServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self):
        self.RequestHandlerClass.jobs.shutdown()
        super().server_close()


def make_handler(project_root: Path, model: Path):
    html_path = project_root / "web" / "recording-transcript.html"
    jobs = TranscriptJobStore(project_root, model)

    class Handler(BaseHTTPRequestHandler):
        server_version = "recording-transcript/1.0"

        def _json(self, status: int, body: dict[str, Any]) -> None:
            import json

            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _html(self) -> None:
            try:
                body = html_path.read_bytes()
                if os.environ.get('WINDOWS_SINGLE_MACHINE') == '1':
                    script = (project_root / 'web' / 'windows-session.js').read_bytes()
                    body = body.replace(b'</body>', b'<script>'+script+b'</script></body>')
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path in {"/", "/index.html"}:
                self._html()
                return
            if path == "/api/health":
                self._json(200, {"status": "ok", **readiness(model), "text_studio_url": os.environ.get("TEXT_STUDIO_URL", "")})
                return
            if path == "/api/transcript/results":
                self._json(200, {"results": list_results(project_root)})
                return
            if path == '/api/transcript/jobs':
                self._json(200, {'jobs': jobs.list()})
                return
            prefix = "/api/transcript/jobs/"
            if path.startswith(prefix):
                job_id = path[len(prefix):]
                if not JOB_ID_RE.fullmatch(job_id):
                    self._json(404, {"error": "任务不存在"})
                    return
                try:
                    job = jobs.get(job_id)
                except (ValueError, OSError) as exc:
                    self._json(500, {"error": f"读取清理结果失败：{exc}"})
                    return
                if job is None:
                    self._json(404, {"error": "任务不存在"})
                    return
                self._json(200, job.public())
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            origin = self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
                self._json(403, {'error': '来源不允许'})
                return
            view = re.fullmatch(r'/api/session/view/([a-zA-Z0-9-]{8,80})(/close)?', path)
            if view and os.environ.get('WINDOWS_SINGLE_MACHINE') == '1':
                from local_runtime.session import view_event
                view_event(Path(os.environ['AI_LIVE_STUDIO_DATA']), view[1], bool(view[2]))
                self._json(200, {'ok': True})
                return
            if path == '/api/session/stop' and self.client_address[0] in {'127.0.0.1', '::1'}:
                self._json(200 if jobs.shutdown() else 409, {'stopped': jobs._closing.is_set()})
                return
            match = re.fullmatch(r'/api/transcript/jobs/([0-9a-f]{32})/(pause|resume|heartbeat|delete)', path)
            if match:
                # Prevent cross-site forms/beacons from controlling localhost jobs.
                origin = self.headers.get('Origin')
                if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
                    self._json(403, {'error': '来源不允许'})
                    return
                try:
                    self._json(200, jobs.action(*match.groups()))
                except KeyError:
                    self._json(404, {'error': '任务不存在'})
                except (ValueError, OSError) as exc:
                    self._json(409, {'error': str(exc)})
                return
            if path != "/api/transcript/jobs":
                self._json(404, {"error": "not_found"})
                return
            try:
                filename, size, upload_path = _write_upload(self)
                job = jobs.create(filename, size, upload_path)
                self._json(202, job.public())
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except OSError as exc:
                self._json(500, {"error": f"无法保存上传文件：{exc}"})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"recording-transcript {self.address_string()} - {fmt % args}", flush=True)

    Handler.jobs = jobs
    return Handler


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Local recording transcript web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--model", default=os.environ.get("RECORDING_TRANSCRIPT_MODEL", ""))
    args = parser.parse_args()
    default_model = WINDOWS_MODEL_DIRECTORY if platform.system() == "Windows" else MODEL_DIRECTORY
    model = Path(args.model).expanduser() if args.model else ROOT / "runtime" / "models" / "asr" / default_model
    html_path = ROOT / "web" / "recording-transcript.html"
    if not html_path.is_file():
        print(f"Missing {html_path}")
        return 1
    server = RecordingTranscriptServer((args.host, args.port), make_handler(ROOT, model))
    print(f"Recording transcript: http://{args.host}:{args.port}", flush=True)
    print(f"ASR backend: {readiness(model).get('asr_backend', 'unknown')}", flush=True)
    print(f"ASR model: {model}", flush=True)
    status = readiness(model)
    print(f"ASR status: {'ready' if status['asr_ready'] else status['asr_error']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
