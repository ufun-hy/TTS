"""One serial background worker for editable, persisted first-round audio plans."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
import time

from server.first_round_plan import build_plan, source_signature, project_inputs, normalized_source
from server.live_session import LiveSessionError, MAX_TTS_RECOVERY_RETRIES
from server.tts_client import RetryableTTSClientError


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
        self._projects = {}
        self._stream = None

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
            if activate and self._stream:
                raise LiveSessionError("直播正在使用音频准备，请使用直播暂停/继续，停止后可切换项目。")
            self._projects[identifier] = copy.deepcopy(project)
            old = self._load(identifier)
            if old is None and not activate:
                return self.status(identifier)
            try:
                if self._stream and self._stream.project_id == identifier:
                    project = self._stream.project_for_plan(project)
                plan = build_plan(project, self.fingerprint(project.get("voice", "default")), old)
                if self._stream and self._stream.project_id == identifier:
                    self._stream.validate_prefix(plan)
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

    def pause(self, project_id, *, from_live=False):
        with self._condition:
            if self._stream and not from_live and self._stream.project_id == project_id:
                raise LiveSessionError("请使用直播暂停/继续控制音频准备。")
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
                    "streaming": bool(self._stream and self._stream.project_id == project_id),
                    "can_stream": bool(blocks and self._ready(blocks[0]) and not plan.get("error") and not plan.get("issue")),
                    "voice": plan.get("voice"),
                    "total_segments": len(blocks), "complete": plan.get("complete", False),
                    "generated_units": plan.get("generated_units", 0), "total_units": plan.get("total_units", 0),
                    "error": plan.get("error") or plan.get("issue", ""), "enabled": plan.get("enabled", False)}

    def open_stream(self, project_id, voice):
        with self._condition:
            if self._stream:
                raise LiveSessionError("已有直播正在使用音频准备。")
            plan = self._load(project_id)
            if not plan or not self.status(project_id).get("can_stream"):
                raise LiveSessionError("请等待首个完整音频块准备好后开启语音传输。")
            if voice != plan["voice"] or self.fingerprint(voice) != plan["voice_fingerprint"]:
                raise LiveSessionError("音色已变化，请重新准备音频。")
            # Resume the same producer; Live only consumes its ready prefix.
            self.update(self._projects[project_id], activate=True)
            self._stream = PreparationStream(self, project_id)
            return self._stream

    def handoff(self, project_id, source, voice):
        """Accept only a complete plan matching the exact, revalidated Live request."""
        with self._condition:
            if self._stream:
                raise LiveSessionError("已有直播正在使用音频准备。")
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
                        if current["retry_count"] > MAX_TTS_RECOVERY_RETRIES:
                            current["error"] = str(error)
                            current["retry_at"] = 0
                        else:
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


class PreparationStream:
    """One live consumer of a preparation plan, with an immutable sent prefix."""

    def __init__(self, preparation, project_id):
        self.preparation = preparation
        self.project_id = project_id
        self.sent = []
        self.frozen_paragraphs = []
        self.frozen_groups = []
        self.closed = False
        self.voice = preparation._plans[project_id]["voice"]
        self.fingerprint = preparation._plans[project_id]["voice_fingerprint"]

    def project_for_plan(self, project):
        project = copy.deepcopy(project)
        if project.get("voice", "default") != self.voice:
            raise ValueError("直播音色已固定，请停止直播后更换音色。")
        count = len(self.frozen_paragraphs)
        if [p["id"] for p in project["paragraphs"][:count]] != [p["id"] for p in self.frozen_paragraphs]:
            raise ValueError("已发送的话术顺序已变化，请停止后重新开启传输。")
        project["paragraphs"][:count] = copy.deepcopy(self.frozen_paragraphs)
        if self.frozen_groups:
            groups = project.get("context_groups") or []
            if [g["id"] for g in groups[:len(self.frozen_groups)]] != [g["id"] for g in self.frozen_groups]:
                raise ValueError("已发送的上下文组已变化，请停止后重新开启传输。")
            groups[:len(self.frozen_groups)] = copy.deepcopy(self.frozen_groups)
        return project

    def validate_prefix(self, plan):
        if plan["voice_fingerprint"] != self.fingerprint:
            raise ValueError("直播期间音色资源已变化，请停止后重新准备。")
        keys = [b["audio_key"] for b in plan["blocks"][:len(self.sent)]]
        if keys != [b["audio_key"] for b in self.sent]:
            raise ValueError("已发送的音频前缀发生变化，请停止后重新开启传输。")

    def take(self):
        """Reserve the next ready block atomically with freezing its source units."""
        owner = self.preparation
        with owner._condition:
            plan = owner._plans[self.project_id]
            if owner._closed:
                raise RuntimeError("音频准备服务已关闭")
            if plan.get("error") or plan.get("issue"):
                raise RuntimeError(plan.get("error") or plan["issue"])
            self.validate_prefix(plan)
            progress = {"total": len(plan["blocks"]),
                        "ready": sum(owner._ready(b) for b in plan["blocks"])}
            if self.closed or not plan.get("enabled"):
                return None, False, progress
            if len(self.sent) == len(plan["blocks"]):
                return None, bool(plan["complete"]), progress
            block = plan["blocks"][len(self.sent)]
            if not owner._ready(block):
                return None, False, progress
            if owner.fingerprint(self.voice) != self.fingerprint:
                raise RuntimeError("直播期间音色资源已变化，请停止后重新准备。")
            project = self.project_for_plan(owner._projects[self.project_id])
            ids = {s["paragraph_id"] for s in block["sources"]}
            paragraphs = project["paragraphs"]
            end = max(i + 1 for i, p in enumerate(paragraphs) if p["id"] in ids)
            groups = project.get("context_groups")
            if groups is not None:
                group_end = max(i + 1 for i, g in enumerate(groups) if ids.intersection(g["paragraph_ids"]))
                self.frozen_groups = copy.deepcopy(groups[:group_end])
                group_ids = {i for g in self.frozen_groups for i in g["paragraph_ids"]}
                end = max(i + 1 for i, p in enumerate(paragraphs) if p["id"] in group_ids)
            self.frozen_paragraphs = copy.deepcopy(paragraphs[:end])
            self.sent.append(copy.deepcopy(block))
            return {**block, "path": owner.audio_path(block)}, False, progress

    def final_source(self):
        owner = self.preparation
        with owner._condition:
            plan = owner._plans[self.project_id]
            units, groups, complete, _ = project_inputs(owner._projects[self.project_id])
            if not complete or not plan["complete"] or len(self.sent) != len(plan["blocks"]):
                return None
            self.validate_prefix(plan)
            source = normalized_source(units, groups)
            self.close()
            return source, copy.deepcopy(plan)

    def pause(self):
        if not self.closed:
            self.preparation.pause(self.project_id, from_live=True)

    def resume(self):
        owner = self.preparation
        with owner._condition:
            if not self.closed:
                owner._plans[self.project_id]["enabled"] = True
                owner._save(owner._plans[self.project_id])
                owner._condition.notify_all()

    def close(self):
        owner = self.preparation
        with owner._condition:
            if self.closed:
                return
            self.pause()
            self.closed = True
            if owner._stream is self:
                owner._stream = None
            owner._condition.notify_all()
