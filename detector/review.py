"""저장된 증거로 보류 사유를 구분한다. 모델 호출·원본 파일·판정은 변경하지 않는다."""
import math
from collections import Counter


def annotate_reviews(report):
    from .ai import digest, matching_recovery_text, valid_binding

    ai = report.setdefault('ai', {})
    source_results = {r.get('source_id'): r for r in ai.get('source_results', [])}
    proofs = {c.get('input', {}).get('candidate_id'): c
              for outer in ai.get('extraction', {}).get('calls', [])
              for c in outer.get('block_calls', [])}

    def item_for(row):
        item = {'html': row.get('source_html', ''), 'context': row.get('source_context', '')}
        if row.get('inspection'):
            item['target_spans'] = row['inspection']['target_spans']
        return item

    def model_uncertain(proof, row, item, owner):
        parsed = proof.get('parsed', {})
        if not isinstance(row.get('evidence_text', row.get('text')), str):
            return False  # 구버전의 잔여 항목에는 선별 문구가 없을 수 있다.
        try:
            return (proof.get('status') == 'ok' and parsed.get('verdict') == 'uncertain'
                    and valid_binding(parsed, item, owner) and matching_recovery_text(parsed, row))
        except (KeyError, TypeError, ValueError):
            return False

    for row in report.get('candidates', []):
        if row.get('verdict') != 'uncertain' or row.get('review_code'):
            continue
        results = row.get('gemma', {}).get('results', [])
        if len(results) == 1:
            result = results[0]
            proof = result.get('call', {})
            item = proof.get('input', {})
            # 결과의 request 원문과 현재 후보 원문도 같아야 한다.
            if (item.get('candidate_id') == result.get('candidate_id')
                    and {k: v for k, v in item.items() if k != 'candidate_id'} == item_for(row)
                    and model_uncertain(proof, row, item, result.get('candidate_id'))):
                row.update(review_code='gemma_uncertain', reason='Gemma 정상 응답: 판단 불확실(uncertain)')
                continue
        about = any(isinstance(signal, (tuple, list)) and len(signal) == 2
                    and signal[0] in ('page-context', 'body-context') for signal in (row.get('context') or []))
        if not about or row.get('gemma') or row.get('processing_status') != 'review':
            continue
        prior = source_results.get(row.get('source_id'), {})
        if (prior.get('decided_by') == 'laya' and prior.get('verdict') in ('ad', 'clean')
                and prior.get('source_sha256') == digest(item_for(row))
                and prior.get('source_html') == row.get('source_html')
                and prior.get('source_context', '') == row.get('source_context', '')
                and isinstance(row.get('inspection'), dict)
                and prior.get('raw') == row.get('raw')
                and prior.get('inspection') == row.get('inspection')):
            confidence = prior.get('laya_confidence')
            rounded_p = prior.get('ml_prob')
            if (type(confidence) in (float, int) and math.isfinite(confidence)
                    and .5 <= confidence <= 1 and type(rounded_p) in (float, int)
                    and 0 <= rounded_p <= 1):
                p = confidence if rounded_p >= .5 else 1 - confidence
                threshold = ai.get('laya_confidence_threshold', .8)
                if (type(threshold) in (float, int) and .5 <= threshold <= 1
                        and confidence >= threshold and round(p, 4) == rounded_p
                        and (prior['verdict'] == 'ad') == (p >= .5)):
                    row.update(ml_prob=rounded_p, laya_confidence=confidence,
                               review_code='laya_context_review',
                               reason='저장된 Laya 확률은 유효하나 당시 문맥 분기에서 보류함(Gemma 미호출)')
                    continue
        if row.get('reason', '').startswith('문맥 검토 대상 또는 유효 Laya 확률 없음'):
            row.update(review_code='context_screening_skipped',
                       reason='이전 버전의 문맥 분기에서 Laya 검사를 생략함; Gemma 미호출')

    for row in ai.get('unresolved_sources', []):
        if row.get('review_code'):
            continue
        proof = proofs.get(row.get('source_id'), {})
        if (proof.get('input') == {'candidate_id': row.get('source_id'), **item_for(row)}
                and model_uncertain(proof, row, item_for(row), row.get('source_id'))):
            row.update(review_code='gemma_uncertain', reason='Gemma 정상 응답: 판단 불확실(uncertain)')
    ai['review_types'] = dict(Counter(r.get('review_code', 'other_review') for r in
        [c for c in report.get('candidates', []) if c.get('verdict') == 'uncertain'] + ai.get('unresolved_sources', [])))
    return report
