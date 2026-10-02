"""Create local CosyVoice prompts from user-confirmed short recordings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import uuid
import wave

from voice_datasets.prompt import build_zero_shot_prompt_text

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
EXTENSIONS = {'.wav', '.mp3', '.m4a', '.mp4'}
DRAFT_ID = re.compile(r'^[0-9a-f]{32}$')


class VoiceRegistration:
    def __init__(self, data: Path, models: Path, bin_dir: Path):
        self.root = data / 'voice-recordings'
        self.tts = models / 'tts'
        self.config = self.tts / 'voices.json'
        self.cli = bin_dir / 'cosyvoice' / ('cosyvoice-cli.exe' if os.name == 'nt' else 'cosyvoice-cli')
        self.ffmpeg = bin_dir / 'ffmpeg' / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        self._lock = threading.Lock()

    def readiness(self):
        required = [self.cli, self.ffmpeg, self.config,
                    self.tts / 'speech_tokenizer_v3.int8.onnx', self.tts / 'campplus.int8.onnx']
        missing = [str(path) for path in required if not path.is_file()]
        return {'ready': not missing, 'missing': missing, 'max_upload_bytes': MAX_UPLOAD_BYTES}

    def directory(self, draft_id):
        if not isinstance(draft_id, str) or not DRAFT_ID.fullmatch(draft_id):
            raise ValueError('录音编号无效，请重新上传')
        directory = self.root / draft_id
        if not (directory / 'reference.wav').is_file():
            raise ValueError('录音不存在或上传未完成，请重新上传')
        return directory

    def upload(self, stream, length: int, filename: str):
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTENSIONS:
            raise ValueError('仅支持 WAV、MP3、M4A、MP4')
        if not 0 < length <= MAX_UPLOAD_BYTES:
            raise ValueError('录音大小须在 1 字节到 50 MiB 之间')
        if not self.ffmpeg.is_file():
            raise ValueError(f'缺少 FFmpeg：{self.ffmpeg}')
        draft_id = uuid.uuid4().hex
        directory = self.root / draft_id
        directory.mkdir(parents=True)
        source = directory / ('source' + suffix)
        with source.open('xb') as output:
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('上传中断，请重新上传')
                output.write(chunk)
                remaining -= len(chunk)
        # Decode at most 31 seconds, then reject >30s rather than silently crop.
        temporary = directory / 'decoded.wav'
        self._run([str(self.ffmpeg), '-v', 'error', '-nostdin', '-n', '-protocol_whitelist', 'file,pipe',
                   '-f', {'.wav': 'wav', '.mp3': 'mp3', '.m4a': 'mov', '.mp4': 'mov'}[suffix], '-i', str(source),
                   '-t', '31', '-vn', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(temporary)],
                  directory / 'decode.log', 60)
        try:
            with wave.open(str(temporary), 'rb') as audio:
                duration = audio.getnframes() / audio.getframerate()
                if not 3 <= duration <= 30:
                    raise ValueError('请选择 3～30 秒的完整人声片段，建议 8～20 秒；长录音请先裁剪')
        except (wave.Error, EOFError) as exc:
            raise ValueError('录音解码结果无效，请换一个文件') from exc
        temporary.rename(directory / 'reference.wav')
        (directory / 'source.json').write_text(json.dumps({
            'filename': filename[:255], 'duration': duration,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'draft_id': draft_id, 'duration': round(duration, 2)}

    @staticmethod
    def _run(command, log: Path, timeout):
        with log.open('ab') as output:
            try:
                result = subprocess.run(command, stdout=output, stderr=output, timeout=timeout,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f'处理超时，请重试；日志保留在 {log}') from exc
        if result.returncode:
            raise ValueError(f'录音处理失败，请检查录音和运行库；日志：{log}')

    def register(self, draft_id, name, transcript):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError('请填写 1～60 字的音色名称')
        if not isinstance(transcript, str) or not 1 <= len(transcript.strip()) <= 2000:
            raise ValueError('请填写与录音一致的原文（最多 2000 字）')
        if '\x00' in transcript or '<|' in transcript or '|>' in transcript:
            raise ValueError('录音原文不能包含模型控制标记或空字符')
        prompt_text = build_zero_shot_prompt_text(transcript)
        directory = self.directory(draft_id)
        voice_id = 'voice_' + draft_id
        with self._lock:
            readiness = self.readiness()
            if not readiness['ready']:
                raise ValueError('缺少创建音色所需文件：\n' + '\n'.join(readiness['missing']))
            config_bytes = self.config.read_bytes()
            config = json.loads(config_bytes.decode('utf-8-sig'))
            if not isinstance(config, dict) or 'default' not in config:
                raise ValueError('音色配置无效，需保留 default 音色')
            if voice_id in config:
                # A lost HTTP response can be retried without creating another voice.
                entry = config[voice_id]
                if (isinstance(entry, dict) and entry.get('prompt_speech') == f'voices/{voice_id}.gguf'
                        and (self.tts / entry['prompt_speech']).is_file()):
                    return {'id': voice_id, 'label': entry.get('label', name.strip()), 'registered': True}
                raise ValueError('音色编号已存在，请重新上传')
            (directory / 'reference.txt').write_text(transcript.strip(), encoding='utf-8')
            run_id = uuid.uuid4().hex
            prompt = directory / f'prompt-{run_id}.gguf'
            self._run([str(self.cli), '--frontend-only', '--speech-tokenizer',
                       str(self.tts / 'speech_tokenizer_v3.int8.onnx'), '--campplus',
                       str(self.tts / 'campplus.int8.onnx'), '--prompt-audio',
                       str(directory / 'reference.wav'), '--prompt-text', prompt_text,
                       '--prompt-speech-output', str(prompt)], directory / 'frontend.log', 180)
            if not prompt.is_file() or prompt.stat().st_size < 24:
                raise ValueError('未生成有效音色文件，原配置保持不变')
            with prompt.open('rb') as stream:
                if stream.read(4) != b'GGUF':
                    raise ValueError('生成的音色格式无效，原配置保持不变')
            # Do not overwrite edits made outside this service while extraction ran.
            if self.config.read_bytes() != config_bytes:
                raise ValueError('音色配置在生成期间发生变化，请重试')
            output = self.tts / 'voices' / f'{voice_id}.gguf'
            output.parent.mkdir(parents=True, exist_ok=True)
            # A previous interrupted attempt may have left an unregistered asset.
            # Keep it, and never overwrite it implicitly.
            if output.exists():
                raise ValueError('目标音色文件已存在但未注册，请重新上传；原文件已保留')
            with prompt.open('rb') as source, output.open('xb') as target:
                shutil.copyfileobj(source, target)
            (directory / f'voices-before-{run_id}.json').write_bytes(config_bytes)
            config[voice_id] = {'prompt_speech': f'voices/{voice_id}.gguf', 'label': name.strip()}
            temporary = self.config.with_name(f'voices-{run_id}.tmp')
            temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, self.config)
            return {'id': voice_id, 'label': name.strip(), 'registered': True}
