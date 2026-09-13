import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def test_rejects_whisper_missing_and_smaller_models(self):
        self.assertEqual(qwen_asr.validate_model(self.model), self.model)
        for config in ({'model_type':'whisper'}, {'model_type':'qwen3_asr', 'thinker_config':{'text_config':{'hidden_size':1024,'num_hidden_layers':28}}}):
            (self.model/'config.json').write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                qwen_asr.validate_model(self.model)
        with self.assertRaises(ValueError):
            qwen_asr.validate_model(self.root/'missing')

    def test_decode_clean_and_never_call_whisper(self):
        ingest = SimpleNamespace(IngestError=RuntimeError, _audio_for_asr=Mock(return_value=self.root/'decoded.wav'), transcribe_mlx=Mock())
        output = {'text':'今天9.9元到手。', 'segments':[{'text':'今天9.9元到手。','start':0,'end':5}]}
        stages = []
        with patch.object(pipeline, '_audio_ingest_module', return_value=ingest), patch.object(pipeline, 'transcribe_qwen', return_value=output) as qwen:
            result = pipeline.transcribe_recording(self.source, self.root, self.model, stages.append)
        self.assertEqual(result, '今天9.9元到手。')
        self.assertEqual(stages, ['recognizing', 'cleaning'])
        qwen.assert_called_once_with(self.root/'decoded.wav', self.model)
        ingest.transcribe_mlx.assert_not_called()

    def test_qwen_error_does_not_fall_back(self):
        ingest = SimpleNamespace(IngestError=RuntimeError, _audio_for_asr=Mock(return_value=self.root/'decoded.wav'), transcribe_mlx=Mock())
        with patch.object(pipeline, '_audio_ingest_module', return_value=ingest), patch.object(pipeline, 'transcribe_qwen', side_effect=ValueError('Qwen inference failed')):
            with self.assertRaisesRegex(pipeline.TranscriptError, 'Qwen inference failed'):
                pipeline.transcribe_recording(self.source, self.root, self.model)
        ingest.transcribe_mlx.assert_not_called()

    def test_empty_output_is_not_published(self):
        ingest = SimpleNamespace(IngestError=RuntimeError, _audio_for_asr=Mock(return_value=self.root/'decoded.wav'))
        with patch.object(pipeline, '_audio_ingest_module', return_value=ingest), patch.object(pipeline, 'transcribe_qwen', return_value={'segments':[]}):
            with self.assertRaises(pipeline.TranscriptError):
                pipeline.transcribe_recording(self.source, self.root, self.model)
