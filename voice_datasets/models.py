"""Offline asset validation and isolated inference processes for voice creation."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from recording_transcript.control import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def asset_status(models, verify=False):
    manifest=json.loads((ROOT/'config/voice-analysis-models.json').read_text())
    base=models/'voice-analysis'
    missing=[]
    for name,entry in manifest['files'].items():
        path=base/name
        if not path.is_file() or path.stat().st_size!=entry['bytes']:
            missing.append(str(path))
        elif verify and sha256(path)!=entry['sha256']:
            missing.append(str(path)+'（校验失败）')
    for package, expected in {'torch':'2.6.0','torchaudio':'2.6.0','funasr':'1.2.6'}.items():
        try:
            actual=importlib.metadata.version(package).split('+')[0]
        except importlib.metadata.PackageNotFoundError:
            actual='未安装'
        if actual!=expected:missing.append(f'{package}=={expected}（当前 {actual}，请更新完整安装包）')
    asr=models/'asr/Qwen3-ASR-1.7B' 
    if not (asr/'config.json').is_file() or not any(asr.glob('*.safetensors')):
        missing.append(str(asr))
    return {'ready':not missing,'missing':missing,'model_versions':manifest['models']}


def run_model(kind, payload, directory, models, control):
    directory.mkdir(parents=True,exist_ok=True)
    request_path=directory/(uuid.uuid4().hex+'.request.json')
    output=directory/(kind+'.json')
    write_json(request_path, {'kind':kind,'payload':payload,'output':str(output),'models':str(models)})
    env=os.environ.copy()
    env.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','MODELSCOPE_OFFLINE':'1',
                'HF_HUB_DISABLE_TELEMETRY':'1','PYTHONIOENCODING':'utf-8','AI_ASR_PARENT_PID':str(os.getpid())})
    with (directory/(kind+'.log')).open('ab') as log:
        process=subprocess.Popen([sys.executable,'-m','voice_datasets.model_worker',str(request_path)],
            cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        control.communicate(process,timeout=7200)
    result=read_json(output)
    if process.returncode or not result.get('complete'):
        raise ValueError(f'{kind} 模型处理失败，详见 {directory / (kind+".log")}')
    return result
