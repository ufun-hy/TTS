"""Loopback-only HTTP adapter for the Windows recording-to-voice workflow."""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.parse

from local_runtime import RuntimeBusyError, RuntimeStage
from voice_datasets.registration import VoiceRegistration


def handle_voice_request(handler, service: VoiceRegistration, runtime, confirm_release):
    path = urllib.parse.urlsplit(handler.path).path
    prefix = '/api/voice-registration'
    if path != '/voice-studio' and path not in {'/voice-studio.js', '/voice-studio.css', '/windows-session.js'} and path != prefix and not path.startswith(prefix + '/'):
        return False
    origin = handler.headers.get('Origin')
    host = urllib.parse.urlsplit('http://' + handler.headers.get('Host', ''))
    if (handler.client_address[0] not in {'127.0.0.1', '::1'}
            or host.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or (origin and origin != 'http://' + handler.headers.get('Host'))):
        handler._json(403, {'error': '仅允许本机同源访问'})
        return True
    try:
        jobs = getattr(service, 'jobs', None)
        if jobs and path in {'/voice-studio', '/voice-studio.js', '/voice-studio.css', '/windows-session.js'} and handler.command == 'GET':
            name = 'voice-studio.html' if path == '/voice-studio' else path[1:]
            target = service.project_root / 'web' / name
            content_type = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]]
            body = target.read_bytes()
            handler.send_response(200)
            handler.send_header('Content-Type', content_type + '; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return True
        if jobs and path.startswith(prefix + '/jobs'):
            return handle_jobs(handler, jobs, path[len(prefix + '/jobs'):])
        if path == prefix and handler.command == 'GET':
            handler._json(200, service.readiness())
        elif path == prefix + '/upload' and handler.command == 'POST':
            handler.connection.settimeout(30)
            length = int(handler.headers.get('Content-Length', '0'))
            filename = urllib.parse.unquote(handler.headers.get('X-Filename', ''))
            handler._json(201, service.upload(handler.rfile, length, filename))
        elif path == prefix + '/register' and handler.command == 'POST':
            if handler.headers.get_content_type() != 'application/json':
                raise ValueError('请使用 JSON 提交音色名称和原文')
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('名称和原文请求过大或为空')
            body = json.loads(handler.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('请求格式无效')
            if body.get('confirmed') is not True:
                raise ValueError('请先确认原文与录音一致')
            lease = runtime.acquire(RuntimeStage.TTS_PREPARING, 'voice-registration', 'CosyVoice frontend',
                                    confirm_release=confirm_release)
            released = False
            try:
                released = confirm_release()
                if not released:
                    raise ValueError('TTS 引擎尚未释放，请在运行状态中恢复后重试')
                result = service.register(body.get('draft_id'), body.get('name'), body.get('transcript'))
            finally:
                runtime.release(lease, '' if released else 'TTS 引擎未确认释放')
            handler._json(201, result)
        else:
            handler._json(404, {'error': 'not_found'})
    except KeyError:
        handler._json(404, {'error': '任务不存在'})
    except RuntimeBusyError:
        handler._json(409, {'error': '正在直播、转写或生成，请等当前任务完成后再添加音色'})
    except (ValueError, TypeError) as exc:
        handler._json(400, {'error': str(exc)})
    except OSError as exc:
        handler._json(500, {'error': f'无法处理音色文件：{exc}'})
    return True


def _body(handler):
    length = int(handler.headers.get('Content-Length', '0'))
    if not 0 < length <= 16384 or handler.headers.get_content_type() != 'application/json':
        raise ValueError('请使用有效的 JSON 请求')
    value = json.loads(handler.rfile.read(length))
    if not isinstance(value, dict):
        raise ValueError('请求格式无效')
    return value


def handle_jobs(handler, jobs, suffix):
    method = handler.command
    if suffix == '/readiness' and method == 'GET':
        handler._json(200, jobs.readiness())
    elif suffix == '/shutdown' and method == 'POST':
        stopped = jobs.shutdown()
        handler._json(200 if stopped else 409, {'stopped': stopped})
    elif suffix == '' and method == 'GET':
        handler._json(200, {'jobs': jobs.list()})
    elif suffix == '' and method == 'POST':
        handler.connection.settimeout(30)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
        first = lambda key, default=None: params.get(key, [default])[0]
        job = jobs.create(handler.rfile, int(handler.headers.get('Content-Length', '0')),
            urllib.parse.unquote(handler.headers.get('X-Filename', '')), first('name', ''),
            first('start', 0), first('end'))
        handler._json(202, job)
    else:
        match = re.fullmatch(r'/([0-9a-f]{32})(?:/(heartbeat|pause|resume|more|edit|preview|save))?', suffix)
        media = re.fullmatch(r'/([0-9a-f]{32})/media/(c[0-9]{5})/([0-9]+)/(original|denoised|preview)', suffix)
        if media and method == 'GET':
            path = jobs.media(*media.groups())
            size = path.stat().st_size
            start, end = 0, size - 1
            request_range = handler.headers.get('Range')
            if request_range:
                bounds = re.fullmatch(r'bytes=(\d+)-(\d*)', request_range)
                if not bounds or int(bounds[1]) >= size:
                    handler.send_response(416)
                    handler.send_header('Content-Range', f'bytes */{size}')
                    handler.end_headers()
                    return True
                start = int(bounds[1]); end = min(size-1, int(bounds[2]) if bounds[2] else size-1)
                if end < start: raise ValueError('音频范围无效')
            handler.send_response(206 if request_range else 200)
            handler.send_header('Content-Type', 'audio/wav')
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Content-Length', str(end-start+1))
            if request_range: handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            handler.end_headers()
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk: break
                    handler.wfile.write(chunk); remaining -= len(chunk)
        elif match and method == 'GET' and not match[2]:
            handler._json(200, jobs.get(match[1]))
        elif match and method == 'POST' and match[2]:
            # Beacons for pause/heartbeat carry no JSON document.
            body = {} if match[2] in ('pause', 'heartbeat') else _body(handler)
            handler._json(202, jobs.action(match[1], match[2], body))
        else:
            handler._json(404, {'error': 'not_found'})
    return True
