"""Small client for the internal Gateway /synthesize endpoint."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, Optional, Tuple
from urllib import error, request


class TTSClientError(RuntimeError):
    pass


def load_api_key() -> str:
    if os.environ.get("TTS_API_KEY"):
        return os.environ["TTS_API_KEY"]
    try:
        return subprocess.check_output(
            ["/usr/bin/security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "com.ufun.tts.api-key", "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


class TTSClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", api_key: str = "", timeout: int = 900) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def synthesize(self, text: str, voice: str, retries: int = 1) -> Tuple[bytes, float]:
        payload = ("{\"text\":" + _json_string(text) + ",\"voice\":" + _json_string(voice) + "}").encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        started = time.monotonic()
        last_error = "TTS request failed"
        for _ in range(max(0, retries) + 1):
            req = request.Request(f"{self.base_url}/synthesize", data=payload, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    audio = response.read()
                if not audio.startswith(b"RIFF"):
                    raise TTSClientError("TTS returned a non-WAV response")
                return audio, (time.monotonic() - started) * 1000
            except (OSError, error.HTTPError, error.URLError, TTSClientError) as exc:
                last_error = str(exc)
        raise TTSClientError(last_error)


def _json_string(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
