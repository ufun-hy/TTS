#!/usr/bin/env python3
"""Text Studio entrypoint with non-destructive UI extensions and script-restore API."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = (ROOT / "web" / "text-studio.html").resolve()
EXTENSION_PATHS = [
    ROOT / "web" / "text-studio-extension.js",
    ROOT / "web" / "text-studio-restore.js",
]
_ORIGINAL_READ_BYTES = Path.read_bytes


def _read_bytes_with_extension(path: Path) -> bytes:
    data = _ORIGINAL_READ_BYTES(path)
    try:
        if path.resolve() != HTML_PATH:
            return data
        scripts: list[str] = []
        for extension_path in EXTENSION_PATHS:
            if extension_path.is_file():
                scripts.append(_ORIGINAL_READ_BYTES(extension_path).decode("utf-8"))
        if not scripts:
            return data
        marker = b"</body>"
        if marker not in data:
            return data
        injected = ("\n<script>\n" + "\n".join(scripts) + "\n</script>\n").encode("utf-8")
        return data.replace(marker, injected + marker, 1)
    except Exception as exc:
        print(f"Text Studio extension injection skipped: {exc}", file=sys.stderr, flush=True)
        return data


Path.read_bytes = _read_bytes_with_extension

import text_studio  # noqa: E402
from script_restore import restore_script  # noqa: E402

_BASE_MAKE_HANDLER = text_studio.make_handler


def _extended_make_handler(*args: Any, **kwargs: Any):
    BaseHandler = _BASE_MAKE_HANDLER(*args, **kwargs)

    class ExtendedHandler(BaseHandler):
        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/api/restore/analyze":
                return super().do_POST()
            try:
                body = self._read_json()
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text is required")
                result = restore_script(text)
                self._json(200, result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": str(exc)})

    return ExtendedHandler


text_studio.make_handler = _extended_make_handler


if __name__ == "__main__":
    raise SystemExit(text_studio.main())
