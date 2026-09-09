"""HTTP API for the LAN audio cache."""

from __future__ import annotations

import base64
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

from timeline.tts_client import TTSClient

from .manager import AudioCacheError, AudioCacheManager, AudioItem


class AudioCacheServer(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(manager: AudioCacheManager, tts: Optional[TTSClient] = None, api_key: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "audio-cache/1.0"

        def _json(self, status: int, body: Dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            if not api_key:
                return True
            supplied = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return supplied.startswith(prefix) and hmac.compare_digest(supplied[len(prefix):], api_key)

        def _body(self) -> Dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                raise ValueError("content_type_must_be_application_json")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024 * 1024:
                raise ValueError("request body must be between 1 byte and 16 MiB")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            if path in ("/health", "/healthz"):
                self._json(200, {"status": "ok", "cache": manager.stats()})
                return
            if path == "/audio/next":
                item = manager.claim_next()
                if not item:
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json(200, _next_payload(item))
                return
            if path.startswith("/audio/files/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    audio = manager.audio_path(item_id)
                except AudioCacheError:
                    audio = None
                if not audio:
                    self._json(404, {"error": "audio_not_found"})
                    return
                self._send_file(audio)
                return
            if path.startswith("/audio/status/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    item = manager.get(item_id)
                except AudioCacheError:
                    item = None
                if not item:
                    self._json(404, {"error": "audio_not_found"})
                else:
                    self._json(200, _item_payload(item))
                return
            self._json(404, {"error": "not_found"})

        def _send_file(self, path: Path) -> None:
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        return
                    self.wfile.write(chunk)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if path == "/audio/ack":
                    item = manager.ack(str(body.get("id", "")), str(body.get("status", "completed")))
                    self._json(200, _item_payload(item))
                    return
                if path == "/audio/enqueue":
                    item = self._enqueue(body)
                    self._json(201, _item_payload(item))
                    return
                if path == "/audio/preload":
                    self._preload(body)
                    return
                self._json(404, {"error": "not_found"})
            except (AudioCacheError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                self._json(502, {"error": str(exc)})

        def _enqueue(self, body: Dict[str, Any]) -> AudioItem:
            item_id = body.get("id")
            metadata = {
                key: body[key]
                for key in ("text", "voice", "target_duration", "sequence", "source")
                if key in body
            }
            if "voice" not in metadata:
                metadata["voice"] = "default"
            encoded = body.get("audio_base64")
            if encoded is not None:
                if not isinstance(encoded, str):
                    raise ValueError("audio_base64 must be a string")
                audio = base64.b64decode(encoded, validate=True)
            else:
                text = str(body.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required when audio_base64 is absent")
                if not tts:
                    raise RuntimeError("TTS client is not configured")
                audio, _latency = tts.synthesize(text, str(metadata["voice"]))
            return manager.add_audio(audio, metadata, str(item_id) if item_id is not None else None)

        def _preload(self, body: Dict[str, Any]) -> None:
            segments = body.get("segments")
            if not isinstance(segments, list):
                raise ValueError("segments must be an array")
            target = int(body.get("preload_segments", getattr(self.server, "preload_segments", 5)))
            if target < 1:
                raise ValueError("preload_segments must be positive")
            ready = manager.stats()["ready"]
            generated = []
            for index, segment in enumerate(segments):
                if ready >= target:
                    break
                if not isinstance(segment, dict):
                    raise ValueError("every segment must be an object")
                item_id = str(segment.get("id", f"segment_{index + 1:03d}"))
                if manager.has(item_id):
                    continue
                payload = dict(segment)
                payload.setdefault("sequence", index)
                item = self._enqueue(payload)
                generated.append(item.id)
                ready += 1
            self._json(200, {"preload_segments": target, "generated": generated, "ready": manager.stats()["ready"]})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"audio-cache {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def _item_payload(item: AudioItem) -> Dict[str, Any]:
    return {"id": item.id, "status": item.status, "duration": item.duration, "metadata": item.public_metadata()}


def _next_payload(item: AudioItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "url": f"/audio/files/{item.id}",
        "duration": item.duration,
        "metadata": item.public_metadata(),
    }


def serve(manager: AudioCacheManager, host: str, port: int, tts: Optional[TTSClient] = None, api_key: str = "", preload_segments: int = 5) -> None:
    server = AudioCacheServer((host, port), make_handler(manager, tts, api_key))
    server.preload_segments = preload_segments
    print(f"Audio cache API: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
