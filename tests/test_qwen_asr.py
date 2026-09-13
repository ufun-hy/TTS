import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recording_transcript import pipeline, qwen_asr


class QwenASRTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='qwen-asr-tests-')).resolve()
        self.model = self.root / 'model'
        self.model.mkdir()
        self.config = {'model_type': 'qwen3_asr', 'thinker_config': {
            'text_config': {'hidden_size': 2048, 'num_hidden_layers': 28}}}
        (self.model/'config.json').write_text(json.dumps(self.config))
        for name in ('tokenizer_config.json', 'preprocessor_config.json', 'vocab.json', 'merges.txt', 'model.safetensors'):
            (self.model/name).write_text('fixture')
        self.source = self.root/'real.mp3'
        self.source.write_bytes(b'test')

    def test_rejects_non_qwen_missing_and_smaller_models(self):
        self.assertEqual(qwen_asr.validate_model(self.model), self.model)
        for config in (
            {'model_type': 'other_asr'},
            {'model_type': 'qwen3_asr', 'thinker_config': {'text_config': {'hidden_size': 1024, 'num_hidden_layers': 28}}},
        ):
            (self.model/'config.json').write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                qwen_asr.validate_model(self.model)
        with self.assertRaises(ValueError):
            qwen_asr.validate_model(self.root/'missing')

    def test_decode_clean_and_call_qwen(self):
        decoded = self.root/'decoded.wav'
        output = {'text': '今天9.9元到手。', 'segments': [{'text': '今天9.9元到手。', 'start': 0, 'end': 5}]}
        stages = []
        with patch.object(pipeline, 'decode_audio', return_value=decoded) as decode, \
             patch.object(pipeline, 'transcribe_qwen', return_value=output) as qwen:
            result = pipeline.transcribe_recording(self.source, self.root, self.model, stages.append)
        self.assertEqual(result, '今天9.9元到手。')
        self.assertEqual(stages, ['recognizing', 'cleaning'])
        decode.assert_called_once()
        qwen.assert_called_once_with(decoded, self.model)

    def test_qwen_error_does_not_use_another_backend(self):
        with patch.object(pipeline, 'decode_audio', return_value=self.root/'decoded.wav'), \
             patch.object(pipeline, 'transcribe_qwen', side_effect=ValueError('Qwen inference failed')):
            with self.assertRaisesRegex(pipeline.TranscriptError, 'Qwen inference failed'):
                pipeline.transcribe_recording(self.source, self.root, self.model)

    def test_empty_output_is_not_published(self):
        with patch.object(pipeline, 'decode_audio', return_value=self.root/'decoded.wav'), \
             patch.object(pipeline, 'transcribe_qwen', return_value={'segments': []}):
            with self.assertRaises(pipeline.TranscriptError):
                pipeline.transcribe_recording(self.source, self.root, self.model)


if __name__ == '__main__':
    unittest.main()
