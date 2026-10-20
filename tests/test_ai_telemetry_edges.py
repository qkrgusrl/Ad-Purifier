"""독립 경계 계약: 전송 전 예산 만료와 클라이언트 준비 실패. 실모델 금지."""
import copy
import json
import unittest
from unittest.mock import patch

import test_ai_telemetry as baseline
from fixtures.ai_contract import OfflineRun, source
from detector.ai_telemetry import AIProgress
from local_llm import client as client_module
from local_llm import engine


class AITelemetryEdgeTests(unittest.TestCase):
    # 기존 테스트를 중복 수집하지 않고 오프라인 실행/감사 helper만 재사용한다.
    setUp = baseline.AITelemetryTests.setUp
    require_observer = baseline.AITelemetryTests.require_observer
    server_factory = baseline.AITelemetryTests.server_factory
    request = baseline.AITelemetryTests.request
    observer = baseline.AITelemetryTests.observer
    execute = baseline.AITelemetryTests.execute
    telemetry = baseline.AITelemetryTests.telemetry
    assert_snapshot = baseline.AITelemetryTests.assert_snapshot
    assert_remaining = baseline.AITelemetryTests.assert_remaining
    audit = baseline.AITelemetryTests.audit

    def test_budget_expires_after_preflight_before_real_request_dispatch(self):
        server = engine.LocalServer.__new__(engine.LocalServer)
        server.ctx_size, server.cache_prompt = 4096, None
        server.base, server.api_key = 'http://fixture.invalid', None
        server.budget = engine.Budget(deadline=float('inf'))
        events, paths = [], []
        server.on_event = lambda event: events.append(copy.deepcopy(event))

        def fake_http(base, path, payload=None, *args, **kwargs):
            paths.append(path)
            if path == '/apply-template':
                return {'prompt': 'offline fixture'}
            if path == '/tokenize':
                return {'tokens': list(range(100))}
            raise AssertionError('미전송이어야 하는 chat HTTP: ' + path)

        real_prompt_size = server.prompt_size

        def expire_after_preflight(*args, **kwargs):
            size = real_prompt_size(*args, **kwargs)
            server.budget.deadline = -1
            return size

        # request는 실제 구현이다. 템플릿/토큰 HTTP만 응답을 대체한다.
        with patch.object(engine, 'http_json', side_effect=fake_http), \
             patch.object(server, 'prompt_size', side_effect=expire_after_preflight):
            result = server.infer('verify', {
                'candidate_id': 'expired-owner', 'html': '<p>환전 안내</p>', 'context': ''})

        self.assertEqual(paths, ['/apply-template', '/tokenize'])
        self.assertEqual(result['status'], 'error')
        self.assertIn('TimeoutError', result.get('error', ''))
        leaves = [e for e in events if e['event'] == 'item_end']
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]['candidate_id'], 'expired-owner')
        # 모든 기대값을 함께 표시해 부모 RED에서 허위 전송 계수를 바로 확인한다.
        self.assertEqual({
            'request_start': sum(e['event'] == 'request_start' for e in events),
            'request_end': sum(e['event'] == 'request_end' for e in events),
            'result_request_count': result['request_count'],
            'item_request_count': leaves[0]['request_count'],
        }, dict(request_start=0, request_end=0, result_request_count=0, item_request_count=0))

    def assert_load_failure_settled(self, task):
        harness = OfflineRun(candidate_ids=[] if task == 'extract' else None)
        # 실제 GemmaClient의 준비 경로에서만 실패시킨다. execute는 서버 시작과
        # HTTP를 기존 오프라인 helper로 대체하고 OfflineRun은 외부 접속을 차단한다.
        original_factory = self.server_factory

        def fail_load():
            raise OSError('fixture-model-load-failure')

        def failing_factory(**kwargs):
            server = original_factory(**kwargs)
            # GemmaClient는 server.__enter__()를 명시 호출한다.
            server.__enter__ = fail_load
            return server

        with patch.object(self, 'server_factory', side_effect=failing_factory):
            report = self.execute(harness, detect_posted=True)

        self.assertTrue(report['needs_review'])
        self.assertEqual(self.chats, [])
        t = self.telemetry(report)
        _, records = self.audit(t)
        self.assertEqual(sum(r['event'] == 'request_start' for r in records), 0)
        self.assertEqual(sum(r['event'] == 'request_end' for r in records), 0)
        g = t['gemma']
        self.assertEqual(g['eligible_' + task], 1)
        leaves = [r for r in records if r['event'] == 'item_end']
        self.assertEqual({
            'settled': g['settled'], 'preflight_failed': g['preflight_failed'],
            'remaining': g['remaining'], 'in_flight': g['in_flight'],
            'requests_started': g['requests_started'], 'item_end': len(leaves),
        }, dict(settled=1, preflight_failed=1, remaining=0, in_flight=0,
                requests_started=0, item_end=1))
        self.assertEqual(leaves[0]['route_path'], task)
        self.assertEqual(leaves[0]['source_id'], 's1')
        self.assertEqual(leaves[0]['request_count'], 0)
        self.assertFalse(leaves[0]['completion_received'])
        self.assertEqual(leaves[0]['preflight_failed'], 1)

    def test_verify_load_failure_settles_without_dispatch(self):
        self.assert_load_failure_settled('verify')

    def test_extract_load_failure_settles_without_dispatch(self):
        self.assert_load_failure_settled('extract')

    def test_extract_preflight_skip_is_settled_before_next_block_http(self):
        rows = [source('s1'), source('s2', text='환전 시간 안내')]
        boundary = []
        original_request = self.request

        def request(path, payload=None, **kwargs):
            if path == '/apply-template':
                data = json.loads(payload['messages'][1]['content'])
                if any(u['text'] == rows[0]['source_html'] for u in data['source_units']):
                    raise OSError('fixture-first-block-preflight-failure')
            if path == '/v1/chat/completions':
                data = json.loads(payload['messages'][1]['content'])
                # 전송 직전 상태를 복사하고 실행 후 검사한다. 제품 코드가
                # 콜백 예외를 흡수해도 assertion이 사라지지 않게 한다.
                boundary.append((data, copy.deepcopy(self.observed)))
            return original_request(path, payload, **kwargs)

        with patch.object(self, 'request', side_effect=request):
            report = self.execute(OfflineRun(rows, candidate_ids=[]), detect_posted=True)

        self.assertEqual(len(boundary), 1)
        model_input, events = boundary[0]
        self.assertTrue(any(u['text'] == rows[1]['source_html'] for u in model_input['source_units']))
        progress = [e['snapshot']['gemma'] for e in events if e['event'] == 'progress']
        self.assertTrue(progress)
        self.assertEqual(progress[-1]['preflight_failed'], 1,
                         '첫 블록 실패를 전체 extract 반환까지 미루면 안 됨')
        self.assertGreaterEqual(progress[-1]['settled'], 1)
        skipped = [e for e in events if e['event'] == 'item_end'
                   and e.get('source_id') == 's1']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['request_count'], 0)
        self.assertEqual(skipped[0]['preflight_failed'], 1)
        t = self.telemetry(report)
        self.assertEqual(t['gemma']['preflight_failed'], 1)
        self.assertEqual(t['gemma']['remaining'], 0)

    def test_unknown_owner_request_end_cannot_change_active_request_counters(self):
        meter = AIProgress()
        meter.snapshot['gemma']['eligible_verify'] = 1
        meter.register('owner-1', source('s1'), 'verify', .55, .8, 'a' * 64, 'b' * 64)
        meter.event(dict(event='request_start', candidate_id='owner-1',
                         request_id='request-1', task='verify', status='started',
                         completion_received=False, request_count=1))
        self.assertFalse(meter.snapshot['audit_degraded'], '유효 시작 이벤트가 전제')
        self.assertEqual(meter.snapshot['gemma']['in_flight'], 1)
        before = copy.deepcopy(meter.snapshot['gemma'])

        meter.event(dict(event='request_end', candidate_id='unknown-owner',
                         request_id='request-1', task='verify', status='ok',
                         completion_received=True, request_count=1))

        self.assertTrue(meter.snapshot['audit_degraded'])
        self.assertGreater(meter.snapshot['audit_errors'], 0)
        self.assertEqual(meter.snapshot['gemma'], before,
                         '알 수 없는 owner가 유효 요청의 진행/응답 계수를 바꾸면 안 됨')

    def test_duplicate_request_events_count_once_and_report_audit_error(self):
        meter = AIProgress()
        meter.snapshot['gemma']['eligible_verify'] = 1
        meter.register('owner-1', source('s1'), 'verify', .55, .8, 'a' * 64, 'b' * 64)
        start = dict(event='request_start', candidate_id='owner-1',
                     request_id='request-1', task='verify', status='started',
                     completion_received=False, request_count=1)
        end = dict(start, event='request_end', status='ok', completion_received=True)
        meter.event(start)
        self.assertFalse(meter.snapshot['audit_degraded'], '유효 시작 이벤트가 전제')
        meter.event(dict(start))
        meter.event(end)
        meter.event(dict(end))
        # 요청 종료와 별개인 논리 owner 종료도 재전달 시 한 번만 정산한다.
        leaf = dict(end, event='item_end', preflight_failed=0)
        meter.event(leaf)
        before_duplicate_leaf = copy.deepcopy(meter.snapshot['gemma'])
        meter.event(dict(leaf))

        g = meter.snapshot['gemma']
        self.assertEqual({key: g[key] for key in (
            'requests_started', 'responses_completed', 'in_flight',
            'requests_failed', 'settled', 'remaining')}, dict(
                requests_started=1, responses_completed=1, in_flight=0,
                requests_failed=0, settled=1, remaining=0))
        self.assertEqual(g, before_duplicate_leaf)
        self.assertTrue(meter.snapshot['audit_degraded'])
        self.assertGreater(meter.snapshot['audit_errors'], 0)


if __name__ == '__main__':
    unittest.main()
