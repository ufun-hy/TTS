"""Offline application updates; no dependency installs, downloads or model writes.

Also shipped as an independent patch tool, so replacing the installed source
cannot change the code performing this transaction. Uses only the stdlib.
"""

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

APP_DIRS = {"audio_cache", "audio_client", "local_runtime", "recording_transcript",
            "server", "timeline", "voice_datasets", "web", "config", "scripts"}
APP_FILES = {"windows_client.py", "windows_launcher.py", "windows_playback_service.py",
             "AI-Live-Studio.exe", "app-manifest.json"}


class PatchError(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def target(root, name, runtime=False):
    parts = PurePosixPath(name).parts
    if (not parts or "\\" in name or PurePosixPath(name).is_absolute()
            or any(p in {".", ".."} or p.endswith((".", " ")) or ":" in p
                   or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", p, re.I)
                   for p in parts)
            or "/".join(parts) != name):
        raise PatchError(f"Unsafe file path: {name}")
    if runtime:
        allowed = parts[0] == "runtime" and len(parts) > 2
    else:
        allowed = name in APP_FILES or parts[0] in APP_DIRS and len(parts) > 1
    if not allowed:
        raise PatchError(f"Not an application file: {name}")
    result = root.joinpath(*parts)
    for item in (result, *result.parents):
        if item == root:
            break
        attributes = getattr(item.lstat(), "st_file_attributes", 0) if item.exists() else 0
        if item.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise PatchError(f"Linked update target is not allowed: {item}")
    if not result.resolve().is_relative_to(root.resolve()):
        raise PatchError(f"File escapes application directory: {name}")
    return result


def replace(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".patch-", delete=False) as out:
        with source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
        temporary = Path(out.name)
    os.replace(temporary, destination)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".journal-", delete=False) as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
        temporary = out.name
    os.replace(temporary, path)


def manifest(root):
    value = read_json(root / "app-manifest.json")
    if (value.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{40}", value.get("commit", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("runtime_id", ""))
            or not value.get("files") or not value.get("runtime_files")):
        raise PatchError("Invalid application manifest; install the new full installer first.")
    seen = set()
    for name, checksum in value["files"].items():
        target(root, name)
        if name == "app-manifest.json" or name.casefold() in seen or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise PatchError(f"Invalid manifest entry: {name}")
        seen.add(name.casefold())
    return value


def verify(root, files, runtime=False):
    for name, expected in files.items():
        path = target(root, name, runtime=runtime)
        if not path.is_file() or digest(path) != expected:
            raise PatchError(f"File missing or modified: {path}")


class Runtime:
    def __init__(self, app, data):
        self.app, self.data = app, data

    def command(self, action):
        completed = subprocess.run(
            [str(self.app / "runtime/python/python.exe"), str(self.app / "scripts/windows-runtime.py"),
             action, "--data", str(self.data)], cwd=self.app,
            capture_output=True, encoding="utf-8", errors="replace", timeout=240,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if completed.returncode:
            raise PatchError(f"Runtime {action} failed: {completed.stdout}\n{completed.stderr}")
        return completed.stdout

    def stop(self):
        self.command("stop")  # Existing ownership checks; never kill unknown listeners.

    def start(self):
        self.command("start")
        deadline = time.monotonic() + 180
        detail = {}
        while time.monotonic() < deadline:
            detail = json.loads(self.command("status"))
            health, processes = detail.get("health", {}), detail.get("processes", {})
            if (all(health.get(k) is True for k in ("ollama", "tts", "cache", "studio", "asr"))
                    and processes and all(p.get("owned") and p.get("running") for p in processes.values())):
                return
            time.sleep(2)
        raise PatchError(f"Runtime health/ownership verification failed: {json.dumps(detail, ensure_ascii=False)}")


def rollback(backup, app, runtime):
    journal = read_json(backup / "transaction.json")
    if journal["app"] != str(app) or journal["state"] == "rolled_back":
        raise PatchError("Backup belongs to another installation or has already been restored.")
    old, new = journal["old"], journal["new"]
    # Never restore a tampered backup or overwrite edits made since this update.
    verify(backup / "files", {n: h for n, h in old.items() if h is not None})
    for name in old:
        path = target(app, name)
        current = digest(path) if path.is_file() else None
        if current not in (old[name], new.get(name)):
            raise PatchError(f"Changed after update; refusing rollback: {path}")
    runtime.stop()
    journal["state"] = "restoring"
    write_json(backup / "transaction.json", journal)
    for name, checksum in old.items():
        path = target(app, name)
        if checksum is not None:
            replace(target(backup / "files", name), path)
        elif path.exists():
            displaced = target(backup / "displaced", name)
            displaced.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, displaced)  # Retain newly added files rather than deleting them.
    journal["state"] = "rolled_back"
    write_json(backup / "transaction.json", journal)


