"""보류 분류는 저장된 증거로만 수행하며 판정·파일을 확정으로 바꾸지 않는다."""
import copy
import unittest

from detector.ai import digest
from detector.review import annotate_reviews
from local_llm.engine import resolve_sentence_choice


class ReviewCauseTests(unittest.TestCase):
    def source(self):
        item = {'candidate_id': 's:0', 'html': '<p>환전 안내</p>', 'context': '', 'target_spans': [[3, 8]]}
        row = {'source_id': 's', 'source_html': item['html'], 'source_context': '', 'tag': 'p',
               'evidence_text': '환전 안내', 'context': [['page-context', -4]],
               'verdict': 'uncertain', 'processing_status': 'review',
               'reason': '문맥 검토 대상: 유효 Laya 확률 없음',
               'inspection': {'mode': 'own', 'target_spans': [[3, 8]]}}
        return item, row

    def test_saved_laya_score_is_restored_for_display_without_confirming_ad(self):
        item, row = self.source()
        result = {**copy.deepcopy(row), 'source_sha256': digest(item), 'decided_by': 'laya',
                  'verdict': 'ad', 'ml_prob': .8988, 'laya_confidence': .898793}
        rep = {'candidates': [row], 'ai': {'source_results': [result]}}
        annotate_reviews(rep)
        self.assertEqual(row['review_code'], 'laya_context_review')
        self.assertEqual(row['verdict'], 'uncertain')
        self.assertEqual(row['ml_prob'], .8988)
        self.assertEqual(row['laya_confidence'], .898793)
        self.assertEqual(rep['ai']['review_types'], {'laya_context_review': 1})

    def test_wrong_source_hash_cannot_supply_a_probability(self):
        item, row = self.source()
        result = {**copy.deepcopy(row), 'source_sha256': '0' * 64, 'decided_by': 'laya',
                  'verdict': 'ad', 'ml_prob': .9, 'laya_confidence': .9}
        annotate_reviews({'candidates': [row], 'ai': {'source_results': [result]}})
        self.assertNotIn('ml_prob', row)
        self.assertNotIn('review_code', row)

    def test_valid_uncertain_is_not_a_binding_error_and_does_not_change_verdict(self):
        item, row = self.source()
        parsed = resolve_sentence_choice(item, {'verdict': 'uncertain', 'evidence_id': 'h0001'})
        row['gemma'] = {'results': [{'candidate_id': 's:0', 'call': {
            'input': item, 'status': 'ok', 'parsed': parsed}}]}
        rep = {'candidates': [row], 'ai': {}}
        annotate_reviews(rep)
        self.assertEqual(rep['ai']['review_types'], {'gemma_uncertain': 1})
        self.assertEqual(row['verdict'], 'uncertain')

    def test_legacy_residual_without_selected_text_does_not_break_history(self):
        item, row = self.source()
        row.pop('evidence_text');row['context'] = None
        parsed = resolve_sentence_choice(item, {'verdict': 'uncertain', 'evidence_id': 'h0001'})
        rep = {'candidates': [], 'ai': {'unresolved_sources': [row], 'extraction': {'calls': [
            {'block_calls': [{'input': {**item, 'candidate_id': 's'}, 'status': 'ok', 'parsed': parsed}]}]}}}
        annotate_reviews(rep)
        self.assertEqual(rep['ai']['review_types'], {'other_review': 1})


if __name__ == '__main__':
    unittest.main()
