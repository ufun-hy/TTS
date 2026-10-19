"""Trusted, loopback-only temporary speaker synthesis in the managed engine."""
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request


def synthesize_draft(body, data_root, engine, engine_url, synthesize):
    if not isinstance(body,dict):raise ValueError('请求格式无效')
    text=body.get('text')
    if not isinstance(text,str) or not 1<=len(text.strip())<=200:raise ValueError('试听文案需为 1～200 字')
    raw=body.get('prompt_path')
    if not isinstance(raw,str):raise ValueError('草稿路径无效')
    path=Path(raw).resolve()
    root=(data_root/'voice-recordings').resolve()
    try:relative=path.relative_to(root)
    except ValueError:raise ValueError('草稿必须来自本机音色工作目录')
    if len(relative.parts)!=2 or path.suffix!='.gguf' or not path.is_file():raise ValueError('音色草稿不存在')
    with path.open('rb') as stream:
        if stream.read(4)!=b'GGUF':raise ValueError('音色草稿无效')
    identity=hashlib.sha256((str(path)+str(path.stat().st_mtime_ns)).encode()).hexdigest()[:24]
    voice='draft_'+identity
    def perform(_):
        request=urllib.request.Request(engine_url.rstrip('/')+'/speaker',
            data=json.dumps({'type':'gguf','name':voice,'path':str(path)}).encode(),
            headers={'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=15) as response:response.read()
        except urllib.error.HTTPError as exc:
            if exc.code!=409:raise
        return synthesize(text.strip(),voice,engine_url)
    return engine.run(perform)
