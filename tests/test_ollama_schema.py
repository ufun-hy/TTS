"""Prevent malformed local JSON from becoming a misleading paragraphs error."""
from pathlib import Path
import unittest
from unittest import mock

from server import text_studio
from server import text_studio_http_providers as providers


class OllamaSchemaTests(unittest.TestCase):
    def test_missing_outer_brace_is_not_parsed_as_inner_array(self):
        broken = '{"paragraphs":[{"id":"p0015","candidates":["保持三天"]}]'
        with self.assertRaisesRegex(ValueError, 'JSON'):
            text_studio._extract_json(broken)

    def test_generalization_passes_exact_id_and_candidate_schema(self):
        with mock.patch.object(text_studio, 'run_http_provider', return_value=(
            '{"paragraphs":[{"id":"p0015","candidates":["版本一","版本二","版本三"]}]}', 'qwen3:8b'
        )) as run:
            result = text_studio.generalize_paragraphs([{'id':'p0015','text':'放三天变软'}], 'ollama', 3, '')
        schema=run.call_args.kwargs['output_schema']['properties']['paragraphs']
        self.assertEqual(schema['items']['properties']['id']['enum'], ['p0015'])
        self.assertEqual(schema['items']['properties']['candidates']['minItems'],3)
        self.assertEqual(len(result[0]['candidates']),3)

    def test_http_uses_schema_and_reports_length_limit(self):
        schema={'type':'object','required':['paragraphs']}
        with mock.patch.object(providers,'_post_json',return_value={'message':{'content':'{}'},'done_reason':'length'}) as post:
            with self.assertRaisesRegex(ValueError,'长度限制'):
                providers.run_http_provider('ollama','prompt','qwen3:8b',Path('.'),output_schema=schema)
        self.assertEqual(post.call_args.args[1]['format'],schema)


if __name__=='__main__':
    unittest.main()
