import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from recording_transcript.cleaner import clean_transcript
from recording_transcript.results import load_result, list_results
from server.recording_transcript import TranscriptJobStore, TranscriptJob, RecordingTranscriptServer, make_handler as recording_handler
from server.text_studio import StudioServer, make_handler as studio_handler, split_paragraphs


@contextmanager
def serving(server_class, handler):
    server = server_class(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TranscriptHandoffTests(unittest.TestCase):
    def test_cleaned_job_survives_restart_and_is_available_to_studio(self):
        root = Path(tempfile.mkdtemp(prefix='transcript-handoff-tests-'))
        upload = root/'upload'/'input.mp3'
        upload.parent.mkdir()
        upload.write_bytes(b'test audio placeholder')
        job = TranscriptJob('a'*32, '清理验收.mp3', 22, upload)
        expected = clean_transcript('嗯今天呢我们主要给大家介绍一下这个产品然后这个产品操作简单')
        with patch('server.recording_transcript.transcribe_recording', return_value=expected):
            TranscriptJobStore(root, root/'model')._run(job)
        self.assertEqual(job.stage, 'completed')
        self.assertEqual(load_result(root, job.job_id)['text'], expected)
        self.assertEqual(TranscriptJobStore(root, root/'model').get(job.job_id).text, expected)
        original = (root/'runtime/recording-transcript/results'/f'{job.job_id}.json').read_bytes()
        with serving(RecordingTranscriptServer, recording_handler(root, root/'model')) as url:
            with urlopen(url+'/api/transcript/jobs/'+job.job_id) as response:
                self.assertEqual(json.load(response)['text'], expected)
        with serving(StudioServer, studio_handler(root, 'http://127.0.0.1:1')) as url:
            with urlopen(url+'/api/transcript/results') as response:
                self.assertEqual(json.load(response)['results'][0]['job_id'], job.job_id)
            with urlopen(url+'/api/transcript/result?job_id='+job.job_id) as response:
                text = json.load(response)['result']['text']
                self.assertEqual(text, expected)
                self.assertTrue(split_paragraphs(text))
        self.assertEqual((root/'runtime/recording-transcript/results'/f'{job.job_id}.json').read_bytes(), original)

    def test_invalid_or_failed_results_are_not_importable(self):
        root = Path(tempfile.mkdtemp(prefix='transcript-invalid-tests-'))
        with self.assertRaises(ValueError):
            load_result(root, '../outside')
        upload = root/'upload'/'input.mp3'
        upload.parent.mkdir()
        upload.write_bytes(b'placeholder')
        job = TranscriptJob('b'*32, '失败.mp3', 11, upload)
        with patch('server.recording_transcript.transcribe_recording', side_effect=RuntimeError('ASR failed')):
            TranscriptJobStore(root, root/'model')._run(job)
        self.assertEqual(job.stage, 'failed')
        self.assertEqual(list_results(root), [])
