"""Long-recording boundaries, durable review and draft isolation regressions."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave

from local_runtime import RuntimeManager, RuntimeStage, RuntimeBusyError
from recording_transcript.control import TaskInterrupted
from voice_datasets import analysis
from voice_datasets.registration import VoiceRegistration
from voice_datasets.studio_jobs import VoiceStudioJobs
from server.voice_draft import synthesize_draft


def wav(path,seconds=12,rate=16000,amplitude=2000):
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,rate,0,'NONE','not compressed'))
        frame=int(amplitude).to_bytes(2,'little',signed=True)
        for _ in range(int(seconds)):
            audio.writeframes(frame*rate)


class AnalysisTests(unittest.TestCase):
    def test_ranges_use_pauses_and_keep_long_continuous_speech_marked(self):
        ranges=analysis.candidate_ranges([[0,5],[5.5,12],[20,65]],0,70)
        self.assertTrue(all(3<=b-a<=30 for a,b,_ in ranges))
        self.assertLess(ranges[0][1],13)
        self.assertTrue(any('连续讲话' in ' '.join(flags) for _,_,flags in ranges))
        self.assertEqual(analysis.candidate_ranges([],0,60),[])
        for start,end in [('nan',20),(20,10),(0,3601)]:
            with self.assertRaises(ValueError):analysis.time_range(start,end,3600)

    def test_unknown_emotion_and_diverse_ranking(self):
        self.assertEqual(analysis.emotion_summary(['neutral','happy'],[.55,.45])['suggested'],'待确认')
        self.assertEqual(analysis.emotion_summary(['neutral','happy'],[.8,.2])['suggested'],'自然')
        self.assertEqual(analysis.emotion_summary(['happy','neutral'],[.8,.2])['suggested'],'开心')
        candidates=[{'id':str(i),'start':i*20,'end':i*20+12,'quality':{'usable':True,'score':100-i},
                     'emotion':{'suggested':'自然' if i<7 else '开心'}} for i in range(10)]
        candidates.append({**candidates[0],'id':'duplicate','start':.1})
        ranked,count=analysis.ranked_candidates(candidates)
        self.assertEqual(count,6)
        self.assertEqual(len(ranked),10)
        self.assertEqual(len([c for c in ranked[:count] if c['emotion']['suggested']=='自然']),3)

    def test_silence_rejected_and_clipping_reported(self):
        root=Path(tempfile.mkdtemp(prefix='voice-quality-test-'));path=root/'a.wav'
        wav(path,amplitude=0)
        self.assertFalse(analysis.quality(path,0,12,[])['usable'])
        wav(path,amplitude=32767)
        self.assertIn('削波',''.join(analysis.quality(path,0,12,[[0,12]])['flags']))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='voice-studio-test-'))
        self.service=VoiceRegistration(self.root/'data',self.root/'models',self.root/'bin')
        self.service.tts.mkdir(parents=True)
        self.service.config.write_text('{"default":{"prompt_speech":"default.gguf"}}')
        self.runtime=RuntimeManager()
        self.preview=mock.Mock(return_value=b'RIFFtest')
        self.jobs=VoiceStudioJobs(self.service,self.root/'models',self.runtime,lambda:True,self.preview)
        self.addCleanup(self.jobs.shutdown)
        self.ready=mock.patch.object(self.jobs,'readiness',return_value={'ready':True,'missing':[],'model_versions':{'test':'1'}})
        self.ready.start();self.addCleanup(self.ready.stop)
        self.decode=mock.patch('voice_datasets.studio_jobs.decode',side_effect=self.fake_decode)
        self.decode.start();self.addCleanup(self.decode.stop)
        self.model=mock.patch('voice_datasets.studio_jobs.run_model',side_effect=self.fake_model)
        self.model.start();self.addCleanup(self.model.stop)

    def fake_decode(self,ffmpeg,source,destination,control,**kwargs):
        control.check();wav(destination,seconds=12 if 'duration' not in kwargs or kwargs['duration']>3600 else int(kwargs['duration']),rate=kwargs.get('rate',16000));return 12

    def fake_model(self,kind,payload,directory,models,control):
        control.check()
        if kind=='vad':return {'spans':[[.5,11.5]],'complete':True}
        if kind=='emotion':return {'values':{x['id']:{'emotion':{'suggested':'自然','score':.9,'label':'neutral'}} for x in payload['items']},'complete':True}
        return {'values':{x['id']:'大家好，大家好。' for x in payload['items']},'complete':True}

    def wait(self,identifier):
        self.jobs._threads[identifier].join(5)
        self.assertFalse(self.jobs._threads[identifier].is_alive())
        return self.jobs.get(identifier)

    def create(self):
        result=self.jobs.create(io.BytesIO(b'audio'),5,'test.wav','主播')
        job=self.wait(result['id'])
        self.assertEqual(job['state'],'ready',job.get('error'))
        return job

    def test_limits_and_missing_models(self):
        for length in (0,analysis.MAX_BYTES+1):
            with self.assertRaises(ValueError):self.jobs.create(io.BytesIO(b''),length,'x.wav','人')
        with mock.patch.object(self.jobs,'readiness',return_value={'ready':False}):
            with self.assertRaisesRegex(ValueError,'缺失'):self.jobs.create(io.BytesIO(b'hi'),2,'x.wav','人')
        with self.assertRaisesRegex(ValueError,'上传中断'):self.jobs.create(io.BytesIO(b'a'),2,'x.wav','人')
        with mock.patch('voice_datasets.studio_jobs.shutil.disk_usage',return_value=mock.Mock(free=1)):
            with self.assertRaisesRegex(ValueError,'空间不足'):self.jobs.create(io.BytesIO(b'a'),1,'x.wav','人')

    def test_no_speech_and_over_hour_never_recommend(self):
        with mock.patch('voice_datasets.studio_jobs.run_model',return_value={'spans':[],'complete':True}):
            job=self.create()
        self.assertEqual(job['candidates'],[])
        self.assertIn('没有',job['notice'])
        def long_decode(*args,**kwargs):wav(args[2],seconds=3601,rate=100);return 3601
        with mock.patch('voice_datasets.studio_jobs.decode',side_effect=long_decode):
            created=self.jobs.create(io.BytesIO(b'a'),1,'x.wav','人');job=self.wait(created['id'])
        self.assertEqual(job['state'],'failed');self.assertIn('1 小时',job['error'])

    def test_edit_retranscribes_invalidates_review_and_prevents_stale_updates(self):
        job=self.create();c=job['candidates'][0]
        body={'candidate_id':c['id'],'version':c['version'],'confirmed':True}
        updated=self.jobs.action(job['id'],'edit',body)
        self.assertTrue(updated['candidates'][0]['confirmed'])
        self.jobs.action(job['id'],'edit',{**body,'variant':'original'})
        edited=self.wait(job['id'])['candidates'][0]
        self.assertEqual(edited['variant'],'original');self.assertFalse(edited['confirmed'])
        self.assertEqual(edited['text'],'大家好，大家好。')
        self.assertEqual(edited['version'],2)
        with self.assertRaisesRegex(ValueError,'已更新'):self.jobs.action(job['id'],'edit',body)
        with self.assertRaisesRegex(ValueError,'核对'):self.jobs.action(job['id'],'preview',{'candidate_id':c['id'],'version':2})

    def test_busy_upload_preserved_and_resume_after_restart(self):
        lease=self.runtime.acquire(RuntimeStage.LIVE,'live-session')
        job=self.jobs.create(io.BytesIO(b'audio'),5,'test.wav','主播')
        self.assertEqual(job['state'],'paused')
        self.assertTrue((self.jobs.directory/job['id']/'source.wav').exists())
        self.runtime.release(lease)
        self.jobs.action(job['id'],'resume');job=self.wait(job['id'])
        self.assertEqual(job['state'],'ready')
        self.jobs.shutdown()
        restored=VoiceStudioJobs(self.service,self.root/'models',RuntimeManager(),lambda:True,self.preview)
        self.addCleanup(restored.shutdown)
        self.assertEqual(restored.get(job['id'])['candidates'],job['candidates'])

    def test_pause_retains_checkpoint_and_user_fields(self):
        started=threading.Event()
        def model(kind,payload,directory,models,control):
            if kind=='emotion':started.set();control.interrupted.wait(3);control.check()
            return self.fake_model(kind,payload,directory,models,control)
        with mock.patch('voice_datasets.studio_jobs.run_model',side_effect=model):
            job=self.jobs.create(io.BytesIO(b'a'),1,'x.wav','主播')
            self.assertTrue(started.wait(2));self.jobs.action(job['id'],'pause');paused=self.wait(job['id'])
        self.assertEqual(paused['state'],'paused');self.assertTrue(paused['candidates'])
        self.jobs.action(job['id'],'resume');job=self.wait(job['id'])
        self.assertEqual(job['state'],'ready')

    def test_preview_is_draft_and_save_is_idempotent(self):
        job=self.create();c=job['candidates'][0];body={'candidate_id':c['id'],'version':1,'confirmed':True}
        self.jobs.action(job['id'],'edit',body)
        before=self.service.config.read_bytes()
        def frontend(draft_id,text,control):
            path=self.service.root/draft_id/'prompt-test.gguf';path.write_bytes(b'GGUF'+b'0'*24);return path
        with mock.patch.object(self.service,'prepare',side_effect=frontend):
            self.jobs.action(job['id'],'preview',body);job=self.wait(job['id'])
        self.assertEqual(job['state'],'ready',job['error'])
        self.assertEqual(self.service.config.read_bytes(),before)
        self.assertTrue(job['candidates'][0]['preview'])
        with self.assertRaisesRegex(ValueError,'试听'):self.jobs.action(job['id'],'save',body)
        with mock.patch.object(self.service,'readiness',return_value={'ready':True,'missing':[]}):
            self.jobs.action(job['id'],'save',{**body,'heard':True});saved=self.wait(job['id'])
        self.assertEqual(saved['state'],'ready',saved['error'])
        config=json.loads(self.service.config.read_text());self.assertEqual(len(config),2)
        again=self.jobs.action(job['id'],'save',{**body,'heard':True})
        self.assertEqual(again['candidates'][0]['registered'],saved['candidates'][0]['registered'])
        self.assertEqual(self.service.config.read_bytes(),json.dumps(config,ensure_ascii=False,indent=2).encode()+b'\n')


class DraftTests(unittest.TestCase):
    def test_path_restriction_and_re_registration_after_cold_start(self):
        root=Path(tempfile.mkdtemp(prefix='voice-draft-test-'));path=root/'voice-recordings'/'a'/'prompt.gguf'
        path.parent.mkdir(parents=True);path.write_bytes(b'GGUF'+b'0'*24)
        engine=mock.Mock();engine.run.side_effect=lambda callback:callback(True)
        synth=mock.Mock(return_value=b'audio')
        with mock.patch('server.voice_draft.urllib.request.urlopen') as request:
            for _ in range(2):self.assertEqual(synthesize_draft({'text':'你好','prompt_path':str(path)},root,engine,'http://engine',synth),b'audio')
            self.assertEqual(request.call_count,2)
        with self.assertRaises(ValueError):synthesize_draft({'text':'你好','prompt_path':'/outside.gguf'},root,engine,'http://engine',synth)
        self.assertFalse((root/'voices.json').exists())


if __name__=='__main__':unittest.main()
