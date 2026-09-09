import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from server import text_studio
from server import text_studio_models as models


class TextStudioModelsTest(unittest.TestCase):
    def test_agy_model_override_before_stdin_marker(self):
        with mock.patch.dict(os.environ, {'TTS_TEXT_STUDIO_AGY_CMD': 'agy --model old --mode plan --output-format json --print -'}):
            self.assertEqual(models.provider_command('agy', 'gemini-3.7-flash-high'),
                             ['agy', '--model', 'gemini-3.7-flash-high', '--mode', 'plan', '--output-format', 'json', '--print', '-'])

    def test_agy_prompt_is_argv_without_shell_or_stdin(self):
        prompt = '原稿 $HOME `echo example` "原文"'
        self.assertEqual(models.agy_prompt_command(['agy','--print','-'], prompt), ['agy','--print',prompt])
        proc = models.subprocess.CompletedProcess([], 0, '{"status":"SUCCESS","response":"{\"paragraphs\":[]}"}', '')
        proc.stdout = json.dumps({'status':'SUCCESS','response':'{"paragraphs":[]}'})
        with mock.patch.object(text_studio.shutil, 'which', return_value='agy'), mock.patch.object(text_studio.subprocess, 'run', return_value=proc) as run:
            text_studio._run_provider('agy', prompt, 240)
        self.assertEqual(run.call_args.args[0][-1], prompt)
        self.assertIsNone(run.call_args.kwargs['input'])

    def test_agy_catalog_filters_gemini_and_preserves_ids(self):
        proc = models.subprocess.CompletedProcess([], 0, 'gemini-test\tGemini Test\nclaude-test\tClaude Test\n', '')
        with mock.patch.object(models.subprocess, 'run', return_value=proc), mock.patch.object(Path, 'is_file', return_value=False):
            result = models.discover_agy_models(['agy'], Path.cwd())
        self.assertEqual(result['models'][0]['id'], 'gemini-test')
        self.assertEqual(len(result['models']), 2)

    def test_agy_success_and_permission_denied_empty_response(self):
        envelope = {'status': 'SUCCESS', 'response': '{"paragraphs":[{"id":"p1","candidates":["改写"]}]}'}
        with mock.patch.object(text_studio.shutil, 'which', return_value='agy'), mock.patch.object(text_studio.subprocess, 'run', return_value=models.subprocess.CompletedProcess([], 0, json.dumps(envelope), '')):
            result = text_studio.generalize_paragraphs([{'id':'p1','original_text':'原稿'}], 'agy', 1, '', model='gemini-test')
            self.assertEqual(result[0]['candidates'], ['改写'])
        envelope.update(response='', denied_actions=[{'action':'command'}])
        with mock.patch.object(text_studio.shutil, 'which', return_value='agy'), mock.patch.object(text_studio.subprocess, 'run', return_value=models.subprocess.CompletedProcess([], 0, json.dumps(envelope), '')):
            with self.assertRaisesRegex(RuntimeError, '权限被拒绝'):
                text_studio._run_provider('agy', '原稿', 240, model='gemini-test')

    def test_gemini_command_overrides_and_discovery_options(self):
        with mock.patch.dict(os.environ, {'TTS_TEXT_STUDIO_GEMINI_CMD': 'gemini --model old --output-format json'}):
            self.assertEqual(models.provider_command('gemini', 'flash'), ['gemini', '--output-format', 'json', '--model', 'flash'])
            self.assertEqual(models.gemini_command(models.provider_command('gemini'), discover=True), ['gemini', '--model', 'old', '--acp'])
        with mock.patch.dict(os.environ, {'TTS_TEXT_STUDIO_GEMINI_CMD': 'gemini --model=old --output-format=json'}):
            self.assertEqual(models.provider_command('gemini', 'pro'), ['gemini', '--output-format=json', '--model', 'pro'])

    def test_gemini_json_envelope_and_error(self):
        envelope = {'response': '{"paragraphs":[{"id":"p1","candidates":["改写"]}]}',
                    'stats': {'models': {'gemini-test': {}}}}
        with mock.patch.object(text_studio.shutil, 'which', return_value='gemini'), mock.patch.object(text_studio.subprocess, 'run', return_value=models.subprocess.CompletedProcess([], 0, json.dumps(envelope), '')) as run:
            result = text_studio.generalize_paragraphs([{'id':'p1','original_text':'原稿'}], 'gemini', 1, '', model='flash')
            self.assertEqual(result[0]['candidates'], ['改写'])
            self.assertIn('flash', run.call_args.args[0])
        with mock.patch.object(text_studio.shutil, 'which', return_value='gemini'), mock.patch.object(text_studio.subprocess, 'run', return_value=models.subprocess.CompletedProcess([], 41, '', '{"error":{"message":"Authentication required"}}')):
            with self.assertRaisesRegex(RuntimeError, 'Authentication required'):
                text_studio._run_provider('gemini', '原稿', 240, model='flash')

    def test_gemini_acp_catalog_and_auth_error(self):
        from contextlib import contextmanager
        requests = []
        @contextmanager
        def rpc(*args):
            def request(method, params):
                requests.append((method, params))
                if method == 'initialize': return {'protocolVersion': 1}
                return {'models': {'currentModelId': 'auto', 'availableModels': [{'modelId':'gemini-test','name':'Test'}]}}
            yield request, None
        with mock.patch.object(models, '_rpc_process', rpc):
            result = models.discover_gemini_models(['gemini','--output-format','json'], Path.cwd())
        self.assertEqual(result['models'][0]['id'], 'gemini-test')
        self.assertEqual(result['default_model'], 'auto')
        self.assertEqual([x[0] for x in requests], ['initialize','session/new'])
        with mock.patch.object(models, 'discover_gemini_models', side_effect=RuntimeError('Authentication required')):
            result = models.list_models('gemini', Path.cwd())
        self.assertEqual(result['error'], 'Authentication required')
        self.assertEqual(result['models'], [])

    def test_gemini_project_roundtrip(self):
        root = Path(tempfile.mkdtemp(prefix='text-studio-gemini-project-test-'))
        p = text_studio.save_project(root, {'paragraphs': [], 'provider': 'gemini', 'model': 'flash'})
        loaded = text_studio.load_project(root, p['project_id'])
        self.assertEqual((loaded['provider'], loaded['model']), ('gemini','flash'))

    def test_default_command_unchanged_and_explicit_model_overrides_only_model(self):
        command = 'codex exec -m old -c model="old-config" -c model_reasoning_effort="low" --color never -'
        with mock.patch.dict(os.environ, {"TTS_TEXT_STUDIO_CODEX_CMD": command}):
            self.assertEqual(models.provider_command('codex'), models.shlex.split(command))
            self.assertEqual(models.provider_command('codex', 'selected-model'), [
                'codex', 'exec', '--model', 'selected-model', '-c', 'model_reasoning_effort=low', '--color', 'never', '-'])
        with mock.patch.dict(os.environ, {"TTS_TEXT_STUDIO_CODEX_CMD": 'codex exec --model=old --config=model="old" -'}):
            self.assertEqual(models.provider_command('codex', 'new'), ['codex', 'exec', '--model', 'new', '-'])

    def test_validation_and_custom_provider_do_not_silently_ignore_selection(self):
        for value in ['--help', 'model; echo secret', {}, 'a' * 201]:
            with self.assertRaises(ValueError):
                models.validate_model(value)
        self.assertEqual(models.validate_model(None), '')
        with mock.patch.dict(os.environ, {'TTS_TEXT_STUDIO_CHATGPT_CMD': 'custom-chat --quiet'}):
            self.assertEqual(models.provider_command('chatgpt'), ['custom-chat', '--quiet'])
            with self.assertRaisesRegex(ValueError, '不支持模型选择'):
                models.provider_command('chatgpt', 'selected')

    def test_project_roundtrip_and_legacy_default(self):
        root = Path(tempfile.mkdtemp(prefix='text-studio-model-project-test-'))
        p = text_studio.save_project(root, {'paragraphs': [], 'model': 'selected-model'})
        self.assertEqual(text_studio.load_project(root, p['project_id'])['model'], 'selected-model')
        old = text_studio.save_project(root, {'paragraphs': []})
        self.assertEqual(old['model'], '')

    def test_selected_model_reaches_every_batch_without_prompt_changes(self):
        paragraphs = [{'id': 'p1', 'original_text': '原稿一'}, {'id': 'p2', 'original_text': '原稿二'}]
        replies = [{'paragraphs': [{'id': p['id'], 'candidates': ['结果']}]} for p in paragraphs]
        with mock.patch.object(text_studio, '_run_provider', side_effect=replies) as run:
            text_studio.generalize_paragraphs(paragraphs, 'codex', 1, '', batch_size=1, model='selected-model')
        self.assertEqual(len(run.call_args_list), 2)
        for index, call in enumerate(run.call_args_list):
            self.assertEqual(call.kwargs, {'model': 'selected-model'})
            self.assertEqual(call.args[1], text_studio._build_prompt([paragraphs[index]], 1, ''))

    def test_provider_failure_names_selected_model_and_preserves_reason(self):
        proc = models.subprocess.CompletedProcess([], 1, '', 'prompt noise\nERROR: model is not available for this account')
        with mock.patch.object(text_studio.shutil, 'which', return_value='codex'), mock.patch.object(text_studio.subprocess, 'run', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, '模型 unavailable 调用失败：model is not available for this account'):
                text_studio._run_provider('codex', 'prompt', 240, model='unavailable')

    def test_rpc_handshake_pagination_and_effective_default(self):
        # Keep fixtures under a distinct temporary directory, without deleting user files.
        root = Path(tempfile.mkdtemp(prefix='text-studio-model-rpc-test-'))
        cli = root / 'codex'
        cli.write_text('#!' + sys.executable + '\n' + '''import json,sys
initialized=False
for line in sys.stdin:
 request=json.loads(line); method=request['method']
 if method=='initialized': initialized=True; continue
 if method=='initialize': result={}
 elif not initialized: raise RuntimeError('missing initialized notification')
 elif method=='config/read': result={'config':{'model':'configured-not-listed'}}
 elif request['params'].get('cursor') is None:
  result={'data':[{'model':'first','displayName':'First','isDefault':True}], 'nextCursor':'page2'}
 else:
  result={'data':[{'model':'second'},{'model':'hidden','hidden':True}], 'nextCursor':None}
 print(json.dumps({'id':request['id'],'result':result}),flush=True)
''')
        cli.chmod(0o700)
        result = models.discover_codex_models([str(cli), 'exec', '-'], root)
        self.assertEqual([m['id'] for m in result['models']], ['first', 'second'])
        self.assertEqual(result['default_model'], 'configured-not-listed')
        with mock.patch.object(models, 'discover_codex_models', side_effect=TimeoutError('model discovery timeout')):
            result = models.list_models('codex', root)
            self.assertEqual(result['error'], 'model discovery timeout')
            self.assertEqual(result['models'], [])


if __name__ == '__main__':
    unittest.main()
