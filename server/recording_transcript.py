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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Put the package root before server/ even when PYTHONPATH already contains it.
sys.path.insert(0, str(ROOT))

from recording_transcript.results import save_result, load_result, list_results
from recording_transcript.pipeline import TranscriptError, transcribe_recording


SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class TranscriptJob:
    job_id: str
    filename: str
    size: int
    upload_path: Path
    stage: str = "queued"
    text: str = ""
    error: str = ""
    updated_at: float = 0.0

    def public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "job_id": self.job_id,
            "filename": self.filename,
            "size": self.size,
            "stage": self.stage,
        }
        if self.stage == "completed":
            body["text"] = self.text
        elif self.stage == "failed":
            body["error"] = self.error or "录音转文稿失败"
        return body


class TranscriptJobStore:
    def __init__(self, project_root: Path, model: Path):
        self.project_root = project_root
        self.model = model
        self._jobs: dict[str, TranscriptJob] = {}
        self._lock = threading.Lock()
        self._asr_lock = threading.Lock()

    def create(self, filename: str, size: int, upload_path: Path) -> TranscriptJob:
        job = TranscriptJob(uuid.uuid4().hex, filename, size, upload_path, updated_at=time.time())
        with self._lock:
            self._jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"transcript-{job.job_id[:8]}").start()
        return job

    def get(self, job_id: str) -> TranscriptJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        try:
            result = load_result(self.project_root, job_id)
        except FileNotFoundError:
            return None
        return TranscriptJob(job_id, result['filename'], result['size'], Path(),
                             stage='completed', text=result['text'], updated_at=result['updated_at'])

    def _set_stage(self, job: TranscriptJob, stage: str) -> None:
        with self._lock:
            job.stage = stage
            job.updated_at = time.time()

    def _run(self, job: TranscriptJob) -> None:
        try:
            # ponytail: serialize local MLX jobs; concurrent model loads waste memory.
            with self._asr_lock:
                final_text = transcribe_recording(
                    job.upload_path,
                    self.project_root,
                    self.model,
                    on_stage=lambda stage: self._set_stage(job, stage),
                )
            save_result(self.project_root, job.job_id, job.filename, job.size, final_text)
            with self._lock:
                job.text = final_text
                job.stage = "completed"
                job.updated_at = time.time()
        except TranscriptError as exc:
            with self._lock:
                job.error = str(exc)
                job.stage = "failed"
                job.updated_at = time.time()
        except Exception as exc:  # keep a long-running server alive on one failed upload
            with self._lock:
                job.error = f"录音转文稿失败：{exc}"
                job.stage = "failed"
                job.updated_at = time.time()
        finally:
            try:
                job.upload_path.unlink(missing_ok=True)
                job.upload_path.parent.rmdir()
            except OSError:
                pass


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
                self._json(200, {"status": "ok", "asr_ready": model.exists(), "text_studio_url": os.environ.get("TEXT_STUDIO_URL", "")})
                return
            if path == "/api/transcript/results":
                self._json(200, {"results": list_results(project_root)})
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

    return Handler


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Local recording transcript web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--model", default=os.environ.get("TTS_ASR_MODEL", ""))
    args = parser.parse_args()
    model = Path(args.model).expanduser() if args.model else ROOT / "runtime" / "models" / "asr" / "large-v3-turbo"
    html_path = ROOT / "web" / "recording-transcript.html"
    if not html_path.is_file():
        print(f"Missing {html_path}")
        return 1
    server = RecordingTranscriptServer((args.host, args.port), make_handler(ROOT, model))
    print(f"Recording transcript: http://{args.host}:{args.port}", flush=True)
    print(f"ASR model: {model}", flush=True)
    print(f"ASR status: {'ready' if model.exists() else 'not found'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
