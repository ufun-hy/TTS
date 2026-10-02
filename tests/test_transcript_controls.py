"""Task pause/resume/restart/delete and worker termination regressions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recording_transcript.control import TaskControl, TaskInterrupted, read_json, write_json
from recording_transcript.jobs import TranscriptJobStore
from recording_transcript import qwen_asr_windows as backend
from recording_transcript.windows_chunks import AudioChunk
from local_runtime import session

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_controls', ROOT/'scripts/windows-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class JobControlTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='asr-control-tests-'))
        self.env = mock.patch.dict(os.environ, {'AI_LIVE_STUDIO_DATA': str(self.root/'data'), 'WINDOWS_SINGLE_MACHINE': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_pause_resume_restart_delete_preserves_checkpoint_and_original(self):
        store = TranscriptJobStore(self.root, self.root/'model')
        self.addCleanup(store.shutdown)
        upload = self.root/'upload'/'input.wav'
        upload.parent.mkdir()
        upload.write_bytes(b'audio')
        ready = threading.Event()

        def transcribe(*args, control, **kwargs):
            if not control.checkpoint.exists():
                write_json(control.checkpoint, {'completed_chunks': 1, 'total_chunks': 2, 'processed_seconds': 30, 'total_seconds': 60})
                control.report()
                ready.set()
                control.interrupted.wait(5)
                control.check()
            return '已恢复的文稿'

        with mock.patch.object(store, '_transcribe', side_effect=transcribe):
            job = store.create('录音.wav', 5, upload)
            self.assertTrue(ready.wait(2))
            store.action(job.job_id, 'pause')
            job.thread.join(3)
            self.assertEqual(job.stage, 'paused')
            self.assertEqual(job.progress['completed_chunks'], 1)
            self.assertTrue(job.upload_path.exists())
        store.shutdown()
        restored = TranscriptJobStore(self.root, self.root/'model')
        self.addCleanup(restored.shutdown)
        current = restored.get(job.job_id)
        self.assertEqual(current.stage, 'paused')
        with mock.patch.object(restored, '_transcribe', side_effect=transcribe):
            restored.action(job.job_id, 'resume')
            current.thread.join(3)
        self.assertEqual(current.stage, 'completed')
        self.assertEqual(current.text, '已恢复的文稿')
        restored.action(job.job_id, 'delete')
        self.assertIsNone(restored.get(job.job_id))
        archived = list((restored.directory.parent/'trash').glob(job.job_id+'-*'))
        self.assertEqual((archived[0]/'input.wav').read_bytes(), b'audio')
        self.assertTrue((archived[0]/'result.json').is_file())

    def test_control_interrupt_terminates_real_child(self):
        control = TaskControl(self.root/'work')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        control.interrupted.set()
        with self.assertRaises(TaskInterrupted):
            control.communicate(child)
        self.assertIsNotNone(child.poll())

    def test_shutdown_pauses_active_job(self):
        store = TranscriptJobStore(self.root, self.root/'model')
        self.addCleanup(store.shutdown)
        upload=self.root/'upload'/'input.wav'
        upload.parent.mkdir();upload.write_bytes(b'audio')
        ready=threading.Event()
        def transcribe(*args, control, **kwargs):
            ready.set();control.interrupted.wait(5);control.check()
        with mock.patch.object(store,'_transcribe',side_effect=transcribe):
            job=store.create('x.wav',5,upload)
            self.assertTrue(ready.wait(2))
            self.assertTrue(store.shutdown())
        self.assertEqual(job.stage,'paused')
        self.assertTrue(job.upload_path.exists())

    def test_checkpoint_skips_finished_chunk(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'fake')
        model=self.root/'model'
        chunks=[AudioChunk(self.root/'first.wav',0,30),AudioChunk(self.root/'second.wav',30,60)]
        checkpoint=self.root/'checkpoint.json'
        with mock.patch.object(backend,'validate_model',return_value=model), \
             mock.patch.object(backend,'readiness',return_value={'asr_ready':True}), \
             mock.patch.object(backend,'split_wav',return_value=chunks), \
             mock.patch.object(backend,'_load_model'), \
             mock.patch.object(backend,'_transcribe_one',side_effect=[{'text':'甲','segments':[{'text':'甲','start':0,'end':30}]},RuntimeError('interrupted')]):
            with self.assertRaises(RuntimeError):
                backend._transcribe_in_process(audio,model,checkpoint)
        self.assertEqual(read_json(checkpoint)['completed_chunks'],1)
        with mock.patch.object(backend,'validate_model',return_value=model), \
             mock.patch.object(backend,'readiness',return_value={'asr_ready':True}), \
             mock.patch.object(backend,'split_wav',return_value=chunks), \
             mock.patch.object(backend,'_load_model'), \
             mock.patch.object(backend,'_transcribe_one',return_value={'text':'乙','segments':[{'text':'乙','start':0,'end':30}]}) as run:
            result=backend._transcribe_in_process(audio,model,checkpoint)
        run.assert_called_once_with(chunks[1].path,model)
        self.assertEqual(result['text'],'甲乙')
        self.assertEqual(result['segments'][1]['start'],30)

    def test_reused_system_pid_never_stopped(self):
        record=runtime._record('ollama',6080,['C:/app/ollama.exe','serve'])
        with mock.patch.object(runtime,'_running',return_value=True), \
             mock.patch.object(runtime,'_listener_pids',return_value=[]), \
             mock.patch.object(runtime,'_process_info',return_value={'executable':'C:/Windows/system32/sihost.exe','command_line':'sihost.exe'}), \
             mock.patch.object(runtime.subprocess,'run') as run:
            self.assertTrue(runtime._inspect_process('ollama',record)['reused_pid'])
            self.assertEqual(runtime._stop_processes({'ollama':record}),{})
            run.assert_not_called()

    def test_last_browser_closure_stops_runtime_but_another_view_keeps_it(self):
        data=self.root/'session-data'
        (data/'runtime').mkdir(parents=True)
        write_json(data/'runtime/session.json',{'started':100})
        now=[101.0]
        with mock.patch.object(session.time,'time',side_effect=lambda:now[0]):
            session.view_event(data,'first-window',True)
            session.view_event(data,'second-window',False)
        def tick(_):
            now[0]+=3
            if now[0]>125:
                with mock.patch.object(session.time,'time',side_effect=lambda:now[0]):
                    session.view_event(data,'second-window',True)
        with mock.patch.object(session.time,'sleep',side_effect=tick), \
             mock.patch.object(session.time,'time',side_effect=lambda:now[0]), \
             mock.patch.object(session.subprocess,'run',return_value=mock.Mock(returncode=0)) as stop:
            session.guard(ROOT,data,100)
        self.assertGreaterEqual(now[0],140)
        stop.assert_called_once()
        self.assertIn('stop',stop.call_args.args[0])

    @unittest.skipUnless(os.name=='nt','Windows parent process handles')
    def test_worker_exits_when_parent_is_killed(self):
        from local_runtime.file_lease import _pid_alive
        child_code='from recording_transcript.windows_worker import watch_parent; import time; watch_parent(); print("ready",flush=True); time.sleep(60)'
        parent_code=("import subprocess,os,sys,time; env=os.environ.copy(); env['AI_ASR_PARENT_PID']=str(os.getpid()); "
                     "child=subprocess.Popen([sys.executable,'-c',"+repr(child_code)+"],env=env,stdout=subprocess.PIPE,text=True); "
                     "print(str(child.pid)+' '+child.stdout.readline().strip(),flush=True); time.sleep(60)")
        parent=subprocess.Popen([sys.executable,'-c',parent_code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            line=parent.stdout.readline().strip()
            self.assertTrue(line.endswith('ready'),line)
            pid=int(line.split()[0])
            parent.kill();parent.wait(timeout=5)
            deadline=time.monotonic()+5
            while _pid_alive(pid) and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse(_pid_alive(pid))
        finally:
            if parent.poll() is None:
                parent.kill();parent.wait(timeout=5)
            parent.stdout.close()
            parent.stderr.close()


if __name__=='__main__':
    unittest.main()
