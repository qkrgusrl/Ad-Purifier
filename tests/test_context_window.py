"""Gemma 문맥 projection 계약. 실제 모델·서버·네트워크를 사용하지 않는다."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from local_llm import engine, prompts


class ContextWindowTests(unittest.TestCase):
    def data(self, target='<p>😀 가입 <b>안내</b> &amp; 원문</p>', context=None):
        return {'candidate_id': 'owner', 'html': target,
                'context': context if context is not None else '앞' * 70 + target + '뒤' * 70}

    def project(self, data):
        return prompts.prepare_input('verify', data, 'v15')

    def test_unique_anchor_limits_each_side_and_preserves_entire_target(self):
        data = self.data(target='<p>' + '대상😀' * 100 + '</p>')
        before = copy.deepcopy(data)
        projected = self.project(data)
        self.assertEqual(projected['context_before'], '앞' * 50)
        self.assertEqual(projected['context_after'], '뒤' * 50)
        self.assertTrue(projected['context_status'])
        units = [u for u in projected['source_units'] if u['source_field'] == 'html']
        self.assertEqual(''.join(u['text'] for u in units), data['html'])
        self.assertEqual(data, before)
        self.assertNotIn('앞' * 51, json.dumps(projected, ensure_ascii=False))
        self.assertNotIn('뒤' * 51, json.dumps(projected, ensure_ascii=False))

    def test_visible_context_decodes_entities_joins_inline_and_ignores_script_style(self):
        target = '<a title="속성 대상">가입</a>'
        data = self.data(target, '<script>HIDDEN_SCRIPT</script><style>HIDDEN_STYLE</style>'
                         '<span>가&amp;</span><b>😀나</b>' + target + '<i>다&#x1F600;</i>라')
        projected = self.project(data)
        self.assertEqual(projected['context_before'], '가&😀나')
        self.assertEqual(projected['context_after'], '다😀라')
        rendered = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn('HIDDEN_SCRIPT', rendered)
        self.assertNotIn('HIDDEN_STYLE', rendered)
        self.assertIn(target, ''.join(u['text'] for u in projected['source_units']))

    def test_duplicate_and_missing_raw_anchor_are_explicit_without_guessing(self):
        target = '<p>같은 문구</p>'
        unique = self.project(self.data(target, '앞' + target + '뒤'))
        duplicate = self.project(self.data(target, '앞' + target + '중간' + target + '뒤'))
        # 표시 텍스트가 같아도 raw HTML anchor로 간주하면 안 된다.
        missing = self.project(self.data(target, '<div>' + '같은 문구' * 40 + '</div>'))
        statuses = [p['context_status'] for p in (unique, duplicate, missing)]
        self.assertTrue(all(isinstance(s, str) and s for s in statuses))
        self.assertEqual(len(set(statuses)), 3)
        for projected in (duplicate, missing):
            self.assertEqual(projected['context_before'], '')
            self.assertEqual(projected['context_after'], '')

    def test_unanchored_long_context_never_reaches_model_projection(self):
        data = self.data(context='REMOTE_CONTEXT_SENTINEL' * 30)
        projected = self.project(data)
        self.assertNotIn('REMOTE_CONTEXT_SENTINEL', json.dumps(projected, ensure_ascii=False))
        self.assertEqual(projected['context_before'], '')
        self.assertEqual(projected['context_after'], '')
        # raw anchor가 없으면 길이와 무관하게 위치 문맥을 비운다.
        short = self.project(self.data(context='예방 교육에서 인용한 문구입니다.'))
        self.assertEqual(short['context_before'], '')
        self.assertEqual(short['context_after'], '')
        self.assertFalse(any(u['selectable'] for u in short['source_units'] if u['source_field'] == 'context'))

    def test_short_unanchored_context_has_no_positional_context(self):
        projected = self.project(self.data(context='짧은 예방교육 문맥'))
        self.assertEqual(projected['context_before'], '')
        self.assertEqual(projected['context_after'], '')

    def test_display_last_declaration_wins_for_visible_context(self):
        target = '<a>대상</a>'
        projected = self.project(self.data(target,
            '<span style="display:none;display:block">보이는앞</span>' + target + '뒤'))
        self.assertEqual(projected['context_before'], '보이는앞')
        self.assertEqual(projected['context_after'], '뒤')

    def test_invalid_display_and_commented_none_keep_context_hidden(self):
        target = '<a>대상</a>'
        for style in ('display:none;display:bogus', 'display:/*주석*/none'):
            with self.subTest(style=style):
                projected = self.project(self.data(target,
                    '<span style="' + style + '">숨긴앞</span>' + target))
                self.assertEqual(projected['context_before'], '')

    def test_visibility_visible_child_overrides_hidden_parent(self):
        target = '<a>대상</a>'
        projected = self.project(self.data(target,
            '<div style="visibility:hidden">숨긴앞<span style="visibility:visible">보이는앞</span>숨긴뒤</div>'
            + target + '뒤'))
        self.assertEqual(projected['context_before'], '보이는앞')
        self.assertEqual(projected['context_after'], '뒤')

    def test_original_html_units_offsets_evidence_and_digest_survive_projection(self):
        data = self.data()
        projected = self.project(data)
        original_units = [u for u in prompts.source_units(data) if u['source_field'] == 'html']
        expected = [{k: u[k] for k in ('id', 'source_field', 'text', 'selectable')} for u in original_units]
        actual = [{k: u[k] for k in expected[0]} for u in projected['source_units'] if u['source_field'] == 'html']
        self.assertEqual(actual, expected)
        chosen = next(u for u in original_units if u['selectable'])
        resolved = engine.resolve_sentence_choice(data, {'verdict': 'ad', 'evidence_id': chosen['id']}, 'v15')
        self.assertEqual((resolved['start_char'], resolved['end_char']), (chosen['start_char'], chosen['end_char']))
        self.assertEqual(resolved['evidence'], data['html'][chosen['start_char']:chosen['end_char']])
        digest = hashlib.sha256(json.dumps({k: data[k] for k in ('html', 'context')}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(resolved['source_sha256'], digest)

    def test_context_ids_are_unselectable_in_schema_and_rejected_for_all_verdicts(self):
        data = self.data(context='<p>참고 문맥</p>')
        projected = self.project(data)
        allowed = prompts.schema_for('verify', projected)['properties']['evidence_id']['enum']
        with self.subTest(contract='schema'):
            self.assertFalse(any(i.startswith('c') for i in allowed))
        for verdict in ('ad', 'clean', 'uncertain'):
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                engine.resolve_sentence_choice(data, {'verdict': verdict, 'evidence_id': 'c0001'}, 'v15')

    def test_verify_extract_and_preflight_share_projection_preserving_original_input(self):
        data = self.data()
        server = object.__new__(engine.LocalServer)
        server.ctx_size, server.cache_prompt = 4096, None
        templates, generations = [], []

        def request(path, payload=None, **kwargs):
            if path == '/apply-template':
                templates.append(json.loads(payload['messages'][1]['content']))
                return {'prompt': 'fake template'}
            if path == '/tokenize':
                return {'tokens': [1]}
            self.assertEqual(path, '/v1/chat/completions')
            generations.append(json.loads(payload['messages'][1]['content']))
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"verdict":"ad","evidence_id":"h0001"}'}}]}

        with patch.object(server, 'request', side_effect=request):
            verify = server.infer('verify', data)
            extract = server.infer('extract', {'blocks': [{'block_id': data['candidate_id'], 'html': data['html'], 'context': data['context']}]})
        self.assertEqual((verify['status'], extract['status']), ('ok', 'ok'))
        expected = self.project(data)
        self.assertEqual(templates, [expected, expected])
        self.assertEqual(generations, [expected, expected])
        self.assertEqual(verify['input'], data)
        self.assertEqual(extract['block_calls'][0]['input'], data)
        self.assertEqual(expected['context_before'], '앞' * 50)
        self.assertEqual(expected['context_after'], '뒤' * 50)


if __name__ == '__main__':
    unittest.main()
