"""Exercise request validation over HTTP before any mutation is dispatched."""
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from server import text_studio


class StudioRequestSecurityTests(unittest.TestCase):
    def test_post_origin_and_content_type(self):
        with tempfile.TemporaryDirectory() as directory:
            server = text_studio.StudioServer(('127.0.0.1', 0), text_studio.make_handler(Path(directory), 'http://gateway'))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def post(path, headers):
                request = Request(base + path, data=b'{"text":"hello"}', headers=headers)
                try:
                    response = urlopen(request, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    response.read()
                    return response.status
            try:
                for path in ['/api/generation/start', '/api/generation/pause', '/api/generation/resume', '/api/generation/stop', '/api/playback/start', '/api/playback/pause', '/api/playback/stop', '/api/audio/replay/start', '/api/audio/replay/pause', '/api/audio/replay/resume', '/api/audio/replay/stop', '/api/audio/library/delete', '/api/runtime/recover']:
                    for origin in ['https://attacker.example', 'null', 'http://localhost:1', 'http://localhost:bad', base + '/path']:
                        with self.subTest(path=path, origin=origin):
                            self.assertEqual(post(path, {'Content-Type': 'application/json', 'Origin': origin}), 403)
                    for media in ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data']:
                        self.assertEqual(post(path, {'Content-Type': media}), 403)
                for origin in [None, base, f'http://localhost:{server.server_port}']:
                    headers = {'Content-Type': 'application/json; charset=utf-8'}
                    if origin is not None:
                        headers['Origin'] = origin
                    self.assertEqual(post('/api/parse', headers), 200)
                self.assertEqual(post('/api/parse', {'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site'}), 403)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
