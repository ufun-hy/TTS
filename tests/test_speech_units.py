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
        self.assertEqual(speech_units(text), ['口感很甜。'] * 40)
        self.assertEqual(''.join(speech_units(text)), text)

    def test_strong_boundaries_never_recombine_sentences(self):
        samples = {
            '这个石榴汁水很多。吃起来很甜。籽也比较软。': ['这个石榴汁水很多。', '吃起来很甜。', '籽也比较软。'],
            '这个石榴甜不甜？真的很甜。籽也很软！': ['这个石榴甜不甜？', '真的很甜。', '籽也很软！'],
            '先拍下；再付款;好吃!真的吗?': ['先拍下；', '再付款;', '好吃!', '真的吗?'],
            '请联系客服。因此按规则处理。': ['请联系客服。', '因此按规则处理。'],
            '汁水很多\n吃起来很甜\n籽也软': ['汁水很多', '吃起来很甜', '籽也软'],
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(speech_units(text), expected)

    def test_price_quantity_and_specification_stay_together(self):
        text = '今天19.9到手6个，每个差不多四两左右。'
        self.assertEqual(speech_units(text), [text])
        text = '今天活动价格只要19.9元到手6个，每个差不多四两左右，另外每一箱再赠送两袋试吃装。'
        self.assertGreater(len(text), 40)
        self.assertEqual(speech_units(text), [text])

    def test_long_comma_sentence_uses_short_natural_units(self):
        text = '姐妹们这个石榴是四川会理发过来的，果子个头比较大，汁水也比较足，吃起来很甜，而且籽还特别软。'
        units = speech_units(text)
        self.assertGreater(len(units), 1)
        self.assertTrue(all(15 <= len(unit) <= 35 for unit in units))
        self.assertEqual(''.join(units), text)
        self.assertTrue(all(unit.endswith(('，', '。')) for unit in units))
        self.assertTrue(any('吃起来很甜，而且籽还特别软。' in unit for unit in units))

    def test_condition_and_numeric_or_quoted_commas_are_protected(self):
        samples = [
            '姐妹们今天这批石榴是果园新鲜采摘的，每箱1,299克，每个差不多四两左右，大家看清规格再拍。',
            '姐妹们今天的果子个头特别大而且汁水充足，如果收到后发现坏果，请先拍照联系客服，按照平台规则处理。',
            '姐妹们今天这批果子是果园直接采摘的，包装标注“优选大果，每箱六个”，大家看清楚规格再拍。',
        ]
        for text in samples:
            units = speech_units(text)
            self.assertEqual(''.join(units), text)
        self.assertTrue(any('1,299克' in unit for unit in speech_units(samples[0])))
        self.assertTrue(any('如果收到后发现坏果，请先拍照联系客服，按照平台规则处理。' in unit for unit in speech_units(samples[1])))
        self.assertTrue(any('“优选大果，每箱六个”' in unit for unit in speech_units(samples[2])))

    def test_shipping_and_after_sales_conditions_remain_whole(self):
        samples = [
            '这批订单按照付款时间先后顺序安排发货，普通地区由快递配送上门，偏远地区的运费以订单页面为准。',
            '如果收到商品之后发现破损或者坏果问题，请及时拍照并联系客服，售后会按照平台规则核实后处理退款。',
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertGreater(len(text), 40)
                self.assertEqual(speech_units(text), [text])

    def test_comma_split_offsets_still_refer_to_original_source(self):
        paragraph = '姐妹们这个石榴是四川会理发过来的，果子个头比较大，汁水也比较足，吃起来很甜，而且籽还特别软。'
        source = paragraph + '\n\n' + paragraph
        units = split_paragraphs(source)
        self.assertGreater(len(units), 2)
        for unit in units:
            self.assertIn(unit['source_paragraph_index'], (1, 2))
            self.assertEqual(paragraph[unit['source_start']:unit['source_end']], unit['original_text'])
        self.assertEqual(''.join(unit['original_text'] for unit in units), paragraph * 2)

    def test_existing_project_is_not_resegmented_on_load_or_save(self):
        root = Path(tempfile.mkdtemp(prefix='tts-unit-compat-tests-'))
        paragraphs = [{'id': 'p0001', 'index': 1, 'original_text': '果子很甜。籽也软。',
                       'candidates': ['候选一。完整保留。', '候选二。'], 'selectedIndex': 1,
                       'editedText': '人工修正到手19.9元。'}]
        saved = save_project(root, {'paragraphs': paragraphs, 'replacement_history': [{'kind': 'product_fact_meta'}]})
        loaded = load_project(root, saved['project_id'])
        self.assertEqual(loaded['paragraphs'], paragraphs)
        self.assertEqual(save_project(root, loaded)['paragraphs'], paragraphs)
        self.assertEqual(loaded['replacement_history'], saved['replacement_history'])
        self.assertEqual(len(split_paragraphs(paragraphs[0]['original_text'])), 2)

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
        legacy = save_project(root, {'project_id': '20260909-175133-bb59e64c', 'name': '正式名字也可能来自自动保存', 'paragraphs': [{'original_text': '测试', 'candidates': ['已生成的缓存']}]})
        path = root/'runtime/text-studio/projects'/legacy['project_id']/'project.json'
        legacy.pop('project_kind')
        path.write_text(json.dumps(legacy))
        original_bytes = path.read_bytes()
        self.assertEqual(len(list_projects(root)), 1)
        self.assertEqual(len(list_projects(root, True)), 2)
        self.assertEqual(path.read_bytes(), original_bytes)
        saved_legacy = save_project(root, {**load_project(root, legacy['project_id']), 'project_kind': 'saved'})
        self.assertEqual(len(list_projects(root)), 2)
        self.assertEqual(saved_legacy['paragraphs'], legacy['paragraphs'])
        self.assertEqual(load_project(root, legacy['project_id'])['paragraphs'], legacy['paragraphs'])
