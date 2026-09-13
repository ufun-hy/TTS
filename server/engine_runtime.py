"""Managed CosyVoice engine lifecycle with idle sleep and on-demand wake."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.request


class EngineRuntimeError(RuntimeError):
    pass


class ManagedEngine:
    """Keep the gateway alive while sleeping the heavyweight engine when idle."""

    def __init__(
        self,
        engine_url: str,
        command: Optional[list[str]] = None,
        log_path: Optional[Path] = None,
        idle_seconds: float = 600.0,
        startup_timeout: float = 180.0,
        poll_interval: float = 0.5,
        probe: Optional[Callable[[], bool]] = None,
        launcher: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine_url = engine_url.rstrip("/")
        self.command = list(command or [])
        self.log_path = Path(log_path) if log_path else None
        self.idle_seconds = max(0.0, float(idle_seconds))
        self.startup_timeout = max(1.0, float(startup_timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self._probe_impl = probe
        self._launcher_impl = launcher
        self._clock = clock
        self._managed = bool(self.command or self._launcher_impl)
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._process: Any = None
        self._active_requests = 0
        self._last_used = self._clock()
        self._closed = False
        self._wake_count = 0
        self._sleep_count = 0
        self._watchdog_stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        if self._managed and self.idle_seconds > 0:
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="cosyvoice-idle", daemon=True)
            self._watchdog.start()

    @property
    def managed(self) -> bool:
        return self._managed

    def stats(self) -> dict[str, Any]:
        with self._lock:
            active = self._active_requests
            last_used = self._last_used
            wake_count = self._wake_count
            sleep_count = self._sleep_count
        return {
            "state": self.state(),
            "managed": self._managed,
            "active_requests": active,
            "idle_seconds": self.idle_seconds,
            "idle_for_seconds": max(0.0, self._clock() - last_used),
            "wake_count": wake_count,
            "sleep_count": sleep_count,
        }

    def state(self) -> str:
        if self._probe_ready():
            return "ready"
        if self._managed:
            with self._lock:
                process = self._process
            if process is None or self._process_exited(process):
                return "sleeping"
            return "starting"
        return "not_ready"

    def run(self, callback: Callable[[bool], Any]) -> Any:
        """Wake if required, protect the engine from idle sleep, then run callback."""
        with self._lock:
            if self._closed:
                raise EngineRuntimeError("engine runtime is closed")
            self._active_requests += 1
        try:
            restarted = self.ensure_ready()
            return callback(restarted)
        finally:
            with self._lock:
                self._active_requests = max(0, self._active_requests - 1)
                self._last_used = self._clock()

    def ensure_ready(self) -> bool:
        if self._probe_ready():
            with self._lock:
                self._last_used = self._clock()
            return False
        if not self._managed:
            raise EngineRuntimeError("CosyVoice engine is not ready")

        with self._start_lock:
            if self._probe_ready():
                with self._lock:
                    self._last_used = self._clock()
                return False

            with self._lock:
                process = self._process
            restarted = False
            if process is None or self._process_exited(process):
                process = self._launch()
                with self._lock:
                    self._process = process
                    self._wake_count += 1
                restarted = True

            deadline = self._clock() + self.startup_timeout
            while self._clock() < deadline:
                if self._probe_ready():
                    with self._lock:
                        self._last_used = self._clock()
                    return restarted
                if self._process_exited(process):
                    raise EngineRuntimeError("CosyVoice engine exited before becoming ready")
                time.sleep(self.poll_interval)
            raise EngineRuntimeError("CosyVoice engine did not become ready before timeout")

    def sleep_if_idle(self) -> bool:
        """Stop only the managed engine; the gateway remains alive."""
        if not self._managed or self.idle_seconds <= 0:
            return False
        with self._start_lock:
            with self._lock:
                if self._active_requests:
                    return False
                process = self._process
                idle_for = self._clock() - self._last_used
                if process is None or self._process_exited(process) or idle_for < self.idle_seconds:
                    return False
                self._process = None
            self._terminate(process)
            with self._lock:
                self._sleep_count += 1
            return True

    def close(self) -> None:
        self._watchdog_stop.set()
        with self._lock:
            self._closed = True
        with self._start_lock:
            with self._lock:
                process = self._process
                self._process = None
            if process is not None and not self._process_exited(process):
                self._terminate(process)

    def _watchdog_loop(self) -> None:
        interval = min(5.0, max(0.25, self.idle_seconds / 4.0))
        while not self._watchdog_stop.wait(interval):
            try:
                self.sleep_if_idle()
            except Exception:
                # The watchdog must never take down the gateway. Health and the
                # next synthesis request will expose/recover engine failures.
                pass

    def _probe_ready(self) -> bool:
        if self._probe_impl:
            try:
                return bool(self._probe_impl())
            except Exception:
                return False
        try:
            request = urllib.request.Request(f"{self.engine_url}/status")
            with urllib.request.urlopen(request, timeout=1.5) as response:
                status = json.load(response)
            return status.get("status") == "ok" and status.get("model_loaded") is True
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _launch(self) -> Any:
        if self._launcher_impl:
            return self._launcher_impl()
        if not self.command:
            raise EngineRuntimeError("managed engine command is missing")
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.log_path.open("ab", buffering=0)
            try:
                return subprocess.Popen(
                    self.command,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                )
            finally:
                log_handle.close()
        return subprocess.Popen(
            self.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )

    @staticmethod
    def _process_exited(process: Any) -> bool:
        try:
            return process.poll() is not None
        except Exception:
            return True

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            process.terminate()
        except Exception:
            return
        try:
            process.wait(timeout=10)
            return
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            return
        try:
            process.wait(timeout=5)
        except Exception:
            pass
