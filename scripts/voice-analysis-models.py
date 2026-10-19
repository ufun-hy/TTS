#!/usr/bin/env python3
"""Explicit offline-pack preparation. Inference never invokes this downloader."""
import argparse
import json
from pathlib import Path
import sys
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from voice_datasets.models import sha256


def main():
    parser=argparse.ArgumentParser(description='Prepare/check pinned voice analysis model assets')
    parser.add_argument('--models',required=True,type=Path)
    parser.add_argument('--download',action='store_true',help='Explicitly download missing assets (about 1.1 GB)')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'config/voice-analysis-models.json').read_text())
    failed=[]
    for name,item in manifest['files'].items():
        target=args.models/'voice-analysis'/name
        if target.exists():
            if sha256(target)!=item['sha256']:failed.append(str(target)+' hash mismatch (retained)')
            continue
        if not args.download:
            failed.append(str(target)+' missing');continue
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.download')
        try:
            with urllib.request.urlopen(item['url'],timeout=60) as src,temporary.open('xb') as dst:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk:break
                    dst.write(chunk)
            if temporary.stat().st_size!=item['bytes'] or sha256(temporary)!=item['sha256']:
                raise ValueError('download checksum mismatch; partial file retained')
            temporary.rename(target)
        except Exception as exc:failed.append(f'{target}: {exc}')
    print(json.dumps({'ready':not failed,'issues':failed,'models':manifest['models']},ensure_ascii=False,indent=2))
    return bool(failed)


if __name__=='__main__':raise SystemExit(main())
