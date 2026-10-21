"""공통 원문 범위/분할/저신뢰 연결 회귀. 모델과 네트워크는 실행하지 않는다."""
import copy
import html
import unittest
from unittest.mock import patch, MagicMock

from detector.inspection import project, split_row
from detector.ai import analyze, digest, matching_recovery_text, valid_binding
from local_llm.engine import resolve_sentence_choice
from local_llm.prompts import prepare_input, source_units


def row(raw, text, **extra):
    return dict(source_id='s', source_html=raw, source_context='<p>예방 교육</p>' + raw,
                tag='p', text=text, evidence_text=text, url='https://fixture.invalid',
                selector='p', location='p', technique='POSTED', signals=[],
                verdict='borderline', decided_by='rules', **extra)


class InspectionUnitTests(unittest.TestCase):
    def unit(self, r):
        p = project(r)
        return split_row(r, p, [{'start_char': 0, 'end_char': len(p['text'])}])[0]

    def test_own_text_excludes_child_but_retains_each_owned_raw_span(self):
        r = row('<p>직접 &amp; 문구<strong>다른 소유 광고</strong> 뒤 문구</p>', '직접 & 문구 뒤 문구')
        u = self.unit(r)
        inp = dict(candidate_id='s', html=u['source_html'], context=u['source_context'],
                   target_spans=u['inspection']['target_spans'])
        rendered = prepare_input('verify', inp, 'v15')
        self.assertEqual([x['text'] for x in rendered['source_units']], ['직접 &amp; 문구', ' 뒤 문구'])
        for unit in rendered['source_units']:
            binding = resolve_sentence_choice(inp, {'verdict': 'clean', 'evidence_id': unit['id']})
            self.assertTrue(valid_binding(binding, inp, 's'))
            self.assertTrue(matching_recovery_text(binding, u))
        bad = dict(binding, start_char=r['source_html'].index('다른'), end_char=r['source_html'].index('</strong>'))
        bad['evidence'] = r['source_html'][bad['start_char']:bad['end_char']]
        self.assertFalse(matching_recovery_text(bad, u))

    def test_hidden_descendants_and_entities_use_exact_original_positions(self):
        r = row('<p>앞<b>인용 &amp; 문구</b>뒤</p>', '앞인용 & 문구뒤')
        u = self.unit(r)
        self.assertEqual(u['inspection']['mode'], 'text')
        pieces = [r['source_html'][s:e] for s, e in u['inspection']['target_spans']]
        self.assertEqual(html.unescape(''.join(pieces)), r['text'])

    def test_attribute_unit_does_not_borrow_another_attribute_or_body(self):
        r = row('<img alt="불법 &amp; 거래" title="안내">', '불법 & 거래', attr='alt')
        r['tag'] = 'img'
        u = self.unit(r)
        spans = u['inspection']['target_spans']
        self.assertEqual(r['source_html'][spans[0][0]:spans[0][1]], '불법 &amp; 거래')
        with self.assertRaises(ValueError):
            project({**r, 'source_html': '<img alt="x" alt="불법 &amp; 거래">'})

    def test_split_preserves_entity_and_source_tail_with_original_offsets(self):
        r = row('<p>가나다 &amp; 마지막</p>', '가나다 & 마지막')
        p = project(r)
        units = split_row(r, p, [{'start_char': 0, 'end_char': 6},
                               {'start_char': 6, 'end_char': len(p['text'])}])
        self.assertEqual(''.join(u['text'] for u in units), p['text'])
        self.assertEqual(units[-1]['text'], '마지막')
        self.assertEqual([u['source_id'] for u in units], ['s#unit-1', 's#unit-2'])
        with self.assertRaises(ValueError):
            split_row(r, p, [{'start_char': 0, 'end_char': 5}])

    def test_ranges_and_context_are_bound_to_this_unit(self):
        r = row('<p>앞 문장. 검사 대상. 뒤 문장.</p>', '앞 문장. 검사 대상. 뒤 문장.')
        data = dict(candidate_id='s', html=r['source_html'], context=r['source_context'],
                    target_spans=[[10, 16]])
        rendered = prepare_input('verify', data, 'v15')
        self.assertIn('앞 문장.', rendered['context_before'])
        self.assertIn('뒤 문장.', rendered['context_after'])
        for spans in ([[0, True]], [[4, 999]], [[8, 10], [5, 7]]):
            with self.assertRaises(ValueError):
                source_units({**data, 'target_spans': spans})

    def test_whitespace_only_split_is_recorded_as_empty_not_lost(self):
        r = row('<p>  본문  </p>', '본문')
        units = split_row(r, project(r), [{'start_char': 0, 'end_char': 2},
                                         {'start_char': 2, 'end_char': 4},
                                         {'start_char': 4, 'end_char': 6}])
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['text'], '본문')
        self.assertEqual(units[0]['inspection']['normalization_empty_ranges'], [[0, 2], [4, 6]])

    def run_flow(self, text, probabilities, verdicts=None, split=None, context=None):
        r = row('<p>' + html.escape(text) + '</p>', text)
        if context is not None:
            r['context'] = context
        traces = {'laya': [], 'gemma': []}
        class Runner:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def plan_batch(self, texts, questions, **kwargs):
                return [{'segments': ([{'start_char': 0, 'end_char': split},
                                       {'start_char': split, 'end_char': len(t)}] if split else
                                      [{'start_char': 0, 'end_char': len(t)}])} for t in texts]
            def predict_batch(self, texts, *args, **kwargs):
                traces['laya'].extend(texts)
                return [{'answers': {'is_illegal_ad': {'noul': probabilities[t]}}} for t in texts]
        class Client:
            def __init__(self, *args, **kwargs): self.event = kwargs['on_event']
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def verify(self, item):
                traces['gemma'].append(copy.deepcopy(item))
                units = source_units(item)
                value = ''.join(html.unescape(u['text']) for u in units)
                parsed = resolve_sentence_choice(item, {'verdict': (verdicts or {}).get(value, 'clean'),
                                                        'evidence_id': units[0]['id']})
                rid = str(len(traces['gemma']))
                for name in ('request_start', 'request_end', 'item_end'):
                    self.event(dict(event=name, candidate_id=item['candidate_id'], request_id=rid,
                                    request_count=1, status='ok', completion_received=name != 'request_start'))
                return {'status': 'ok', 'results': [{**parsed, 'candidate_id': item['candidate_id'],
                        'status': 'ok', 'needs_review': parsed['verdict'] == 'uncertain',
                        'call': {'status': 'ok', 'request_count': 1}}]}
        with patch('detector.ai.LayaRunner', Runner), patch('local_llm.client.GemmaClient', Client):
            stats = analyze([r], [], 'unused', True)
        return r, stats, traces

    def test_each_split_has_own_probability_only_low_confidence_reaches_gemma(self):
        r, stats, traces = self.run_flow('정상 문구. 애매 문구.', {'정상 문구.': .05, '애매 문구.': .55}, split=7)
        self.assertEqual(len(traces['gemma']), 1)
        self.assertEqual(r['verdict'], 'clean')
        self.assertEqual(len(r['inspection_units']), 2)
        self.assertNotIn('ml_prob', r)
        self.assertFalse(stats['telemetry']['audit_degraded'])
        self.assertEqual(stats['telemetry']['gemma']['remaining'], 0)

    def test_one_uncertain_split_prevents_whole_source_clean(self):
        r, stats, traces = self.run_flow('정상 문구. 애매 문구.', {'정상 문구.': .05, '애매 문구.': .55},
                                        {'애매 문구.': 'uncertain'}, split=7)
        self.assertEqual(r['verdict'], 'uncertain')
        self.assertEqual(len(traces['gemma']), 1)

    def test_high_confidence_split_never_calls_gemma(self):
        r, stats, traces = self.run_flow('첫 정상. 둘 정상.', {'첫 정상.': .02, '둘 정상.': .01}, split=6)
        self.assertEqual(traces['gemma'], [])
        self.assertEqual(r['verdict'], 'clean')

    def test_context_candidate_is_screened_and_low_confidence_reaches_gemma(self):
        r, stats, traces = self.run_flow('환전 안내', {'환전 안내': .55}, context=[['page-context', -2]])
        self.assertEqual(traces['laya'], ['환전 안내'])
        self.assertEqual(len(traces['gemma']), 1)
        self.assertEqual(r['verdict'], 'clean')

    def test_high_confidence_ad_context_exception_preserves_probability_and_verifies(self):
        r, stats, traces = self.run_flow('일반 약관', {'일반 약관': .8988}, context=[['page-context', -4]])
        self.assertEqual(r['verdict'], 'clean')
        self.assertEqual(r['ml_prob'], .8988)
        self.assertEqual(stats['review_types'], {})
        self.assertEqual(len(traces['gemma']), 1)
        self.assertFalse(stats['telemetry']['audit_degraded'])

    def test_high_confidence_clean_is_not_a_context_conflict_exception(self):
        r, stats, traces = self.run_flow('일반 약관', {'일반 약관': .01}, context=[['page-context', -4]])
        self.assertEqual(traces['gemma'], [])
        self.assertEqual(r['verdict'], 'clean')
        self.assertEqual(r['processing_status'], 'completed')
        self.assertEqual(stats['review_types'], {})

    def test_audit_rejects_high_confidence_without_real_context_conflict(self):
        from detector.ai_telemetry import AIProgress
        for allow, context, route in ((False, [['page-context', -4]], 'verify_context_conflict'),
                                      (True, [], 'verify_context_conflict'),
                                      (True, [['page-context', -4]], 'verify')):
            meter = AIProgress(allow_context_conflict=allow)
            meter.register('x', {'source_id': 's', 'context': context}, route, .9, .8, 's', 'i')
            meter.event({'event': 'request_start', 'candidate_id': 'x', 'request_id': 'r', 'request_count': 1})
            self.assertTrue(meter.snapshot['audit_degraded'])
            self.assertEqual(meter.snapshot['gemma']['requests_started'], 0)

    def test_model_uncertainty_is_distinguished_from_binding_failure(self):
        r, stats, traces = self.run_flow('정보 부족', {'정보 부족': .55}, {'정보 부족': 'uncertain'})
        self.assertEqual(r['review_code'], 'gemma_uncertain')
        self.assertEqual(stats['review_types'], {'gemma_uncertain': 1})

    def test_length_grouping_preserves_probability_owner_after_reordering(self):
        text = 'a long phrase. z.'
        r, stats, traces = self.run_flow(text, {'a long phrase.': .95, 'z.': .05}, split=text.index('z.'))
        self.assertEqual(traces['laya'], ['z.', 'a long phrase.'])
        self.assertEqual([u['ml_prob'] for u in r['inspection_units']], [.95, .05])
        self.assertEqual(r['verdict'], 'ad')
        self.assertEqual(traces['gemma'], [])

    def test_cleanup_failure_after_prediction_stays_incomplete_and_blocks_gemma(self):
        from detector.laya_runtime import LayaCleanupError
        r = row('<p>환전 안내</p>', '환전 안내')
        runner = MagicMock()
        runner.__enter__.return_value = runner
        runner.plan_batch.return_value = [{'segments': [{'start_char': 0, 'end_char': 5}]}]
        runner.predict_batch.return_value = [{'answers': {'is_illegal_ad': {'noul': .55}}}]
        runner.__exit__.side_effect = LayaCleanupError('회수 실패')
        with patch('detector.ai.LayaRunner', return_value=runner), patch('local_llm.client.GemmaClient') as client:
            stats = analyze([r], [], 'unused', True)
        self.assertEqual(stats['telemetry']['stage'], 'incomplete')
        self.assertEqual(r['verdict'], 'uncertain')
        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
