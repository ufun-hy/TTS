"""Signed release discovery and resumable downloads; installation stays offline."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import zipfile

from .update import UpdateError, _open_https, _require_https


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def append_history(data, event, **fields):
    record = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}
    path = Path(data) / 'updates/update-history.jsonl'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    except OSError:
        pass


def require(condition):
    if not condition:
        raise ValueError('Invalid release field')


def verify_signed_payload(envelope, public_key):
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateError('更新验签组件 cryptography 缺失或无法加载，请重新安装完整客户端。') from exc
    try:
        payload = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, payload)
        return json.loads(payload)
    except (InvalidSignature, ValueError, TypeError, KeyError, UpdateError) as exc:
        raise UpdateError('更新清单签名或内容无效') from exc


def verify_release(envelope, public_key):
    try:
        release = verify_signed_payload(envelope, public_key)
        require(release['schema'] == 1)
        require(type(release['sequence']) is int and release['sequence'] > 0)
        require(re.fullmatch(r'[0-9a-f]{40}', release['commit']))
        require(isinstance(release['version'], str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}', release['version']))
        require(isinstance(release['notes'], str) and len(release['notes']) <= 20000)
        require(release['packages'])
        for package in release['packages']:
            require(package['kind'] in ('patch', 'installer'))
            require(type(package['size']) is int and 0 < package['size'] <= 20 * 1024**3)
            require(re.fullmatch(r'[0-9a-f]{64}', package['sha256']))
            require(re.fullmatch(r'[0-9a-f]{64}', package['runtime_id']))
            _require_https(package['url'])
        bootstrap = release.get('bootstrap')
        if bootstrap is not None:
            require(isinstance(bootstrap, dict))
            require(bootstrap['version'] == release['version'])
            require(type(bootstrap['size']) is int and 0 < bootstrap['size'] <= 100 * 1024**2)
            require(re.fullmatch(r'[0-9a-f]{64}', bootstrap['sha256']))
            _require_https(bootstrap['url'])
        return release
    except (ValueError, TypeError, KeyError) as exc:
        raise UpdateError('更新清单签名或内容无效') from exc


def download_artifact(package, directory, progress=lambda done, total: None, suffix='.zip'):
    """Download one signed artifact, sharing resumable HTTPS and hash checks."""
    if not isinstance(package, dict):
        raise UpdateError('下载文件清单无效')
    size, digest, url = package.get('size'), package.get('sha256'), package.get('url')
    if (type(size) is not int or not 0 < size <= 50 * 1024**3
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not isinstance(url, str) or not isinstance(suffix, str)
            or not re.fullmatch(r'\.[a-zA-Z0-9]{1,8}', suffix)):
        raise UpdateError('下载文件清单无效')
    _require_https(url)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + suffix)
    if target.exists() and file_hash(target) == digest:
        progress(size, size)
        return target
    partial = target.with_suffix('.partial')
    offset = partial.stat().st_size if partial.exists() else 0
    if offset >= size:
        offset = 0
    from urllib.request import Request, build_opener
    from .update import _HTTPSRedirectHandler
    req = Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
    with build_opener(_HTTPSRedirectHandler()).open(req, timeout=30) as response:
        if response.status == 206:
            expected = f'bytes {offset}-{size - 1}/{size}'
            if response.headers.get('Content-Range') != expected:
                raise UpdateError('下载续传范围不匹配，请重试')
        elif response.status == 200:
            offset = 0
        else:
            raise UpdateError(f'下载失败：HTTP {response.status}')
        with partial.open('ab' if offset else 'wb') as output:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                offset += len(block)
                if offset > size:
                    raise UpdateError('下载内容超过清单大小')
                output.write(block)
                progress(offset, size)
    if offset != size or file_hash(partial) != digest:
        raise UpdateError('下载不完整或 SHA256 不匹配；可重试下载')
    partial.replace(target)
    return target


_CURRENT_INSTALL = object()


class OnlineUpdater:
    def __init__(self, app, data, current=_CURRENT_INSTALL):
        self.app, self.data = Path(app), Path(data)
        self.config = json.loads((self.app / 'config/update-source.json').read_text(encoding='utf-8'))
        self.current = (json.loads((self.app / 'app-manifest.json').read_text(encoding='utf-8'))
                        if current is _CURRENT_INSTALL else (current or {}))
        self.release = self.package = None

    def check(self):
        try:
            release = self._check()
        except Exception as exc:
            append_history(self.data, 'check_failed', error=f'{type(exc).__name__}: {exc}')
            raise
        if release:
            append_history(self.data, 'update_available', version=release['version'], sequence=release['sequence'],
                           commit=release['commit'], kind=self.package['kind'], size=self.package['size'],
                           sha256=self.package['sha256'])
        else:
            append_history(self.data, 'up_to_date', version=self.current.get('version', ''),
                           commit=self.current.get('commit', ''),
                           sequence=self.current.get('release_sequence', 0))
        return release

    def _check(self):
        url = self.config.get('manifest_url', '')
        if not url or not self.config.get('public_key'):
            raise UpdateError('发布方尚未配置更新地址和签名公钥。请使用离线补丁。')
        with _open_https(url, 15) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise UpdateError('更新清单过大')
        release = verify_release(json.loads(raw), self.config['public_key'])
        if (self.current and (release['commit'] == self.current['commit']
                or release['sequence'] <= self.current.get('release_sequence', 0))):
            self.release = self.package = None
            return None
        packages = release['packages']
        self.package = next((p for p in packages if self.current and p['kind'] == 'patch'
                             and p['runtime_id'] == self.current['runtime_id']), None)
        if self.package is None:
            self.package = next((p for p in packages if p['kind'] == 'installer'), None)
        if self.package is None:
            raise UpdateError('此版本需要新的完整安装包，发布方尚未提供兼容包。')
        self.release = release
        return release

    def download(self, progress=lambda done, total: None):
        if not self.package:
            raise UpdateError('请先检查更新')
        try:
            target = download_artifact(self.package, self.data / 'updates/downloads', progress)
        except Exception as exc:
            append_history(self.data, 'download_failed', version=(self.release or {}).get('version', ''),
                           kind=self.package['kind'], size=self.package['size'], sha256=self.package['sha256'],
                           error=f'{type(exc).__name__}: {exc}')
            raise
        append_history(self.data, 'download_verified', version=(self.release or {}).get('version', ''),
                       kind=self.package['kind'], size=self.package['size'], sha256=self.package['sha256'])
        return target

    def unpack(self, archive):
        try:
            destination = self._unpack(archive)
        except Exception as exc:
            append_history(self.data, 'package_validation_failed',
                           version=(self.release or {}).get('version', ''),
                           kind=(self.package or {}).get('kind', ''),
                           sha256=(self.package or {}).get('sha256', ''),
                           error=f'{type(exc).__name__}: {exc}')
            raise
        append_history(self.data, 'package_validated', version=(self.release or {}).get('version', ''),
                       kind=self.package['kind'], sha256=self.package['sha256'])
        return destination

    def _unpack(self, archive):
        if not self.package or file_hash(archive) != self.package['sha256']:
            raise UpdateError('安装前校验失败，请重新下载')
        destination = self.data / 'updates/downloads' / ('staged-' + uuid.uuid4().hex)
        with zipfile.ZipFile(archive) as bundle:
            size, seen = 0, set()
            for item in bundle.infolist():
                name = PurePosixPath(item.filename)
                key = str(name).casefold()
                size += item.file_size
                if (name.is_absolute() or '..' in name.parts or '\\' in item.orig_filename or ':' in item.orig_filename
                    or any(part.endswith((' ', '.')) or part.split('.')[0].upper() in
                           {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
                           for part in name.parts)
                    or stat.S_ISLNK(item.external_attr >> 16) or key in seen
                    or size > 30 * 1024**3 or len(seen) > 100000):
                    raise UpdateError('更新包包含不安全路径或超出大小限制')
                seen.add(key)
            destination.mkdir(parents=True)
            bundle.extractall(destination)
        if self.package['kind'] == 'patch':
            manifest = json.loads((destination / 'payload/app-manifest.json').read_text(encoding='utf-8'))
            if (manifest['commit'] != self.release['commit'] or manifest['runtime_id'] != self.package['runtime_id']
                or manifest.get('release_sequence') != self.release['sequence']):
                raise UpdateError('补丁版本与签名清单不一致')
            if not (destination / 'Apply-Patch.ps1').is_file():
                raise UpdateError('补丁缺少安装入口')
        else:
            if not (destination / 'AI-Live-Studio-Windows-Test-Setup.exe').is_file():
                raise UpdateError('完整包缺少安装入口')
            manifest = json.loads((destination / 'app-manifest.json').read_text(encoding='utf-8'))
            if (manifest['commit'] != self.release['commit'] or manifest['runtime_id'] != self.package['runtime_id']
                or manifest.get('release_sequence') != self.release['sequence']):
                raise UpdateError('完整包版本与签名清单不一致')
        return destination


def ensure_idle(app, data):
    try:
        manifest = json.loads((Path(app) / 'app-manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    identity = {key: manifest.get(key, '') for key in ('version', 'commit', 'release_sequence')}
    try:
        _ensure_idle(app, data)
    except Exception as exc:
        append_history(data, 'maintenance_blocked', **identity,
                       error=f'{type(exc).__name__}: {exc}')
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError('无法确认本机任务已安全结束，请先停止服务后重试。') from exc
    append_history(data, 'maintenance_ready', **identity)


def _ensure_idle(app, data):
    """Fail closed when the runtime cannot establish a safe maintenance boundary."""
    import subprocess
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener, ProxyHandler
    from .settings import SecretStoreError, tts_secret_store
    result = subprocess.run(
        [str(Path(app) / 'runtime/python/python.exe'), str(Path(app) / 'scripts/windows-runtime.py'),
         'status', '--data', str(data)], capture_output=True, encoding='utf-8', errors='replace',
        timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if getattr(result, 'returncode', 0) != 0:
        raise UpdateError('无法读取服务状态，请先通过启动器停止服务，再更新。')
    snapshot = json.loads(result.stdout)
    processes = snapshot.get('processes')
    if not isinstance(processes, dict) or not processes:
        raise UpdateError('无法确认服务状态，请先通过启动器停止服务，再更新。')
    if any(not isinstance(process, dict) or type(process.get('running')) is not bool
           for process in processes.values()):
        raise UpdateError('存在无法确认运行状态的服务，请先处理服务状态。')
    if any(p.get('running') and not p.get('owned') for p in processes.values()):
        raise UpdateError('存在无法确认归属的服务，请先处理服务状态。')
    if not any(p.get('running') for p in processes.values()):
        return
    opener = build_opener(ProxyHandler({}))
    try:
        api_key = tts_secret_store(data).get()
    except SecretStoreError:
        api_key = ''
    def read(url):
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        with opener.open(Request(url, headers=headers), timeout=5) as response:
            return json.load(response)
    try:
        live = read('http://127.0.0.1:8770/api/live/status')
        runtime = read('http://127.0.0.1:8770/api/runtime/status')
        jobs = read('http://127.0.0.1:8771/api/transcript/jobs')['jobs']
        replay = read('http://127.0.0.1:8000/audio/replay/status')
        try:
            playback = read('http://127.0.0.1:8770/api/playback/status')
        except HTTPError as exc:
            error_body = exc.read(4096).decode('utf-8', 'replace')
            if exc.code != 409 or 'independent playback requires a generation-only session' not in error_body:
                raise
            playback = {}
        client = playback.get('client_state') if isinstance(playback.get('client_state'), dict) else {}
        replay_active = replay.get('status') not in ('idle', 'stopped', 'failed')
        playback_active = (bool(playback) and playback.get('status') not in ('stopped', 'failed'))
        playback_active = (playback_active or client.get('playback_status') not in ('', 'idle')
                           or client.get('buffered_segments', 0) > 0
                           or client.get('buffered_seconds', 0) > 0)
        if (live.get('status') not in ('idle', 'stopped', 'failed') or runtime.get('state') != 'IDLE'
            or not runtime.get('model_released')
            or live.get('client_buffered_segments', 0) > 0 or live.get('client_buffered_seconds', 0) > 0
            or replay_active or playback_active
            or any(job.get('status') not in ('completed', 'failed', 'cancelled') for job in jobs)):
            raise UpdateError('仍有直播、生成、播放、录音任务或模型占用，请结束任务并停止服务后更新。')
    except (OSError, ValueError, KeyError) as exc:
        raise UpdateError('无法确认任务已结束，请先停止服务，再更新。') from exc
