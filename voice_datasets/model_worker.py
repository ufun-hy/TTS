"""One model stage per process. No runtime downloads, no persistent CUDA model."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import wave

from recording_transcript.control import read_json, write_json
from voice_datasets.analysis import emotion_summary, pcm_samples


def deny_network(*args, **kwargs):
    raise RuntimeError('音色分析仅允许离线模型；请安装完整模型包')


def vad(payload, models, output):
    import torch
    torch.set_num_threads(1)
    model=torch.jit.load(str(models/'voice-analysis/silero_vad.jit'),map_location='cpu').eval()
    source=Path(payload['audio'])
    saved=read_json(output)
    spans=saved.get('spans',[])
    cursor=saved.get('processed_seconds',0)
    with wave.open(str(source),'rb') as audio:
        rate=audio.getframerate();total=audio.getnframes()/rate
        if rate!=16000 or audio.getsampwidth()!=2 or audio.getnchannels()!=1:
            raise ValueError('VAD requires 16kHz mono PCM16')
        # Each minute has context; only its core owns spans, then seams merge.
        while cursor<total:
            left=max(0,cursor-1);right=min(total,cursor+61)
            audio.setpos(round(left*rate));model.reset_states()
            position=left;active=None;quiet=None;window=[]
            with torch.inference_mode():
                while position<right:
                    count=min(512,round((right-position)*rate))
                    if count<=0:break
                    values=pcm_samples(audio,count)
                    tensor=torch.tensor(values,dtype=torch.float32)/32768
                    tensor=torch.nn.functional.pad(tensor,(0,512-len(tensor)))
                    probability=float(model(tensor,rate).item())
                    if probability>=.5:
                        if active is None:active=position
                        quiet=None
                    elif active is not None and probability<.35:
                        quiet=position if quiet is None else quiet
                        if position-quiet>=.35:
                            if quiet-active>=.25:window.append((active,quiet))
                            active=quiet=None
                    position+=count/rate
            if active is not None:window.append((active,right))
            core_end=min(total,cursor+60)
            for a,b in window:
                a=max(cursor,a);b=min(core_end,b)
                if b<=a:continue
                if spans and a-spans[-1][1]<.36:spans[-1][1]=max(b,spans[-1][1])
                else:spans.append([a,b])
            cursor=core_end
            write_json(output,{'spans':spans,'processed_seconds':cursor,'total_seconds':total,'complete':cursor>=total})


def emotions(payload, models, output):
    import numpy as np
    import soundfile as sf
    import torch
    from funasr import AutoModel
    # CPU inference avoids loading the emotion network beside the CUDA ASR model.
    torch.set_num_threads(2)
    model=AutoModel(model=str(models/'voice-analysis/emotion2vec_plus_base'),device='cpu',
                    disable_update=True,disable_pbar=True,trust_remote_code=False)
    values=read_json(output).get('values',{})
    for item in payload['items']:
        if item['id'] in values:continue
        samples,rate=sf.read(item['audio'],dtype='float32')
        if rate!=16000:raise ValueError('Emotion input must be 16 kHz')
        with torch.inference_mode():
            result=model.generate(input=samples,granularity='utterance',extract_embedding=False,fs=16000)[0]
        # YIN is a descriptive acoustic feature; it does not determine emotion.
        import librosa
        pitches=librosa.yin(samples,fmin=65,fmax=550,sr=rate)
        rms=librosa.feature.rms(y=samples)[0]
        pitches=pitches[:len(rms)][rms[:len(pitches)]>max(.005,float(np.max(rms))*.15)]
        values[item['id']]={'emotion':emotion_summary(result['labels'],result['scores']),
                           'pitch_range_hz':round(float(np.percentile(pitches,90)-np.percentile(pitches,10)),1) if len(pitches) else None}
        write_json(output,{'values':values,'completed':len(values),'total':len(payload['items']),'complete':False})
    write_json(output,{'values':values,'complete':True})


def transcripts(payload, models, output):
    from recording_transcript.qwen_asr_windows import _transcribe_one
    values=read_json(output).get('values',{})
    for item in payload['items']:
        if item['id'] not in values:
            result=_transcribe_one(Path(item['audio']),models/'asr/Qwen3-ASR-1.7B')
            values[item['id']]=result['text']
            write_json(output,{'values':values,'completed':len(values),'total':len(payload['items']),'complete':False})
    write_json(output,{'values':values,'complete':True})


def main():
    from recording_transcript.windows_worker import watch_parent
    watch_parent()
    socket.socket.connect=deny_network
    socket.create_connection=deny_network
    request=json.loads(Path(sys.argv[1]).read_text())
    functions={'vad':vad,'emotion':emotions,'transcript':transcripts}
    functions[request['kind']](request['payload'],Path(request['models']),Path(request['output']))


if __name__=='__main__':main()
