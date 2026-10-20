import copy
import json
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import text_studio
from server.prohibited_speech import RULES, analyze, broadcast_text, filter_segments
from server.context_variants import validate_variant_result, prepare_context_live, choose_variant_round
from server.semantic_tts_blocks import semantic_blocks

SENTENCE = '我给咱们姐妹把宠粉福利做到了，支持七天无理由，还给大家赠送运费险啊。'


def confirmation(text, ci=0, hit=None):
    hit = hit or analyze(text)['blocked'][0]
    return {'candidate_index': ci, 'candidate_text': text, 'start': hit['start'], 'text': hit['text'],
            'labels': hit['labels'], 'rules_version': RULES['version'], 'confirmed_at': '2026-09-26T00:00:00Z'}


class ProhibitedReviewTests(unittest.TestCase):
    def test_exact_occurrence_and_rule_scope_matches_browser(self):
        text = '🍑欢迎。' + SENTENCE + SENTENCE + '免费试吃。'
        review = confirmation(text)
        result = analyze(text, [review])
        self.assertEqual(result['text'], '🍑欢迎。' + SENTENCE)
        self.assertEqual(len(result['approved']), 1)
        self.assertEqual(len(result['blocked']), 2)
        cases = [(text, [review]), (text + '新的表述。', [review]), (text, []),
                 (text, [{**review, 'rules_version': -1}]), (text, [{**review, 'labels': []}])]
        script = "const {analyze}=require('./web/text-studio-prohibited.js');let s='';process.stdin.on('data',v=>s+=v);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(([t,r])=>analyze(t,r)))));"
        proc = subprocess.run(['node', '-e', script], input=json.dumps(cases), text=True, capture_output=True, check=True)
        for (source, reviews), browser in zip(cases, json.loads(proc.stdout)):
            python = analyze(source, reviews)
            self.assertEqual(browser['text'], python['text'])
            for field in ('blocked', 'approved'):
                self.assertEqual([v['text'] for v in browser[field]], [v['text'] for v in python[field]])

    def test_candidate_slot_and_empty_or_ignored_state_do_not_grant_approval(self):
        text = SENTENCE
        review = confirmation(text, 1)
        raw = [{'id': 'p1', 'candidates': [text, text], 'prohibited_reviews': [review]}]
        before = copy.deepcopy(raw)
        self.assertEqual(filter_segments(raw)[0]['candidates'], [text])
        self.assertEqual(raw, before)
        with self.assertRaises(ValueError):
            filter_segments([{'id': 'p1', 'candidates': [text], 'prohibited_reviews': [review], 'ignored': True}])
        with self.assertRaises(ValueError):
            filter_segments([{'id': 'p1', 'candidates': [text], 'prohibited_reviews': True}])

    def test_confirmed_preview_reaches_transport_on_mac_and_windows(self):
        for platform in ('posix', 'nt'):
            response = mock.MagicMock()
            response.read.return_value = b'RIFFconfirmed' if platform == 'nt' else b'{"success":true}'
            response.__enter__.return_value = response
            with mock.patch.object(text_studio.os, 'name', platform), mock.patch.object(text_studio, '_read_keychain_api_key', return_value='test'), mock.patch.object(text_studio.urllib.request, 'urlopen', return_value=response) as transport:
                text_studio._tts_preview(SENTENCE, 'default', 'http://gateway', [confirmation(SENTENCE)])
                self.assertEqual(json.loads(transport.call_args.args[0].data)['text'], SENTENCE)
                transport.reset_mock()
                with self.assertRaises(ValueError):
                    text_studio._tts_preview(SENTENCE, 'default', 'http://gateway', [])
                transport.assert_not_called()

    def test_context_cross_unit_confirmation_and_changed_neighbor(self):
        left, right = '🍑支持七天无理由，', '还给大家赠送运费险啊。'
        units = [{'id': 'p1', 'original_text': left}, {'id': 'p2', 'original_text': right}]
        raw = {'variants': [{'segments': [{'id': p['id'], 'text': p['original_text']} for p in units]}]}
        result = validate_variant_result(raw, units, 1, 'g1', 1)
        paragraphs = [{**p, **r} for p, r in zip(units, result['paragraphs'])]
        hit = analyze(left + right)['blocked'][0]
        for p, start in zip(paragraphs, (0, len(left))):
            p['prohibited_reviews'] = [confirmation(p['original_text'], hit={**hit, 'start': -start})]
        payload = {'paragraphs': paragraphs, 'context_groups': [result['group']]}
        frozen = prepare_context_live(payload)
        selected, _ = choose_variant_round(frozen['paragraphs'], frozen['context_groups'], rng=random.Random(0))
        self.assertEqual(semantic_blocks(selected)[0]['text'], left + right)
        response = mock.MagicMock(); response.read.return_value = b'{"success":true}'; response.__enter__.return_value = response
        with mock.patch.object(text_studio, '_read_keychain_api_key', return_value='test'), mock.patch.object(text_studio.urllib.request, 'urlopen', return_value=response) as transport:
            text_studio._tts_preview(right, 'default', 'http://gateway', context_segments=selected, paragraph_id='p2')
            self.assertEqual(json.loads(transport.call_args.args[0].data)['text'], right)
        paragraphs[1]['candidates'][0] += '还有别的权益。'
        with self.assertRaisesRegex(ValueError, 'fully blocked'):
            prepare_context_live(payload)

    def test_save_restore_revoke_and_http_preview_live(self):
        root = Path(tempfile.mkdtemp(prefix='tts-review-test-'))
        p = {'id': 'p1', 'original_text': SENTENCE, 'candidates': [SENTENCE], 'prohibited_reviews': [confirmation(SENTENCE)]}
        saved = text_studio.save_project(root, {'paragraphs': [p]})
        loaded = text_studio.load_project(root, saved['project_id'])
        self.assertEqual(loaded['paragraphs'][0]['prohibited_reviews'], p['prohibited_reviews'])
        live = mock.Mock(); live.start.return_value = {'status': 'starting'}
        with mock.patch.object(text_studio, 'build_live_manager', return_value=live):
            server = text_studio.StudioServer(('127.0.0.1', 0), text_studio.make_handler(root, 'http://gateway'))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def post(path, body):
            req = Request(f'http://127.0.0.1:{server.server_port}{path}', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(req) as response: return json.load(response)
        response = mock.MagicMock(); response.read.return_value = b'{"success":true}'; response.__enter__.return_value = response
        try:
            post('/api/live/start', {'segments': loaded['paragraphs']})
            self.assertEqual(live.start.call_args.args[1][0]['candidates'], [SENTENCE])
            # Patch transport only for Gateway requests; the outer HTTP call still uses real urlopen.
            with mock.patch.object(text_studio, '_read_keychain_api_key', return_value='test'), mock.patch.object(text_studio.urllib.request, 'urlopen', return_value=response) as transport:
                post('/api/tts/preview', {'text': SENTENCE, 'prohibited_reviews': p['prohibited_reviews']})
                self.assertEqual(json.loads(transport.call_args.args[0].data)['text'], SENTENCE)
            loaded['paragraphs'][0]['prohibited_reviews'] = []
            revoked = post('/api/project/save', loaded)['project']
            self.assertEqual(revoked['paragraphs'][0]['prohibited_reviews'], [])
            with self.assertRaises(HTTPError) as error:
                post('/api/live/start', {'segments': revoked['paragraphs']})
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
