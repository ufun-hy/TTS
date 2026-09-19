"""Small client for the internal Gateway /synthesize endpoint."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import errno
import http.client
import logging
import math
import os
import socket
import subprocess
import time
from typing import Tuple
from urllib import error, request


class TTSClientError(RuntimeError):
    retryable = False

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class RetryableTTSClientError(TTSClientError):
    retryable = True

    def retry_delay(self, retry_count: int) -> float:
        # Keep a minimum delay even for Retry-After: 0 to prevent a hot loop.
        if self.retry_after is not None:
            return max(1.0, self.retry_after)
        return min(30.0, 2.0 ** min(max(0, retry_count - 1), 5))


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdigit():
            seconds = float(value)
        else:
            seconds = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        return seconds if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _temporary_transport_error(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    return (
        isinstance(reason, (TimeoutError, socket.timeout, ConnectionError, http.client.IncompleteRead))
        or isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN
        or isinstance(reason, OSError) and reason.errno in (
            errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
            errno.ECONNREFUSED, errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH,
        )
    )


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
        retries = max(0, retries)
        for attempt in range(retries + 1):
            req = request.Request(f"{self.base_url}/synthesize", data=payload, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    audio = response.read()
                if not audio.startswith(b"RIFF"):
                    raise TTSClientError("TTS returned a non-WAV response")
                return audio, (time.monotonic() - started) * 1000
            except error.HTTPError as exc:
                error_type = RetryableTTSClientError if exc.code in (429, 502, 503, 504) else TTSClientError
                failure = error_type(
                    str(exc), status_code=exc.code,
                    retry_after=_retry_after(exc.headers.get("Retry-After") if exc.headers else None),
                )
                exc.close()
            except (OSError, error.URLError, http.client.HTTPException) as exc:
                error_type = RetryableTTSClientError if _temporary_transport_error(exc) else TTSClientError
                failure = error_type(str(exc))
            logging.getLogger(__name__).warning(
                "TTS request error status=%s retryable=%s retry_after=%s retry_count=%s error=%s",
                failure.status_code, failure.retryable, failure.retry_after, attempt, failure,
            )
            if not isinstance(failure, RetryableTTSClientError) or attempt == retries:
                raise failure
            time.sleep(failure.retry_delay(attempt + 1))


def _json_string(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
