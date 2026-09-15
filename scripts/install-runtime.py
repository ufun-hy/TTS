"""Expand large bundled runtime archives after Inno Setup copies the app."""

from __future__ import annotations

from pathlib import Path
import sys
import zipfile


def extract(archive: Path, destination: Path) -> None:
    if not archive.is_file():
        raise FileNotFoundError(archive)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)
    archive.unlink()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: install-runtime.py APP_DIR", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    try:
        extract(root / "runtime" / "packages" / "torch.whl", root / "runtime" / "python" / "Lib" / "site-packages")
        extract(root / "runtime" / "packages" / "ollama.zip", root / "runtime" / "bin" / "ollama")
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"bundled runtime extraction failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
