"""Loopback-only HTTP adapter for the Windows recording-to-voice workflow."""
from __future__ import annotations

import json
import urllib.parse

from local_runtime import RuntimeBusyError, RuntimeStage
from voice_datasets.registration import VoiceRegistration


def handle_voice_request(handler, service: VoiceRegistration, runtime, confirm_release):
    path = urllib.parse.urlsplit(handler.path).path
    prefix = '/api/voice-registration'
    if path != prefix and not path.startswith(prefix + '/'):
        return False
    origin = handler.headers.get('Origin')
    host = urllib.parse.urlsplit('http://' + handler.headers.get('Host', ''))
    if (handler.client_address[0] not in {'127.0.0.1', '::1'}
            or host.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or (origin and origin != 'http://' + handler.headers.get('Host'))):
        handler._json(403, {'error': '仅允许本机同源访问'})
        return True
    try:
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
    except RuntimeBusyError:
        handler._json(409, {'error': '正在直播、转写或生成，请等当前任务完成后再添加音色'})
    except (ValueError, TypeError) as exc:
        handler._json(400, {'error': str(exc)})
    except OSError as exc:
        handler._json(500, {'error': f'无法处理音色文件：{exc}'})
    return True
