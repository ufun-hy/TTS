"""One-shot Windows CUDA ASR worker; process exit releases model memory."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import os
import threading
from contextlib import redirect_stdout


def watch_parent() -> None:
    """A handle identifies this parent instance even if Windows reuses its PID."""
    if os.name != 'nt' or not os.environ.get('AI_ASR_PARENT_PID'):
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    parent = kernel.OpenProcess(0x00100000, False, int(os.environ['AI_ASR_PARENT_PID']))
    if not parent:
        raise RuntimeError('无法确认 ASR 主进程，停止启动子进程')
    def watch():
        try:
            if kernel.WaitForSingleObject(parent, 0xffffffff) == 0:
                os._exit(1)
        finally:
            kernel.CloseHandle(parent)
    threading.Thread(target=watch, daemon=True).start()

from .qwen_asr_windows import _transcribe_in_process


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m recording_transcript.windows_worker AUDIO MODEL", file=sys.stderr)
        return 2
    try:
        watch_parent()
        checkpoint = os.environ.get('AI_ASR_CHECKPOINT')
        with redirect_stdout(sys.stderr):
            result = _transcribe_in_process(Path(argv[1]), Path(argv[2]), Path(checkpoint) if checkpoint else None)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
