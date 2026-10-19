"""Signed, resumable model installation outside the application directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from .online_update import append_history, download_artifact, file_hash, verify_signed_payload
from .update import UpdateError, _open_https, _require_https


def inspect_local_models(models_dir, app_dir=None):
    """Report local model readiness without requiring a network manifest."""
    models = Path(models_dir).expanduser()
    app = Path(app_dir) if app_dir else models
    result = {}

    asr = models / 'asr' / 'Qwen3-ASR-1.7B'
    asr_errors = []
    if not asr.is_dir():
        asr_errors.append(f'Qwen3-ASR-1.7B 未找到：{asr}')
    else:
        try:
            config = json.loads((asr / 'config.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            config = {}
        if (config.get('model_type') != 'qwen3_asr'
                or any(not (asr / name).is_file() for name in
                       ('config.json', 'preprocessor_config.json', 'tokenizer_config.json'))
                or not any(asr.glob('*.safetensors'))):
            asr_errors.append(f'Qwen3-ASR-1.7B 文件不完整：{asr}')
    result['asr'] = {'ready': not asr_errors, 'errors': asr_errors}

    tts = models / 'tts'
    tts_errors = []
    if not (tts / 'CosyVoice3-2512_Q8_0.gguf').is_file():
        tts_errors.append(f'CosyVoice3 Q8 模型未找到：{tts / "CosyVoice3-2512_Q8_0.gguf"}')
    voices_path = tts / 'voices.json'
    try:
        voices = json.loads(voices_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        voices = None
        tts_errors.append(f'voices.json 未找到：{voices_path}')
    except (OSError, ValueError):
        voices = None
        tts_errors.append(f'voices.json 无法读取：{voices_path}')
    if voices is not None:
        if not isinstance(voices, dict) or 'default' not in voices:
            tts_errors.append('voices.json 缺少 default 音色')
        elif isinstance(voices, dict):
            for voice_id, entry in voices.items():
                prompt = entry.get('prompt_speech') if isinstance(entry, dict) else ''
                if (not isinstance(voice_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', voice_id)
                        or not isinstance(prompt, str) or not prompt):
                    tts_errors.append(f'音色配置无效：{voice_id}')
                    continue
                prompt_path = Path(prompt).expanduser()
                candidates = ([prompt_path] if prompt_path.is_absolute()
                              else [app / prompt_path, voices_path.parent / prompt_path])
                if not any(path.is_file() for path in candidates):
                    tts_errors.append(f'音色 {voice_id} 的 prompt_speech 未找到：{candidates[-1]}')
    result['tts'] = {'ready': not tts_errors, 'errors': tts_errors}

    manifests = models / 'llm' / 'ollama-store' / 'manifests'
    modelfile = models / 'llm' / 'Modelfile'
    llm_errors = []
    if not modelfile.is_file() or not any(path.is_file() for path in manifests.rglob('*')):
        llm_errors.append(f'Qwen3 8B / Ollama 模型未找到或不完整：{modelfile}、{manifests}')
    result['llm'] = {'ready': not llm_errors, 'errors': llm_errors}
    return result


def _safe_model_path(value):
    path = PurePosixPath(value) if isinstance(value, str) else PurePosixPath('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if (not isinstance(value, str) or not value or len(value) > 1024
            or path.is_absolute() or '\\' in value or ':' in value
            or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                   or part.split('.')[0].upper() in reserved for part in path.parts)
            or '/'.join(path.parts) != value):
        raise UpdateError(f'模型清单包含不安全路径：{value}')
    if path.parts[0].casefold() in ('.downloads', '.ai-live-studio-models.json'):
        raise UpdateError(f'模型清单占用了安装器保留路径：{value}')
    return path


def verify_model_manifest(envelope, public_key):
    try:
        manifest = verify_signed_payload(envelope, public_key)
        if (manifest['schema'] != 1 or manifest['product'] != 'AI Live Studio'
                or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', manifest['id'])
                or not isinstance(manifest['version'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}', manifest['version'])
                or type(manifest['sequence']) is not int or manifest['sequence'] < 1
                or not isinstance(manifest['files'], list) or not manifest['files']
                or len(manifest['files']) > 50000):
            raise ValueError('invalid model manifest header')
        seen = set()
        for item in manifest['files']:
            path = _safe_model_path(item['path'])
            key = str(path).casefold()
            if key in seen:
                raise ValueError('duplicate model path')
            seen.add(key)
            if (type(item['size']) is not int or not 0 < item['size'] <= 50 * 1024**3
                    or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
                raise ValueError('invalid model file metadata')
            _require_https(item['url'])
        if sum(item['size'] for item in manifest['files']) > 200 * 1024**3:
            raise ValueError('model manifest exceeds the total size limit')
        return manifest
    except (ValueError, TypeError, KeyError) as exc:
        raise UpdateError('模型清单签名或内容无效') from exc


class ModelInstaller:
    def __init__(self, models_dir, data_dir, config_path):
        self.models_dir = Path(models_dir).expanduser()
        self.data_dir = Path(data_dir)
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.manifest = None
        self.envelope_hash = ''
        self.state_path = self.models_dir / '.ai-live-studio-models.json'
        self.state = {}

    def check(self):
        try:
            manifest = self._check()
        except Exception as exc:
            append_history(self.data_dir, 'model_check_failed', error=f'{type(exc).__name__}: {exc}')
            raise
        append_history(self.data_dir, 'model_manifest_checked', model_id=manifest['id'],
                       version=manifest['version'], sequence=manifest['sequence'],
                       manifest_sha256=self.envelope_hash, files=len(manifest['files']),
                       size=sum(item['size'] for item in manifest['files']))
        return manifest

    def _check(self):
        url, public_key = self.config.get('manifest_url', ''), self.config.get('public_key', '')
        if not url or not public_key:
            raise UpdateError('模型下载源尚未配置签名清单地址和公钥。')
        with _open_https(url, 15) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise UpdateError('模型清单过大')
        envelope = json.loads(raw)
        manifest = verify_model_manifest(envelope, public_key)
        digest = hashlib.sha256(raw).hexdigest()
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, OSError, ValueError):
            state = {}
        if (not isinstance(state, dict) or type(state.get('sequence', 0)) is not int
                or not isinstance(state.get('files', {}), dict)):
            state = {}
        if state.get('id') == manifest['id']:
            if state.get('sequence', 0) > manifest['sequence']:
                raise UpdateError('模型清单版本低于本机已安装版本，已阻止降级。')
            if (state.get('sequence') == manifest['sequence'] and state.get('manifest_sha256')
                    and state['manifest_sha256'] != digest):
                raise UpdateError('同一模型版本序号对应了不同清单，已阻止覆盖。')
        self.state = state if state.get('id') == manifest['id'] else {}
        self.manifest, self.envelope_hash = manifest, digest
        return manifest

    def _target(self, relative):
        root = self.models_dir.resolve()
        target = root.joinpath(*_safe_model_path(relative).parts)
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise UpdateError(f'模型路径指向安装目录之外：{relative}') from exc
        return target

    def _downloads_dir(self):
        path = self.models_dir / '.downloads'
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise UpdateError('模型下载目录不能是符号链接。')
        try:
            path.resolve().relative_to(self.models_dir.resolve())
        except ValueError as exc:
            raise UpdateError('模型下载目录指向了模型目录之外。') from exc
        return path

    def status(self):
        if self.manifest is None:
            raise UpdateError('请先检查模型更新')
        missing, changed, reused = [], [], []
        for item in self.manifest['files']:
            target = self._target(item['path'])
            if not target.exists():
                missing.append(item['path'])
            elif target.is_file() and file_hash(target) == item['sha256']:
                reused.append(item['path'])
            else:
                changed.append(item['path'])
        result = {
            'id': self.manifest['id'], 'version': self.manifest['version'],
            'sequence': self.manifest['sequence'], 'missing': missing,
            'changed': changed, 'reused': reused,
            'update_available': bool(missing or changed or self.state.get('sequence', 0) < self.manifest['sequence']),
        }
        append_history(self.data_dir, 'model_status', model_id=result['id'], version=result['version'],
                       sequence=result['sequence'], missing=len(missing), changed=len(changed),
                       reused=len(reused), update_available=result['update_available'],
                       manifest_sha256=self.envelope_hash)
        return result

    def install(self, progress=lambda done, total: None):
        try:
            result = self._install(progress)
        except Exception as exc:
            append_history(self.data_dir, 'model_install_failed',
                           model_id=(self.manifest or {}).get('id', ''),
                           version=(self.manifest or {}).get('version', ''),
                           sequence=(self.manifest or {}).get('sequence', 0),
                           manifest_sha256=self.envelope_hash,
                           error=f'{type(exc).__name__}: {exc}')
            raise
        append_history(self.data_dir, 'model_install_complete', model_id=self.manifest['id'],
                       version=result['version'], sequence=self.manifest['sequence'],
                       installed=result['installed'], reused=result['reused'],
                       bytes=result['bytes'], manifest_sha256=result['manifest_sha256'])
        return result

    def _install(self, progress):
        if self.manifest is None:
            self.check()
        old_files = self.state.get('files', {})
        total = sum(item['size'] for item in self.manifest['files'])
        completed, installed, reused = 0, 0, 0
        for item in self.manifest['files']:
            target = self._target(item['path'])
            if target.is_file() and file_hash(target) == item['sha256']:
                reused += 1
                completed += item['size']
                progress(completed, total)
                continue
            if target.exists():
                old_digest = old_files.get(item['path']) if isinstance(old_files, dict) else None
                if not target.is_file() or not old_digest or file_hash(target) != old_digest:
                    raise UpdateError(f'现有文件与签名模型不一致，未覆盖：{target}')
            downloads = self._downloads_dir()
            source = download_artifact(item, downloads,
                lambda done, size, base=completed: progress(base + done, total), suffix='.model')
            target.parent.mkdir(parents=True, exist_ok=True)
            # Resolve again after creating parents so a junction cannot redirect writes.
            target = self._target(item['path'])
            if file_hash(source) != item['sha256']:
                raise UpdateError(f'模型安装前校验失败：{item["path"]}')
            os.replace(source, target)
            installed += 1
            completed += item['size']
            progress(completed, total)
        state = {
            'schema': 1, 'id': self.manifest['id'], 'version': self.manifest['version'],
            'sequence': self.manifest['sequence'], 'manifest_sha256': self.envelope_hash,
            'files': {item['path']: item['sha256'] for item in self.manifest['files']},
        }
        self.models_dir.mkdir(parents=True, exist_ok=True)
        temporary_state = self.state_path.with_suffix('.json.tmp')
        temporary_state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary_state, self.state_path)
        self.state = state
        return {'version': self.manifest['version'], 'installed': installed, 'reused': reused,
                'bytes': total, 'manifest_sha256': self.envelope_hash}


def set_models_directory(data_dir, models_dir):
    path = Path(data_dir) / 'config/models-path.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'models_dir': str(Path(models_dir).resolve())}, indent=2) + '\n',
                         encoding='utf-8')
    os.replace(temporary, path)
