"""Managed CosyVoice engine lifecycle with idle sleep and on-demand wake."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
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
        gpu_lease: Any = None,
        borrowed_gpu_owners: tuple[str, ...] = ("tts", "tts-preview", "live-session"),
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
        self._gpu_lease = gpu_lease
        self._borrowed_gpu_owners = tuple(borrowed_gpu_owners)
        self._gpu_lease_held = False
        self._gpu_lease_owned = False
        self._managed = bool(self.command or self._launcher_impl)
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._process: Any = None
        self._active_requests = 0
        self._last_used = self._clock()
        self._closed = False
        self._last_error = ""
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
            last_error = self._last_error
            gpu_lease_held = self._gpu_lease_held
        return {
            "state": self.state(),
            "managed": self._managed,
            "active_requests": active,
            "idle_seconds": self.idle_seconds,
            "idle_for_seconds": max(0.0, self._clock() - last_used),
            "wake_count": wake_count,
            "sleep_count": sleep_count,
            "last_error": last_error,
            "gpu_lease_held": gpu_lease_held,
        }

    def state(self) -> str:
        with self._lock:
            last_error = self._last_error
            process = self._process
        # A failed stop owns the lifecycle until the process is explicitly
        # observed as exited. A healthy HTTP probe must not hide that error.
        if last_error:
            return "stop_failed"
        if self._probe_ready():
            return "ready"
        if self._managed:
            if process is None or self._process_exited(process):
                return "sleeping"
            if last_error:
                return "stop_failed"
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
        if not self._managed:
            raise EngineRuntimeError("CosyVoice engine is not ready")

        # Admission and stop/kill share one lifecycle lock. In particular, a
        # request cannot use a still-responsive engine while sleep_if_idle()
        # is waiting for termination to finish.
        with self._start_lock:
            with self._lock:
                process = self._process
                last_error = self._last_error
            if last_error:
                if process is None or self._process_exited(process):
                    if not self._release_gpu_lease():
                        raise EngineRuntimeError(f"CosyVoice engine cannot be reused: {last_error}")
                    with self._lock:
                        self._process = None
                        self._last_error = ""
                    process = None
                else:
                    raise EngineRuntimeError(f"CosyVoice engine cannot be reused: {last_error}")

            if self._probe_ready():
                with self._lock:
                    self._last_used = self._clock()
                    self._last_error = ""
                return False

            restarted = False
            if process is not None and self._process_exited(process):
                with self._lock:
                    self._process = None
                process = None
            if process is None:
                process = self._launch()
                with self._lock:
                    self._process = process
                    self._wake_count += 1
                    self._last_error = ""
                restarted = True

            deadline = self._clock() + self.startup_timeout
            while self._clock() < deadline:
                if self._probe_ready():
                    with self._lock:
                        self._last_used = self._clock()
                        self._last_error = ""
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
                idle_for = self._clock() - self._last_used
                if self._process is None or idle_for < self.idle_seconds:
                    return False
            return self._stop_locked()

    def stop_now(self) -> bool:
        """Stop the managed engine and release its GPU lease only after exit."""
        if not self._managed:
            return True
        with self._start_lock:
            return self._stop_locked()

    def close(self) -> None:
        self._watchdog_stop.set()
        with self._lock:
            self._closed = True
        with self._start_lock:
            self._stop_locked(allow_active=True)

    def _stop_locked(self, allow_active: bool = False) -> bool:
        """Run with ``_start_lock`` held; retain all ownership on failure."""
        with self._lock:
            if self._active_requests and not allow_active:
                return False
            process = self._process
        if process is not None and not self._process_exited(process):
            try:
                self._terminate(process)
            except EngineRuntimeError as exc:
                with self._lock:
                    self._last_error = str(exc)
                return False
        if process is not None and not self._process_exited(process):
            with self._lock:
                self._last_error = "CosyVoice engine process is still alive"
            return False
        if not self._release_gpu_lease():
            with self._lock:
                self._last_error = "GPU owner could not be released after engine exit"
            return False
        with self._lock:
            self._process = None
            self._last_error = ""
            if process is not None:
                self._sleep_count += 1
        return True

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
        self._acquire_gpu_lease()
        try:
            if self._launcher_impl:
                return self._launcher_impl()
            if not self.command:
                raise EngineRuntimeError("managed engine command is missing")
            env = os.environ.copy()
            if sys.platform == "darwin":
                # macOS can strip DYLD_* when the gateway starts via system Python.
                # Set it at the final native-process boundary, on every cold wake.
                paths = [str(Path(self.command[0]).resolve().parent), "/opt/homebrew/opt/icu4c/lib"]
                if env.get("DYLD_LIBRARY_PATH"):
                    paths.append(env["DYLD_LIBRARY_PATH"])
                env["DYLD_LIBRARY_PATH"] = ":".join(paths)
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
                        env=env,
                    )
                finally:
                    log_handle.close()
            return subprocess.Popen(
                self.command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                env=env,
            )
        except BaseException:
            self._release_gpu_lease()
            raise

    def _acquire_gpu_lease(self) -> None:
        if self._gpu_lease is None:
            return
        with self._lock:
            if self._gpu_lease_held:
                return
        try:
            acquired = bool(self._gpu_lease.acquire())
        except Exception as exc:
            raise EngineRuntimeError(f"GPU owner acquire failed: {exc}") from exc
        if acquired:
            with self._lock:
                self._gpu_lease_held = True
                self._gpu_lease_owned = True
            return
        current = {}
        reader = getattr(type(self._gpu_lease), "read", None)
        path = getattr(self._gpu_lease, "path", None)
        if reader and path is not None:
            try:
                value = reader(path)
                current = value if isinstance(value, dict) else {}
            except Exception:
                current = {}
        if current.get("owner") in self._borrowed_gpu_owners:
            with self._lock:
                self._gpu_lease_held = True
                self._gpu_lease_owned = False
            return
        owner = current.get("owner") or "unknown"
        stage = current.get("stage") or "busy"
        raise EngineRuntimeError(f"GPU is busy: {stage} ({owner})")

    def _release_gpu_lease(self) -> bool:
        with self._lock:
            if not self._gpu_lease_held:
                return True
            if not self._gpu_lease_owned:
                self._gpu_lease_held = False
                return True
            lease = self._gpu_lease
        try:
            released = bool(lease.release()) if lease is not None else True
        except Exception:
            released = False
        if released:
            with self._lock:
                self._gpu_lease_held = False
                self._gpu_lease_owned = False
        return released

    @staticmethod
    def _process_exited(process: Any) -> bool:
        try:
            return process.poll() is not None
        except Exception:
            # An unreadable process handle is not proof of termination.
            return False

    @staticmethod
    def _terminate(process: Any) -> None:
        """Terminate a child and verify it exited before returning."""
        errors: list[str] = []
        try:
            process.terminate()
        except Exception as exc:
            errors.append(f"terminate failed: {exc}")
        try:
            process.wait(timeout=10)
        except Exception:
            pass
        if ManagedEngine._process_exited(process):
            return
        try:
            process.kill()
        except Exception as exc:
            errors.append(f"kill failed: {exc}")
        try:
            process.wait(timeout=5)
        except Exception:
            pass
        if ManagedEngine._process_exited(process):
            return
        detail = "; ".join(errors) or "process remained alive after terminate and kill"
        raise EngineRuntimeError(f"CosyVoice engine could not be stopped: {detail}")
