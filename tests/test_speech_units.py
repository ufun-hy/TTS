import json
from pathlib import Path
import tempfile
import unittest

from server.speech_units import speech_units
from server.text_studio import split_paragraphs, save_project, list_projects, load_project


class SpeechUnitWorkflowTests(unittest.TestCase):
    def test_topics_and_complete_facts(self):
        text = '来自云南果园。阳光充足。口感清甜多汁。到手19.9元，一箱5斤，拍两箱共10斤。48小时内发货。坏果请联系客服。'
        units = split_paragraphs(text)
        self.assertGreaterEqual(len(units), 4)
        self.assertEqual(''.join(u['original_text'] for u in units), text)
        self.assertTrue(any('19.9元，一箱5斤，拍两箱共10斤' in u['original_text'] for u in units))
        for unit in units:
            self.assertEqual(text[unit['source_start']:unit['source_end']], unit['original_text'])
            self.assertEqual(unit['source_paragraph_index'], 1)

    def test_long_unpunctuated_fact_and_quotes_are_preserved(self):
        for text in ['。开头。“今天清甜！”这是原话。', '规格' + '特大果' * 100 + '，共5斤19.9元', '如果坏果请联系客服。因此按规则处理。']:
            self.assertEqual(''.join(speech_units(text)), text)
        text = '口感很甜。' * 40
        self.assertTrue(all(len(u) <= 120 for u in speech_units(text)))
        self.assertEqual(''.join(speech_units(text)), text)

    def test_drafts_promote_and_legacy_records_remain_recoverable(self):
        # Keep test output for inspection; no user data or automatic cleanup.
        root = Path(tempfile.mkdtemp(prefix='tts-project-tests-'))
        payload = {'paragraphs': split_paragraphs('到手9.9元。'), 'name': '清理文稿', 'project_kind': 'draft'}
        draft = save_project(root, payload)
        self.assertEqual(list_projects(root), [])
        self.assertEqual(len(list_projects(root, True)), 1)
        saved = save_project(root, {**draft, 'project_kind': 'saved'})
        self.assertEqual(saved['project_id'], draft['project_id'])
        self.assertEqual(len(list_projects(root)), 1)
        legacy = save_project(root, {'project_id': 'model-smoke-20260909', 'paragraphs': [{'original_text': '测试'}]})
        path = root/'runtime/text-studio/projects'/legacy['project_id']/'project.json'
        legacy.pop('project_kind')
        path.write_text(json.dumps(legacy))
        self.assertEqual(len(list_projects(root)), 1)
        self.assertEqual(len(list_projects(root, True)), 2)
        self.assertEqual(load_project(root, legacy['project_id'])['paragraphs'], legacy['paragraphs'])
