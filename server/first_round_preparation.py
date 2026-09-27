"""One serial background worker for editable, persisted first-round audio plans."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
import time

from server.first_round_plan import build_plan, source_signature
from timeline.tts_client import RetryableTTSClientError


class FirstRoundPreparation:
    def __init__(self, root: Path, synthesize, voice_fingerprint):
        self.root = root / "runtime" / "text-studio" / "audio-preparation"
        self.synthesize = synthesize
        self.fingerprint = voice_fingerprint
        self._condition = threading.Condition(threading.RLock())
        self._plans = {}
        self._active = None
        self._working = None
        self._thread = None
        self._closed = False

    def _path(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", project_id):
            raise ValueError("invalid project_id")
        return self.root / project_id / "plan.json"

    def _load(self, project_id):
        path = self._path(project_id)
        if project_id not in self._plans and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            value["enabled"] = False  # Restart never silently starts GPU work.
            value["retry_at"] = 0
            self._plans[project_id] = value
        return self._plans.get(project_id)

    def _save(self, plan):
        path = self._path(plan["project_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def audio_path(self, block):
        return self.root / "audio" / (block["audio_key"] + ".wav")

    def _ready(self, block):
        try:
            with self.audio_path(block).open("rb") as f:
                return f.read(4) == b"RIFF"
        except OSError:
            return False

    def update(self, project, activate=False):
        identifier = project["project_id"]
        with self._condition:
            old = self._load(identifier)
            if old is None and not activate:
                return self.status(identifier)
            try:
                plan = build_plan(project, self.fingerprint(project.get("voice", "default")), old)
                plan["error"] = ""
            except (ValueError, OSError, TypeError, KeyError) as exc:
                # Saving text must still succeed; suppress stale audio on errors.
                plan = {**(old or {}), "project_id": identifier, "blocks": [],
                        "source_signature": "", "error": str(exc), "complete": False}
            plan["enabled"] = activate or bool(old and old.get("enabled"))
            plan["retry_at"] = 0
            plan["retry_count"] = 0
            if activate:
                if self._active and self._active != identifier:
                    previous = self._plans[self._active]
                    previous["enabled"] = False
                    self._save(previous)
                self._active = identifier
            self._plans[identifier] = plan
            self._save(plan)
            if activate and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="first-round-preparation", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return self.status(identifier)

    def pause(self, project_id):
        with self._condition:
            plan = self._load(project_id)
            if plan:
                plan["enabled"] = False
                self._save(plan)
            self._condition.notify_all()
            return self.status(project_id)

    def pause_active(self):
        with self._condition:
            if self._active:
                self.pause(self._active)

    def status(self, project_id):
        with self._condition:
            plan = self._load(project_id)
            if not plan:
                return {"project_id": project_id, "status": "idle", "prepared_segments": 0, "total_segments": 0}
            blocks = plan["blocks"]
            count = sum(self._ready(b) for b in blocks)
            if plan.get("error") or plan.get("issue"):
                state = "failed"
            elif plan.get("complete") and count == len(blocks) and blocks:
                state = "ready"
            elif not plan.get("enabled"):
                state = "paused"
            elif count < len(blocks):
                state = "preparing"
            else:
                state = "waiting_text"
            return {"project_id": project_id, "status": state, "prepared_segments": count,
                    "total_segments": len(blocks), "complete": plan.get("complete", False),
                    "generated_units": plan.get("generated_units", 0), "total_units": plan.get("total_units", 0),
                    "error": plan.get("error") or plan.get("issue", ""), "enabled": plan.get("enabled", False)}

    def handoff(self, project_id, source, voice):
        """Accept only a complete plan matching the exact, revalidated Live request."""
        with self._condition:
            plan = self._load(project_id)
            if not plan or not plan.get("complete") or plan.get("error") or plan.get("issue"):
                return None
            fingerprint = self.fingerprint(voice)
            if source_signature(source, voice, fingerprint) != plan.get("source_signature"):
                return None
            plan["enabled"] = False
            self._save(plan)
            result = copy.deepcopy(plan)
            result["audio_paths"] = {b["text"]: self.audio_path(b) for b in plan["blocks"] if self._ready(b)}
            self._condition.notify_all()
            return result

    def _run(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                plan = self._plans.get(self._active)
                pending = []
                if plan and plan.get("enabled") and not plan.get("error") and not plan.get("issue"):
                    pending = [b for b in plan["blocks"] if not self._ready(b)]
                if not pending or plan.get("retry_at", 0) > time.monotonic():
                    self._condition.wait(1)
                    continue
                block = dict(pending[0])
                identifier, voice = plan["project_id"], plan["voice"]
                fingerprint = plan["voice_fingerprint"]
                self._working = (identifier, block["audio_key"])
            error = None
            try:
                if self.fingerprint(voice) != fingerprint:
                    raise ValueError("音色资源已变化，请点击继续准备以更新音频。")
                audio = self.synthesize(block["text"], voice)
                if not isinstance(audio, bytes) or not audio.startswith(b"RIFF"):
                    raise ValueError("TTS 未返回 WAV 音频")
                if self.fingerprint(voice) != fingerprint:
                    raise ValueError("合成期间音色资源发生变化，请继续准备。")
                path = self.audio_path(block)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(audio)
                temporary.replace(path)
            except Exception as exc:
                error = exc
            with self._condition:
                self._working = None
                current = self._plans.get(identifier)
                if current and any(b["audio_key"] == block["audio_key"] for b in current["blocks"]):
                    if isinstance(error, RetryableTTSClientError):
                        current["retry_count"] = current.get("retry_count", 0) + 1
                        current["retry_at"] = time.monotonic() + error.retry_delay(current["retry_count"])
                    elif error:
                        current["error"] = str(error)
                    else:
                        current["retry_count"] = 0
                        current["retry_at"] = 0
                    self._save(current)
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
