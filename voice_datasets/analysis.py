"""Bounded audio processing and deterministic candidate ranking (no model imports)."""
from __future__ import annotations

import array
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid
import wave

MAX_BYTES = 2 * 1024**3
MAX_SECONDS = 3600
FORMATS = {'.wav': 'wav', '.mp3': 'mp3', '.m4a': 'mov', '.mp4': 'mov'}
STYLE_NAMES = {'neutral': '自然', 'happy': '开心', 'surprised': '惊讶', 'angry': '生气',
               'sad': '低落', 'fearful': '恐惧', 'disgusted': '厌恶'}


def time_range(start, end, duration):
    try:
        start = float(start or 0)
        end = duration if end in (None, '') else float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError('时间范围必须是秒数') from exc
    if not all(math.isfinite(x) for x in (start, end, duration)) or not 0 <= start < end <= duration:
        raise ValueError('时间范围超出录音或起止时间无效')
    return start, end


def run_audio(command, log, control, timeout=900):
    log.parent.mkdir(parents=True, exist_ok=True)
    control.check()
    with log.open('ab') as stream:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        control.communicate(process, timeout)
    if process.returncode:
        raise ValueError(f'音频处理失败，日志：{log}')


def decode(ffmpeg, source, destination, control, *, start=0, duration=3601, rate=16000, denoise=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(uuid.uuid4().hex + '.wav')
    command = [str(ffmpeg), '-nostdin', '-v', 'error', '-n', '-protocol_whitelist', 'file,pipe',
               '-f', FORMATS[source.suffix.lower()], '-i', str(source), '-ss', str(start),
               '-t', str(duration), '-vn', '-ac', '1', '-ar', str(rate), '-c:a', 'pcm_s16le']
    if denoise:
        command += ['-af', 'afftdn=nr=6:tn=1']
    run_audio(command + [str(temporary)], destination.parent / 'audio.log', control)
    with wave.open(str(temporary), 'rb') as audio:
        seconds = audio.getnframes() / audio.getframerate()
    if seconds <= 0:
        raise ValueError('录音中没有可解码的音频')
    control.check()
    os.replace(temporary, destination)
    return seconds


def pcm_samples(audio, frames):
    samples = array.array('h', audio.readframes(frames))
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def quality(path, start, end, spans):
    """Scan only a <=30s candidate; thresholds are warnings, not noise labels."""
    with wave.open(str(path), 'rb') as audio:
        audio.setpos(round(start * audio.getframerate()))
        samples = pcm_samples(audio, round((end - start) * audio.getframerate()))
    if not samples:
        return {'usable': False, 'flags': ['没有有效音频'], 'score': 0}
    rms = math.sqrt(sum(x*x for x in samples) / len(samples)) / 32768
    db = 20 * math.log10(max(rms, 1e-9))
    clipping = sum(abs(x) >= 32760 for x in samples) / len(samples)
    speech = min(1, sum(max(0, min(end, b) - max(start, a)) for a, b in spans) / (end-start))
    flags = []
    if db < -35: flags.append('音量偏低，请试听')
    if clipping > .001: flags.append('存在削波风险，请试听')
    if speech < .65: flags.append('停顿较多')
    usable = speech >= .25 and db > -60
    score = speech * 100 - min(50, clipping*5000) - max(0, -35-db)
    return {'usable': usable, 'score': round(score, 2), 'rms_dbfs': round(db, 2),
            'clipping_ratio': round(clipping, 5), 'speech_ratio': round(speech, 3), 'flags': flags}


def candidate_ranges(spans, start, end):
    """Prefer pauses; long unbroken speech is offered for manual boundary review."""
    spans = [(max(start,a),min(end,b)) for a,b in spans if b>start and a<end]
    result = []
    index = 0
    while index < len(spans):
        a,b = spans[index]
        left = max(start,a-.12)
        if b-a > 29.5:
            while left < b:
                right = min(end,b+.12,left+25)
                if right-left >= 3:
                    result.append((left,right,['连续讲话，需手动确认边界']))
                left = right
            index += 1
            continue
        last = index
        right = min(end,b+.12)
        while last+1 < len(spans):
            na,nb = spans[last+1]
            if na-spans[last][1] > 2 or nb+.12-left > 25:
                break
            last += 1
            right = min(end,nb+.12)
            if right-left >= 10:
                break
        if right-left >= 3:
            flags = []
            if a<=start or b>=end: flags.append('处理范围边缘，需确认边界')
            result.append((left,right,flags))
        index = last+1
    return result


def emotion_summary(labels, scores):
    if len(labels)!=len(scores) or not scores or any(not math.isfinite(float(v)) for v in scores):
        raise ValueError('情绪模型返回无效结果')
    ranked = sorted(zip(labels,map(float,scores)), key=lambda x:x[1], reverse=True)
    label,score = ranked[0]
    key = str(label).split('/')[-1].lower()
    margin = score - (ranked[1][1] if len(ranked)>1 else 0)
    suggested = STYLE_NAMES.get(key) if score>=.7 and margin>=.2 else None
    return {'label': str(label), 'score': score, 'margin': margin, 'suggested': suggested or '待确认',
            'scores': dict(zip(labels,map(float,scores)))}


def ranked_candidates(candidates):
    ordered = sorted((c for c in candidates if c['quality']['usable']),
                     key=lambda c:(-c['quality']['score'],c['start']))
    unique=[]
    for candidate in ordered:
        if any(max(0,min(candidate['end'],x['end'])-max(candidate['start'],x['start'])) /
               min(candidate['end']-candidate['start'],x['end']-x['start']) > .5 for x in unique):
            continue
        unique.append(candidate)
    counts={};first=[];rest=[]
    for candidate in unique:
        style=candidate.get('emotion',{}).get('suggested','待确认')
        if counts.get(style,0)<3 and len(first)<12:
            first.append(candidate);counts[style]=counts.get(style,0)+1
        else: rest.append(candidate)
    # Keep recommendations diverse; further results remain explicitly accessible.
    return first+rest, len(first)