def apply(package, app, data, runtime):
    for path in (data / "updates").glob("*/transaction.json"):
        if read_json(path).get("state") not in {"committed", "rolled_back"}:
            raise PatchError(f"Unfinished update found. Restore its backup first: {path.parent}")
    if not (app / "app-manifest.json").is_file():
        raise PatchError("Legacy installation has no version baseline. Use the new full installer first.")
    previous, incoming = manifest(app), manifest(package)
    if previous["runtime_id"] != incoming["runtime_id"] or previous["runtime_files"] != incoming["runtime_files"]:
        raise PatchError("Runtime dependencies differ. Use the full installer, not this patch.")
    verify(app, incoming["runtime_files"], runtime=True)
    verify(app, previous["files"])
    verify(package, incoming["files"])
    names = sorted(set(previous["files"]) | set(incoming["files"]) | {"app-manifest.json"})
    for name in set(incoming["files"]) - set(previous["files"]):
        if target(app, name).exists():
            raise PatchError(f"Unmanaged file would be overwritten: {name}")
    backup = data / "updates" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    old = {}
    for name in names:
        source = target(app, name)
        old[name] = digest(source) if source.is_file() else None
        if old[name] is not None:
            destination = target(backup / "files", name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    new = {**incoming["files"], "app-manifest.json": digest(package / "app-manifest.json")}
    journal = {"app": str(app), "from": previous["commit"], "to": incoming["commit"],
               "state": "prepared", "old": old, "new": new}
    write_json(backup / "transaction.json", journal)
    print(f"Backup / recovery journal: {backup}", flush=True)
    try:
        runtime.stop()  # If ownership is uncertain, no application file has changed yet.
    except Exception:
        journal["state"] = "rolled_back"
        write_json(backup / "transaction.json", journal)
        raise
    try:
        journal["state"] = "applying"
        write_json(backup / "transaction.json", journal)
        for name in names:
            destination = target(app, name)
            if name in new:
                replace(target(package, name), destination)
            elif destination.exists():
                retired = target(backup / "retired", name)
                retired.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, retired)
        verify(app, new)
        # Timestamp/size based .pyc files can survive same-second updates and
        # rollbacks. Checked hashes validate the actual source on every import.
        for name in incoming["files"]:
            if name.endswith(".py"):
                source = target(app, name)
                cache_name = Path(importlib.util.cache_from_source(str(source))).relative_to(app).as_posix()
                cache = target(app, cache_name)
                py_compile.compile(str(source), cfile=str(cache), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        runtime.start()
    except Exception as exc:
        try:
            rollback(backup, app, runtime)
        except Exception as recovery:
            raise PatchError(f"Update failed: {exc}\nRecovery incomplete: {recovery}\nKeep backup: {backup}") from exc
        raise PatchError(f"Update failed; old files restored, runtime left stopped: {exc}\nBackup: {backup}") from exc
    journal["state"] = "committed"
    write_json(backup / "transaction.json", journal)
    return backup


@contextmanager
def update_lock(data):
    directory = data / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "patch.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield  # Closing the handle releases the lock, including after a crash.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "rollback"))
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    app, data = args.app.resolve(), args.data.resolve()
    try:
        if app == data or data.is_relative_to(app) or app.is_relative_to(data):
            raise PatchError("Application and user data must be separate directories.")
        with update_lock(data):
            runtime = Runtime(app, data)
            if args.action == "apply":
                if not args.package:
                    raise PatchError("--package is required")
                backup = apply(args.package.resolve(), app, data, runtime)
                print(f"PASS: application updated and healthy. Backup: {backup}")
            else:
                if not args.backup or not args.backup.resolve().is_relative_to(data / "updates"):
                    raise PatchError("--backup must point to a backup under this user's data/updates")
                rollback(args.backup.resolve(), app, runtime)
                print("PASS: previous files restored. Double-click AI Live Studio to start.")
        return 0
    except (OSError, ValueError, KeyError, PatchError, subprocess.SubprocessError) as exc:
        print(f"UPDATE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
