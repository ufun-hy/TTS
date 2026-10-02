"""Durable recording jobs, resumable controls and recoverable deletion."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
import time
import uuid

from local_runtime import FileGpuLease
from .control import TaskControl, TaskInterrupted, read_json, write_json
from .pipeline import TranscriptError, transcribe_recording
from .qwen_asr_windows import WorkerLifecycleError
from .results import _runtime_root, _result_path, load_result, list_results, save_result

ACTIVE = {'queued', 'decoding', 'loading', 'recognizing', 'cleaning', 'pausing', 'deleting'}


@dataclass
class TranscriptJob:
    job_id: str
    filename: str
    size: int
    upload_path: Path
    stage: str = 'queued'
    text: str = ''
    error: str = ''
    updated_at: float = 0.0
    progress: dict = field(default_factory=dict)
    control: TaskControl | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    last_seen: float = 0.0
    delete_requested: bool = False
    worker_exited: bool = True

    def public(self):
        value = {'job_id': self.job_id, 'filename': self.filename, 'size': self.size,
                 'stage': self.stage, 'updated_at': self.updated_at, 'progress': self.progress.copy()}
        if self.stage == 'completed':
            value['text'] = self.text
        if self.error:
            value['error'] = self.error
        return value


def _worker_exit_confirmed(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, WorkerLifecycleError):
            return bool(error.worker_exited)
        error = error.__cause__
    return True


class TranscriptJobStore:
    def __init__(self, project_root: Path, model: Path):
        self.project_root, self.model = project_root, model
        self.directory = _runtime_root(project_root) / 'recording-transcript' / 'jobs'
        self._jobs = {}
        self._lock = threading.RLock()
        self._asr_lock = threading.Lock()
        self._closing = threading.Event()
        lock_path = os.environ.get('AI_LIVE_STUDIO_GPU_LOCK', '') if os.environ.get('WINDOWS_SINGLE_MACHINE') == '1' else ''
        self._gpu_lease = FileGpuLease(lock_path, 'asr', 'Qwen3-ASR-1.7B', 'ASR') if lock_path else None
        for path in self.directory.glob('*/job.json'):
            value = read_json(path)
            if value.get('job_id') != path.parent.name:
                continue
            try:
                source = path.parent / ('input' + value['extension'])
                job = TranscriptJob(value['job_id'], value['filename'], value['size'], source,
                    stage=value['stage'], error=value.get('error', ''),
                    updated_at=value.get('updated_at', 0), progress=value.get('progress', {}))
                if job.stage in ACTIVE:
                    job.stage = 'paused'
                    job.error = '上次运行已中断，点击继续可从已保存片段恢复'
                if job.stage == 'completed':
                    job.text = load_result(project_root, job.job_id)['text']
                self._jobs[job.job_id] = job
                self._save(job)
            except (KeyError, TypeError, ValueError, OSError):
                continue
        self._watcher = threading.Thread(target=self._watch_views, daemon=True, name='transcript-views')
        self._watcher.start()

    def _save(self, job):
        job.updated_at = time.time()
        value = job.public()
        value.pop('text', None)
        value['extension'] = job.upload_path.suffix
        write_json(self.directory / job.job_id / 'job.json', value)

    def _transcribe(self, *args, **kwargs):
        return transcribe_recording(*args, **kwargs)

    def _spawn(self, job):
        job.control = TaskControl(self.directory / job.job_id / 'work', lambda value: self._progress(job, value))
        job.thread = threading.Thread(target=self._run, args=(job,), daemon=True, name='transcript-'+job.job_id[:8])
        job.thread.start()

    def create(self, filename, size, upload_path):
        with self._lock:
            if self._closing.is_set():
                raise ValueError('软件正在退出，请稍后重试')
            job_id = uuid.uuid4().hex
            target = self.directory / job_id / ('input'+upload_path.suffix)
            target.parent.mkdir(parents=True)
            os.replace(upload_path, target)
            try:
                upload_path.parent.rmdir()
            except OSError:
                pass
            job = TranscriptJob(job_id, filename, size, target, last_seen=time.monotonic())
            self._jobs[job_id] = job
            self._save(job)
            self._spawn(job)
            return job

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return job
            try:
                result = load_result(self.project_root, job_id)
            except FileNotFoundError:
                return None
            return TranscriptJob(job_id, result['filename'], result['size'], Path(),
                stage='completed', text=result['text'], updated_at=result['updated_at'])

    def list(self):
        with self._lock:
            jobs = {key: value.public() for key, value in self._jobs.items()}
            for result in list_results(self.project_root):
                if result['job_id'] not in jobs:
                    jobs[result['job_id']] = self.get(result['job_id']).public()
            for job in jobs.values():
                job.pop('text', None)
            return sorted(jobs.values(), key=lambda job: job['updated_at'], reverse=True)

    def _stage(self, job, stage):
        with self._lock:
            job.control.check()
            job.stage = stage
            self._save(job)

    def _progress(self, job, value):
        with self._lock:
            progress = {key: value.get(key, 0) for key in (
                'completed_chunks', 'total_chunks', 'processed_seconds', 'total_seconds')}
            if progress == job.progress and value.get('stage') == job.stage:
                return
            job.progress = progress
            if job.stage not in {'pausing', 'deleting'}:
                job.stage = value.get('stage', job.stage)
            self._save(job)

    def action(self, job_id, action):
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError('任务不存在')
            if action == 'heartbeat':
                job.last_seen = time.monotonic()
            elif action == 'pause':
                if job.stage in ACTIVE and job.stage != 'deleting':
                    job.stage = 'pausing'
                    job.control.interrupted.set()
                    self._save(job)
            elif action == 'resume':
                if not job.worker_exited:
                    raise ValueError('子进程退出未确认，请先退出并重启软件')
                if self._closing.is_set():
                    raise ValueError('软件正在退出')
                if job.stage not in {'paused', 'failed'} or (job.thread and job.thread.is_alive()):
                    raise ValueError('任务仍在停止或正在运行，请等待状态更新')
                if not job.upload_path.is_file():
                    raise ValueError('原录音不存在，请重新上传')
                job.error = ''
                job.stage = 'queued'
                job.last_seen = time.monotonic()
                self._save(job)
                self._spawn(job)
            elif action == 'delete':
                if not job.worker_exited:
                    raise ValueError('子进程退出未确认，保留任务数据，请先退出并重启软件')
                job.delete_requested = True
                if job.thread and job.thread.is_alive():
                    job.stage = 'deleting'
                    job.control.interrupted.set()
                    self._save(job)
                else:
                    self._archive(job)
            else:
                raise ValueError('不支持的任务操作')
            return job.public()

    def _archive(self, job):
        # User-requested delete is recoverable and never touches the original file.
        destination = self.directory.parent / 'trash' / (job.job_id+'-'+uuid.uuid4().hex[:8])
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.directory / job.job_id
        if source.is_dir():
            os.replace(source, destination)
        else:
            destination.mkdir()
        result = _result_path(self.project_root, job.job_id)
        if result.exists():
            os.replace(result, destination / 'result.json')
        job.stage = 'deleted'
        self._jobs.pop(job.job_id, None)

    def _run(self, job):
        if job.control is None:
            job.control = TaskControl(self.directory / job.job_id / 'work', lambda value: self._progress(job, value))
        locked = acquired = False
        worker_exited = True
        try:
            while not locked:
                job.control.check()
                locked = self._asr_lock.acquire(timeout=0.2)
            job.control.check()
            if self._gpu_lease:
                while not acquired:
                    job.control.check()
                    acquired = self._gpu_lease.acquire()
                    if not acquired:
                        job.control.interrupted.wait(0.25)
            text = self._transcribe(job.upload_path, self.project_root, self.model,
                on_stage=lambda stage: self._stage(job, stage), control=job.control)
            with self._lock:
                job.control.check()
                save_result(self.project_root, job.job_id, job.filename, job.size, text)
                job.text, job.stage, job.error = text, 'completed', ''
                self._save(job)
        except TaskInterrupted:
            with self._lock:
                job.stage = 'paused'
                self._save(job)
        except Exception as exc:
            worker_exited = _worker_exit_confirmed(exc)
            job.worker_exited = worker_exited
            with self._lock:
                job.stage, job.error = 'failed', str(exc)
                self._save(job)
        finally:
            if acquired and worker_exited:
                self._gpu_lease.release()
            if locked:
                self._asr_lock.release()
            if job.delete_requested and worker_exited:
                with self._lock:
                    self._archive(job)

    def _watch_views(self):
        while not self._closing.wait(2):
            with self._lock:
                for job in list(self._jobs.values()):
                    if job.stage in ACTIVE and job.last_seen and time.monotonic()-job.last_seen > 20:
                        self.action(job.job_id, 'pause')

    def shutdown(self):
        self._closing.set()
        with self._lock:
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.stage in ACTIVE:
                    self.action(job.job_id, 'pause')
        deadline = time.monotonic()+12
        for job in jobs:
            if job.thread:
                job.thread.join(max(0, deadline-time.monotonic()))
        return not any(job.thread and job.thread.is_alive() for job in jobs)
