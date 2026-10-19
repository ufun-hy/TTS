"""Durable single-speaker recording analysis and reviewed voice drafts."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import wave

from local_runtime import RuntimeStage, RuntimeBusyError
from recording_transcript.control import TaskControl, TaskInterrupted, read_json, write_json
from recording_transcript.jobs import _worker_exit_confirmed
from voice_datasets.analysis import (MAX_BYTES, MAX_SECONDS, FORMATS, candidate_ranges,
                                    decode, quality, ranked_candidates, time_range)
from voice_datasets.models import asset_status, run_model

ACTIVE={'queued','decoding','vad','quality','emotion','transcript','preparing','preview','saving','pausing'}
TEST_TEXT='你好，欢迎来到直播间。这款商品有两种规格，价格是九十九元。请注意，购买前先确认尺寸和数量。'


class VoiceStudioJobs:
    def __init__(self, registration, models, runtime, stop_engine, preview):
        self.registration=registration
        self.models=Path(models)
        self.directory=registration.root.parent/'voice-studio/jobs'
        self.runtime=runtime;self.stop_engine=stop_engine;self.preview=preview
        self._lock=threading.RLock();self._jobs={};self._threads={};self._controls={};self._seen={}
        self._closing=threading.Event()
        for path in self.directory.glob('*/job.json'):
            value=read_json(path)
            if value.get('id')!=path.parent.name:continue
            if value.get('state') in ACTIVE:
                value['state']='paused';value['error']='上次处理已中断，可从检查点继续'
            self._jobs[value['id']]=value
        self._watcher=threading.Thread(target=self._watch,daemon=True,name='voice-studio-views')
        self._watcher.start()

    def readiness(self, verify=False):
        status=asset_status(self.models,verify)
        short=self.registration.readiness()
        status['missing']+=short['missing'];status['ready']=not status['missing']
        status.update(max_upload_bytes=MAX_BYTES,max_seconds=MAX_SECONDS)
        return status

    def _write(self,job):
        job['updated_at']=time.time()
        write_json(self.directory/job['id']/'job.json',job)

    def get(self,job_id):
        with self._lock:
            if job_id not in self._jobs:raise KeyError('任务不存在')
            result=copy.deepcopy(self._jobs[job_id])
            # Internal filesystem paths are never supplied by the browser.
            for c in result.get('candidates',[]):
                for field in ('draft_id','prompt'):c.pop(field,None)
            return result

    def list(self):
        with self._lock:
            return [{key:job.get(key) for key in ('id','name','filename','state','stage','updated_at','error')}
                    for job in sorted(self._jobs.values(),key=lambda x:x['updated_at'],reverse=True)]

    def create(self,stream,length,filename,name,start=0,end=None):
        if not isinstance(name,str) or not 1<=len(name.strip())<=30:raise ValueError('主播名称需为 1～30 字')
        suffix=Path(filename).suffix.lower()
        if suffix not in FORMATS:raise ValueError('仅支持 WAV、MP3、M4A、MP4')
        if not 0<length<=MAX_BYTES:raise ValueError('录音最大 2 GiB，且不能为空')
        start,selected_end=time_range(start,end,MAX_SECONDS)
        if not self.readiness()['ready']:raise ValueError('分析模型或运行文件缺失，请先检查模型状态')
        self.directory.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(self.directory).free<length+2*1024**3:raise ValueError('磁盘空间不足，上传后至少需保留 2 GiB')
        identifier=uuid.uuid4().hex;folder=self.directory/identifier;folder.mkdir()
        with (folder/('source'+suffix)).open('xb') as out:
            remaining=length
            while remaining:
                chunk=stream.read(min(remaining,1024*1024))
                if not chunk:raise ValueError('上传中断，已上传内容保留，请重新上传')
                out.write(chunk);remaining-=len(chunk)
        job={'id':identifier,'name':name.strip(),'filename':filename[:255],'suffix':suffix,
             'range_start':start,'range_end':None if end in (None,'') else selected_end,
             'state':'paused','stage':'uploaded','candidates':[],'pending':{'kind':'analyze'},'error':''}
        with self._lock:
            self._jobs[identifier]=job;self._seen[identifier]=time.monotonic();self._write(job)
            try:
                self._start(job)
            except RuntimeBusyError:
                job['notice']='运行时正忙，录音已保存，请空闲后点击继续'
                self._write(job)
        return self.get(identifier)

    def _start(self,job):
        if self._closing.is_set():raise ValueError('软件正在关闭')
        if self._threads.get(job['id']) and self._threads[job['id']].is_alive():raise ValueError('任务仍在处理，请稍候')
        # Fail before creating a worker if another runtime operation owns the GPU.
        lease=self.runtime.acquire(RuntimeStage.TTS_PREPARING,'tts-preview','voice-studio',confirm_release=self.stop_engine)
        control=TaskControl(self.directory/job['id']/'work')
        self._controls[job['id']]=control
        job['state']='queued';job['error']='';self._seen[job['id']]=time.monotonic();self._write(job)
        thread=threading.Thread(target=self._run,args=(job,control,lease),daemon=True,name='voice-studio-'+job['id'][:8])
        self._threads[job['id']]=thread
        thread.start()

    def action(self,job_id,action,body=None):
        body=body or {}
        with self._lock:
            if job_id not in self._jobs:raise KeyError('任务不存在')
            job=self._jobs[job_id]
            if action=='heartbeat':self._seen[job_id]=time.monotonic();return {'ok':True}
            if action=='pause':
                if job['state'] in ACTIVE:
                    self._controls[job_id].interrupted.set();job['state']='pausing';self._write(job)
                return self.get(job_id)
            if job['state'] in ACTIVE:raise ValueError('任务正在处理，请先暂停并等待结束')
            if action=='resume':
                if not job.get('pending'):raise ValueError('没有待恢复的任务')
            elif action=='more':
                available=[c['id'] for c in job['candidates'] if c['quality']['usable'] and not c.get('prepared')][:12]
                if not available:raise ValueError('没有更多候选')
                job['pending']={'kind':'prepare','ids':available}
            else:
                c=next((x for x in job['candidates'] if x['id']==body.get('candidate_id')),None)
                if c is None:raise ValueError('候选不存在')
                if body.get('version')!=c['version']:raise ValueError('候选已更新，请刷新后重试')
                if action=='edit':
                    if c.get('registered'):raise ValueError('已保存的音色不可在此修改，请选择其他候选')
                    start,end=time_range(body.get('start',c['start']),body.get('end',c['end']),job['duration'])
                    start,end=round(start,3),round(end,3)
                    if not 3<=end-start<=30:raise ValueError('参考片段须为 3～30 秒')
                    if start<job['range_start'] or end>job['range_end']:raise ValueError('片段超出任务分析范围')
                    variant=body.get('variant',c['variant'])
                    if variant not in ('original','denoised'):raise ValueError('参考版本无效')
                    style=body.get('style',c['style'])
                    text=body.get('text',c.get('text',''))
                    if not isinstance(style,str) or not 1<=len(style.strip())<=20:raise ValueError('风格名称需为 1～20 字')
                    if not isinstance(text,str) or len(text)>2000 or '<|' in text or '|>' in text or '\x00' in text:raise ValueError('录音原文无效')
                    changed=(start,end,variant)!=(c['start'],c['end'],c['variant'])
                    text_changed=text.strip()!=c.get('text','')
                    c.update(start=start,end=end,variant=variant,style=style.strip())
                    if changed or text_changed:
                        c['version']+=1;c['confirmed']=False;c.pop('preview',None);c.pop('draft_id',None);c.pop('prompt',None)
                    if changed:
                        c['prepared']=False;c['text']='';c['emotion']={'suggested':'待确认'}
                        job['pending']={'kind':'prepare','ids':[c['id']]}
                    else:
                        c['text']=text.strip()
                        c['confirmed']=bool(body.get('confirmed')) and bool(c['text'])
                        self._write(job);return self.get(job_id)
                elif action in ('preview','save'):
                    if not c.get('prepared') or not c.get('confirmed'):raise ValueError('请先核对说话人、边界和原文')
                    if action=='save':
                        if c.get('registered'):return self.get(job_id)
                        if not c.get('preview') or body.get('heard') is not True:raise ValueError('请先试听并确认声音效果')
                    test_text=body.get('test_text',TEST_TEXT)
                    if not isinstance(test_text,str) or not 1<=len(test_text.strip())<=200:raise ValueError('试听文案需为 1～200 字')
                    job['pending']={'kind':action,'candidate_id':c['id'],'test_text':test_text.strip()}
                else:raise ValueError('不支持的操作')
            self._write(job);self._start(job)
        return self.get(job_id)

    def _stage(self,job,state,**progress):
        with self._lock:
            self._controls[job['id']].check()
            job.update(state=state,stage=state,progress=progress);self._write(job)

    def _model(self,job,kind,payload,directory,control):
        def progress(_):
            result=read_json(directory/(kind+'.json'))
            if result:
                with self._lock:
                    job['progress']={key:result[key] for key in ('processed_seconds','total_seconds','completed','total') if key in result}
        old=control.on_progress;old_path=control.checkpoint
        control.on_progress=progress;control.checkpoint=directory/(kind+'.json')
        try:return run_model(kind,payload,directory,self.models,control)
        finally:control.on_progress=old;control.checkpoint=old_path

    def _analyze(self,job,control):
        folder=self.directory/job['id'];audio=folder/'analysis.wav'
        self._stage(job,'decoding')
        if not audio.exists():
            decode(self.registration.ffmpeg,folder/('source'+job['suffix']),audio,control)
        with wave.open(str(audio),'rb') as wav:duration=wav.getnframes()/wav.getframerate()
        if duration>MAX_SECONDS:raise ValueError('原录音超过 1 小时，请裁剪后重新上传')
        start,end=time_range(job['range_start'],job['range_end'],duration)
        with self._lock:
            job.update(duration=duration,range_end=end);self._write(job)
        if not job.get('analyzed'):
            self._stage(job,'vad')
            result=self._model(job,'vad',{'audio':str(audio)},folder/'vad',control)
            spans=result['spans']
            self._stage(job,'quality')
            if not job['candidates']:
                candidates=[]
                for index,(a,b,flags) in enumerate(candidate_ranges(spans,start,end)):
                    control.check();metrics=quality(audio,a,b,spans);metrics['flags']+=flags
                    candidates.append({'id':f'c{index:05d}','start':round(a,3),'end':round(b,3),'version':1,'variant':'denoised',
                                       'quality':metrics,'style':'待确认','confirmed':False,'prepared':False})
                with self._lock:job['candidates']=candidates;self._write(job)
            self._stage(job,'emotion')
            items=[]
            for c in job['candidates']:
                if not c['quality']['usable']:continue
                path=folder/'emotion-audio'/(c['id']+'.wav')
                if not path.exists():decode(self.registration.ffmpeg,audio,path,control,start=c['start'],duration=c['end']-c['start'])
                items.append({'id':c['id'],'audio':str(path)})
            if items:
                values=self._model(job,'emotion',{'items':items},folder/'emotions',control)['values']
                with self._lock:
                    for c in job['candidates']:
                        if c['id'] in values:
                            c.update(values[c['id']]);c['style']=c['emotion']['suggested']
                    ranked,count=ranked_candidates(job['candidates'])
                    rejected=[c for c in job['candidates'] if c not in ranked]
                    job['candidates']=ranked+rejected;job['recommended_ids']=[c['id'] for c in ranked[:count]]
            else:job['recommended_ids']=[]
            with self._lock:job['analyzed']=True;self._write(job)
        self._prepare(job,job['recommended_ids'],control)
        if not job['recommended_ids']:job['notice']='没有找到可推荐人声，请换一份单主播清晰录音或调整分析范围。'

    def _prepare(self,job,ids,control):
        folder=self.directory/job['id']
        for number,identifier in enumerate(ids):
            c=next(x for x in job['candidates'] if x['id']==identifier)
            if c.get('prepared'):continue
            self._stage(job,'preparing',completed=number,total=len(ids))
            directory=folder/c['id']/str(c['version'])
            for variant in ('original','denoised'):
                path=directory/(variant+'.wav')
                if not path.exists():
                    decode(self.registration.ffmpeg,folder/('source'+job['suffix']),path,control,
                           start=c['start'],duration=c['end']-c['start'],rate=24000,denoise=variant=='denoised')
            asr=directory/'asr.wav'
            if not asr.exists():decode(self.registration.ffmpeg,directory/(c['variant']+'.wav'),asr,control)
            self._stage(job,'transcript',completed=number,total=len(ids))
            values=self._model(job,'transcript',{'items':[{'id':identifier,'audio':str(asr)}]},directory/'asr',control)['values']
            with self._lock:
                c['text']=values[identifier];c['prepared']=True;c['confirmed']=False
                c['characters_per_second']=round(len(c['text'])/(c['end']-c['start']),2)
                self._write(job)

    def _draft(self,job,c,control):
        if c.get('prompt') and Path(c['prompt']).is_file():return Path(c['prompt'])
        draft_id=c.get('draft_id') or uuid.uuid4().hex
        folder=self.registration.root/draft_id
        folder.mkdir(parents=True,exist_ok=True)
        source=self.directory/job['id']/c['id']/str(c['version'])/(c['variant']+'.wav')
        shutil.copyfile(source,folder/'reference.wav')
        with self._lock:c['draft_id']=draft_id;self._write(job)
        prompt=self.registration.prepare(draft_id,c['text'],control)
        with self._lock:c['prompt']=str(prompt);self._write(job)
        return prompt

    def _run(self,job,control,lease):
        exited=True;engine_stopped=False;failure=None
        try:
            engine_stopped=self.stop_engine()
            if not engine_stopped:raise ValueError('旧 TTS 引擎未释放，请恢复运行时后重试')
            status=self.readiness(verify=True)
            if not status['ready']:raise ValueError('缺少或损坏模型：'+', '.join(status['missing']))
            with self._lock:job['model_versions']=status['model_versions'];self._write(job)
            action=copy.deepcopy(job['pending']);kind=action['kind']
            if kind=='analyze':self._analyze(job,control)
            elif kind=='prepare':self._prepare(job,action['ids'],control)
            else:
                c=next(x for x in job['candidates'] if x['id']==action['candidate_id'])
                self._stage(job,'preparing')
                prompt=self._draft(job,c,control)
                if kind=='preview':
                    self._stage(job,'preview');engine_stopped=False
                    audio=self.preview(prompt,action['test_text'])
                    control.check()
                    filename='preview-'+uuid.uuid4().hex+'.wav'
                    (self.directory/job['id']/c['id']/str(c['version'])/filename).write_bytes(audio)
                    with self._lock:c['preview']=filename;c['preview_text']=action['test_text'];self._write(job)
                else:
                    self._stage(job,'saving');control.check()
                    result=self.registration.register(c['draft_id'],job['name']+'·'+c['style'],c['text'],prepared_prompt=prompt,control=control)
                    with self._lock:c['registered']=result;self._write(job)
            with self._lock:job['pending']=None
        except TaskInterrupted:
            failure='paused'
        except Exception as exc:
            exited=_worker_exit_confirmed(exc);failure=str(exc)
        finally:
            # A synchronous preview can still be finishing on the gateway after a disconnect.
            if not engine_stopped:
                try:engine_stopped=self.stop_engine()
                except Exception:engine_stopped=False
            released=self.runtime.release(lease,'' if exited and engine_stopped else '音色任务进程未确认退出，保留资源锁')
            with self._lock:
                if not released:job['state']='failed';job['error']='资源释放失败，请在运行时恢复或重启软件后继续'
                elif failure=='paused':job['state']='paused';job['error']=''
                elif failure:job['state']='failed';job['error']=failure
                else:job['state']='ready';job['error']=''
                self._write(job)

    def media(self,job_id,candidate_id,version,variant):
        with self._lock:
            job=self._jobs.get(job_id)
            if job is None:raise KeyError('任务不存在')
            c=next((x for x in job['candidates'] if x['id']==candidate_id),None)
            if c is None or str(c['version'])!=str(version):raise ValueError('候选版本已变化')
            if variant in ('original','denoised'):name=variant+'.wav'
            elif variant=='preview' and c.get('preview'):name=c['preview']
            else:raise ValueError('音频尚未准备好')
            path=self.directory/job_id/candidate_id/str(c['version'])/name
            if not path.is_file():raise ValueError('音频尚未准备好')
            return path

    def _watch(self):
        while not self._closing.wait(2):
            with self._lock:
                for identifier,job in self._jobs.items():
                    if job['state'] in ACTIVE and time.monotonic()-self._seen.get(identifier,0)>25:
                        self.action(identifier,'pause')

    def shutdown(self):
        self._closing.set()
        with self._lock:
            for control in self._controls.values():control.interrupted.set()
            threads=list(self._threads.values())
        deadline=time.monotonic()+12
        for thread in threads:thread.join(max(0,deadline-time.monotonic()))
        return not any(t.is_alive() for t in threads)
