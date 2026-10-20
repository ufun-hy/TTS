import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import text_studio
from server.prohibited_speech import analyze, broadcast_text, filter_segments


CASES = [
    ('大家可以先试吃，不好吃不要钱。口感清甜。', '口感清甜。'),
    ('免费试吃。先吃再买。先试一下。试用。免费试用。', ''),
    ('不满意不要钱。七天无理由。７ 天无理由。无条件退款。', ''),
    ('免费体验；先试后买！先用后买。免费品尝。', ''),
    ('坏果包赔。假一赔十。我们承诺退款。支持7天退货。送运费险。', ''),
    ('有问题联系客服处理售后。退款流程请查看订单。按平台规则申请赔付。', '有问题联系客服处理售后。退款流程请查看订单。按平台规则申请赔付。'),
    ('不包赔。不支持退货。并非假一赔十。没有运费险。', '不包赔。不支持退货。并非假一赔十。没有运费险。'),
    ('🍎欢迎。“免费试用。”口感好。', '🍎欢迎。口感好。'),
    ('到手19.9元，大家先试吃，不好吃不要钱。新鲜采摘！', '新鲜采摘！'),
    ('试 吃\n欢迎大家\r\n免费体验', '欢迎大家'),
    ('今天是{{current_time}}。试吃。明天见。', '今天是{{current_time}}。明天见。'),
    ('适用多种场景。售后问题请咨询客服。', '适用多种场景。售后问题请咨询客服。'),
    ('不提供试吃。', ''),  # Literal trial/free/unconditional expressions are prohibited even in disclaimers.
    ('', ''),
]


class ProhibitedSpeechTests(unittest.TestCase):
    def test_python_browser_rules_agree_and_preserve_sentences(self):
        for source, expected in CASES:
            with self.subTest(source=source):
                self.assertEqual(broadcast_text(source), expected)
                self.assertEqual(broadcast_text(expected), expected)
                for hit in analyze(source)['blocked']:
                    self.assertEqual(source[hit['start']:hit['end']], hit['text'])
        root = Path(__file__).resolve().parents[1]
        script = "const {analyze}=require('./web/text-studio-prohibited.js');let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(analyze))));"
        response = subprocess.run(['node', '-e', script], cwd=root, input=json.dumps([x[0] for x in CASES]), capture_output=True, text=True, check=True)
        for (source, expected), browser in zip(CASES, json.loads(response.stdout)):
            self.assertEqual(browser['text'], expected)
            self.assertEqual([x['text'] for x in browser['blocked']], [x['text'] for x in analyze(source)['blocked']])

    def test_filter_all_candidates_and_leave_input_untouched(self):
        raw = [{'id':'p1','candidates':['试吃。','欢迎。免费体验。','有问题联系客服处理售后。']}, {'id':'p2','candidates':['七天无理由。']}]
        before = json.dumps(raw)
        self.assertEqual(filter_segments(raw), [{'id':'p1','candidates':['欢迎。','有问题联系客服处理售后。']}])
        self.assertEqual(json.dumps(raw), before)
        self.assertEqual(filter_segments([{'id':'p1','text':'安全。试吃。'}]), [{'id':'p1','text':'安全。'}])
        with self.assertRaisesRegex(ValueError, '没有可播报内容'):
            filter_segments([{'id':'p1','candidates':['试吃。']}])
        with self.assertRaises(ValueError):
            filter_segments([{'id':'p1','text':'安全。'}, {'id':'p1','text':'试吃。'}])

    def test_preview_filters_before_credentials_transport_or_cache(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"success":true}'
        response.__enter__.return_value = response
        with mock.patch.object(text_studio, '_read_keychain_api_key', return_value='test-key') as key, mock.patch.object(text_studio.urllib.request, 'urlopen', return_value=response) as transport:
            text_studio._tts_preview('欢迎。大家先试吃，不好吃不要钱。', 'default', 'http://gateway')
            self.assertEqual(json.loads(transport.call_args.args[0].data)['text'], '欢迎。')
            transport.reset_mock(); key.reset_mock()
            with self.assertRaisesRegex(ValueError, '没有可试听内容'):
                text_studio._tts_preview('试吃。', 'default', 'http://gateway')
            transport.assert_not_called(); key.assert_not_called()

    def test_http_boundary_filters_even_without_browser_detection(self):
        root = Path(tempfile.mkdtemp(prefix='prohibited-api-test-'))
        live = mock.Mock()
        live.start.return_value = {'status':'starting'}
        with mock.patch.object(text_studio, 'build_live_manager', return_value=live):
            server = text_studio.StudioServer(('127.0.0.1', 0), text_studio.make_handler(root, 'http://127.0.0.1:1'))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(segments):
            req = Request(base+'/api/live/start', data=json.dumps({'segments':segments, 'ignored':True}).encode(), headers={'Content-Type':'application/json'})
            return urlopen(req)
        try:
            with post([{'id':'p1','candidates':['试吃。新鲜采摘。']},{'id':'p2','candidates':['免费体验。']}]): pass
            self.assertEqual(live.start.call_args.args[1], [{'id':'p1','candidates':['新鲜采摘。']}])
            live.start.reset_mock()
            with self.assertRaises(HTTPError) as error:
                post([{'id':'p1','text':'不满意不要钱。'}])
            self.assertEqual(error.exception.code, 400)
            live.start.assert_not_called()
            with urlopen(base+'/text-studio-prohibited-rules.js') as response:
                self.assertIn('试吃', response.read().decode())
        finally:
            server.shutdown(); server.server_close(); thread.join()
