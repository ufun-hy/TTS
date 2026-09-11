#!/usr/bin/env python3
"""Text Studio entrypoint with non-destructive UI extensions injected at response time."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = (ROOT / "web" / "text-studio.html").resolve()
EXTENSION_PATH = ROOT / "web" / "text-studio-extension.js"
_ORIGINAL_READ_BYTES = Path.read_bytes


def _read_bytes_with_extension(path: Path) -> bytes:
    data = _ORIGINAL_READ_BYTES(path)
    try:
        if path.resolve() != HTML_PATH or not EXTENSION_PATH.is_file():
            return data
        js = _ORIGINAL_READ_BYTES(EXTENSION_PATH).decode("utf-8")
        marker = b"</body>"
        if marker not in data:
            return data
        injected = ("\n<script>\n" + js + "\n</script>\n").encode("utf-8")
        return data.replace(marker, injected + marker, 1)
    except Exception as exc:
        print(f"Text Studio extension injection skipped: {exc}", file=sys.stderr, flush=True)
        return data


Path.read_bytes = _read_bytes_with_extension

import text_studio  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(text_studio.main())
