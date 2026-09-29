"""One tracked application payload for both the installer and offline patches."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = {
    "audio_cache", "audio_client", "local_runtime", "recording_transcript",
    "server", "timeline", "voice_datasets", "web", "config", "scripts",
}
SOURCE_FILES = {"windows_client.py", "windows_launcher.py", "windows_playback_service.py"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compatibility():
    # Conservative: ANY build recipe/dependency change requires a full installer.
    h = hashlib.sha256()
    for name in (
        "build/windows/build.ps1", "build/windows/runtime-profile.json",
        "scripts/requirements-qwen-asr-windows.txt", "build/windows/python-constraints.txt",
    ):
        h.update(name.encode())
        h.update((ROOT / name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def package(destination, launcher):
    destination.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    selected = [p for p in files if p and (
        p.split("/")[0] in SOURCE_DIRS or p in SOURCE_FILES
    )]
    for name in selected:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copyfile(launcher, destination / "AI-Live-Studio.exe")
    selected.append("AI-Live-Studio.exe")
    manifest = {
        "schema": 1,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "runtime_id": compatibility(),
        "runtime_files": json.loads((ROOT / "build/windows/runtime-profile.json").read_text())["files"],
        "files": {name: digest(destination / name) for name in sorted(selected)},
    }
    (destination / "app-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--launcher", type=Path, default=ROOT / "dist/AI-Live-Studio.exe")
    args = parser.parse_args()
    package(args.destination, args.launcher)
