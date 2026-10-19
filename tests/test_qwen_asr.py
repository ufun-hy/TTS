import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import ModuleType, SimpleNamespace
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

    def test_different_callers_share_model_and_gpu_thread_even_after_error(self):
        loads, generations = [], []
        mlx = ModuleType('mlx')
        core = ModuleType('mlx.core')
        core.gpu = object()
        core.default_device = lambda: core.gpu
        mlx.core = core
        stt = ModuleType('mlx_audio.stt')
        output = SimpleNamespace(text='测试语音。', segments=[{'text': '测试语音。', 'start': 0, 'end': 1}])

        def generate(audio, **kwargs):
            generations.append(threading.current_thread())
            if audio == 'bad.wav':
                raise RuntimeError('bad audio')
            return output

        def load(_path):
            loads.append(threading.current_thread())
            return SimpleNamespace(generate=generate)

        stt.load = load
        callers, results, errors = [], [], []

        def call(path):
            callers.append(threading.current_thread())
            try:
                results.append(qwen_asr.transcribe(Path(path), self.model))
            except Exception as exc:
                errors.append(exc)

        qwen_asr._load_model.cache_clear()
        try:
            with patch.dict('sys.modules', {'mlx': mlx, 'mlx.core': core, 'mlx_audio.stt': stt}), \
                 patch.object(qwen_asr, 'readiness', return_value={'asr_ready': True}):
                # Upload-style short-lived threads, including a failed request.
                for index, path in enumerate(('first.wav', 'bad.wav', 'next.wav')):
                    caller = threading.Thread(target=call, args=(path,), name=f'upload-{index}')
                    caller.start()
                    caller.join(3)
                    self.assertFalse(caller.is_alive())
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(call, ('parallel-a.wav', 'parallel-b.wav')))
            self.assertEqual(len(loads), 1)
            self.assertEqual(len(generations), 5)
            self.assertTrue(all(thread is loads[0] for thread in generations))
            self.assertNotIn(loads[0], callers)
            self.assertEqual(len(results), 4)
            self.assertTrue(all(result['text'] == output.text for result in results))
            self.assertEqual([str(exc) for exc in errors], ['bad audio'])
        finally:
            qwen_asr._load_model.cache_clear()

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
