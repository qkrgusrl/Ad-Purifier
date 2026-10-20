"""관측 계약. 실제 run/analyze/client/infer, 수집·모델·HTTP만 오프라인 대체."""
import copy
import importlib
import inspect
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixtures.ai_contract import OfflineRun, main, prediction, source
from detector import ai
from local_llm import client as client_module
from local_llm.engine import LocalServer, source_digest

LAYA = ('total', 'attempted', 'completed', 'failed', 'in_flight', 'remaining')
GEMMA = ('eligible_verify', 'eligible_extract', 'settled', 'remaining', 'in_flight',
         'requests_started', 'responses_completed', 'requests_failed', 'output_failed',
         'preflight_failed', 'reused')


class AITelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix='ai-telemetry-'))
        self.out = Path(self.temp)
        self.chats, self.trace, self.observed = [], [], []
        self.fault = None

    def require_observer(self):
        for entry in (main.run, ai.analyze):
            parameter = inspect.signature(entry).parameters.get('telemetry')
            self.assertIsNotNone(parameter, entry.__name__ + ': optional telemetry 계약 미구현')
            self.assertIsNone(parameter.default)
        parameter = inspect.signature(client_module.GemmaClient).parameters.get('on_event')
        self.assertIsNotNone(parameter, 'GemmaClient optional on_event 계약 미구현')
        self.assertIsNone(parameter.default)

    def server_factory(self, **kwargs):
        # 생성/프로세스 시작만 우회. 제품 infer/prototype와 client callback 전달은 실제다.
        server = LocalServer.__new__(LocalServer)
        server.ctx_size, server.cache_prompt = 4096, None
        server.on_event = kwargs.get('on_event')
        server.request = self.request
        return server

    def request(self, path, payload=None, **kwargs):
        if path == '/apply-template':
            if self.fault == 'preflight':
                raise OSError('fixture-preflight')
            return {'prompt': json.dumps(payload['messages'], ensure_ascii=False)}
        if path == '/tokenize':
            return {'tokens': list(range(5000 if self.fault == 'overflow' else 100))}
        self.assertEqual(path, '/v1/chat/completions')
        if kwargs.get('on_dispatch') is not None:
            kwargs['on_dispatch']()
        self.trace.append('http')
        self.chats.append(copy.deepcopy(payload))
        if self.fault == 'transport':
            raise OSError('fixture-transport')
        data = json.loads(payload['messages'][1]['content'])
        unit = next(u for u in data['source_units'] if u['selectable'])
        content = '{broken' if self.fault == 'output' else json.dumps(
            {'verdict': 'ad', 'evidence_id': unit['id']})
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}

    def observer(self, event):
        self.observed.append(copy.deepcopy(event))
        self.trace.append(event['event'])

    def execute(self, harness, observer=None, **kwargs):
        self.require_observer()
        real_client = client_module.GemmaClient
        with harness:
            with patch.object(client_module, 'LocalServer', side_effect=self.server_factory), \
                 patch.object(LocalServer, '__enter__', lambda server: server), \
                 patch.object(LocalServer, '__exit__', lambda server, *args: None), \
                 patch.object(ai, 'GemmaClient', real_client, create=True), \
                 patch.dict('sys.modules', {'local_llm.client': client_module}):
                report = harness.run(self.out, telemetry=observer or self.observer, **kwargs)
        self.assertEqual(json.loads((self.out / 'report.json').read_text()),
                         json.loads(json.dumps(report)))
        return report

    def telemetry(self, report):
        value = report['ai'].get('telemetry')
        self.assertIsInstance(value, dict, 'report.ai.telemetry 필요')
        self.assertNotIn('snapshot', value, '최종 snapshot 필드는 평탄해야 함')
        self.assert_snapshot(value)
        self.assert_remaining(value)
        return value

    def assert_snapshot(self, value):
        self.assertIn('stage', value)
        for section, keys in (('laya', LAYA), ('gemma', GEMMA)):
            self.assertIsInstance(value.get(section), dict)
            for key in keys:
                self.assertIsInstance(value[section].get(key), int, section + '.' + key)
                self.assertGreaterEqual(value[section][key], 0)
        self.assertIsInstance(value.get('audit_degraded'), bool)
        self.assertIsInstance(value.get('audit_errors'), int)

    def assert_remaining(self, t):
        l, g = t['laya'], t['gemma']
        self.assertEqual(l['remaining'], l['total'] - l['completed'] - l['failed'])
        self.assertEqual(g['remaining'], g['eligible_verify'] + g['eligible_extract'] - g['settled'])

    def audit(self, telemetry):
        path = Path(telemetry['audit_path'])
        self.assertFalse(path.is_absolute(), 'out 기준 상대경로 필요')
        if not path.is_absolute():
            path = self.out / path
        self.assertEqual(path.resolve().parent, (self.out / 'ai-audit').resolve())
        self.assertRegex(path.name, r'^[A-Za-z0-9_-]+\.jsonl$')
        self.assertEqual(path.stem, telemetry['run_id'])
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(records, '감사 JSONL이 비어 있음')
        for record in records:
            self.assertEqual(record.get('run_id'), telemetry['run_id'])
            self.assertTrue(record.get('time'), 'publisher 시간 누락')
        return path, records

    def test_live_progress_and_owner_audit_through_real_run(self):
        row = source()
        h = OfflineRun([row], predict=lambda ts: [prediction(.55000001) for _ in ts])
        def predict(texts):
            self.assertTrue(any(e['event'] == 'progress' and
                e['snapshot']['laya']['in_flight'] > 0 for e in self.observed),
                'Laya 처리 중 snapshot을 추론 전에 게시해야 함')
            return [prediction(.55000001) for _ in texts]
        h.predict = predict
        report = self.execute(h)
        telemetry = self.telemetry(report)
        progress = [e for e in self.observed if e['event'] == 'progress']
        self.assertGreaterEqual(len(progress), 3)
        for event in progress:
            self.assert_snapshot(event['snapshot'])
            self.assert_remaining(event['snapshot'])
            self.assertEqual(event.get('run_id'), telemetry['run_id'])
            self.assertTrue(event.get('time'))
        self.assertTrue(any(e['snapshot']['gemma']['in_flight'] > 0 for e in progress))
        self.assertEqual(telemetry['laya']['completed'], 1)
        self.assertEqual(telemetry['gemma']['requests_started'], 1)
        self.assertEqual(telemetry['gemma']['responses_completed'], 1)
        self.assertEqual(telemetry['gemma']['settled'], 1)
        self.assertEqual(telemetry['gemma']['remaining'], 0)
        _, records = self.audit(telemetry)
        for name in ('request_start', 'request_end', 'item_end'):
            matching = [r for r in records if r['event'] == name]
            self.assertEqual(len(matching), 1, name)
            record = matching[0]
            self.assertEqual(record.get('p'), .55000001)
            self.assertEqual(record.get('confidence'), .55000001)
            self.assertEqual(record.get('threshold'), .8)
            self.assertEqual(record.get('source_id'), row['source_id'])
            self.assertTrue(record.get('owner_id'))
            self.assertEqual(record.get('route_path'), 'verify')
            self.assertEqual(record.get('source_sha256'), source_digest(
                {'html': row['source_html'], 'context': row['source_context']}))
            self.assertRegex(record.get('input_sha256', ''), r'^[0-9a-f]{64}$')
            self.assertTrue(record.get('request_id'))
        self.assertEqual(len({r['request_id'] for r in records
                              if r['event'] in ('request_start', 'request_end', 'item_end')}), 1)
        self.assertEqual(len(self.chats), 1)

    def test_duplicate_laya_inputs_and_partial_invalid_output(self):
        rows = [source('s1'), source('s2'), source('s3', text='환전 시간 안내')]
        h = OfflineRun(rows, predict=lambda ts: [prediction(.55)])
        report = self.execute(h)
        t = self.telemetry(report)
        self.assertEqual(len(h.laya_inputs), 2)
        self.assertEqual(t['laya'], dict(total=2, attempted=2, completed=1, failed=1,
                                        preflight_rejected=0, in_flight=0, remaining=0))
        self.assertTrue(report['needs_review'])
        _, records = self.audit(t)
        failed_text = sorted(set(r['text'] for r in rows))[1]
        failed_ids = {r['source_id'] for r in rows if r['text'] == failed_text}
        self.assertFalse(any(r['event'] == 'request_start' and r.get('source_id') in failed_ids for r in records))

    def test_laya_input_too_long_is_listed_as_length_skipped(self):
        rows = [source('s1'), source('s2', text='환전 시간 안내')]
        rejected = prediction(preflight={'reason': 'input_too_long', 'input_tokens': 1500,
                                         'token_limit': 900, 'model_invoked': False},
                              status='preflight_rejected', answers={})
        h = OfflineRun(rows, predict=lambda ts: [rejected for _ in ts])
        report = self.execute(h)
        ai_report = report['ai']
        self.assertEqual(ai_report['length_skipped_count'], 2)
        self.assertEqual((ai_report['length_skipped_laya'], ai_report['length_skipped_gemma']), (2, 0))
        self.assertEqual({x['source_id'] for x in ai_report['length_skipped']}, {'s1', 's2'})
        for item in ai_report['length_skipped']:
            self.assertEqual((item['stage'], item['input_tokens'], item['token_limit']), ('laya', 1500, 900))
            self.assertGreater(item['chars'], 0)
        self.assertTrue(report['needs_review'], '길이 때문에 못 본 항목은 정상으로 두지 않고 검토 필요')
        self.assertEqual(self.chats, [], '거절된 입력을 Gemma로 대신 보내면 안 됨')

    def test_gemma_input_overflow_is_listed_as_length_skipped(self):
        self.fault = 'overflow'
        h = OfflineRun([source('s1')], predict=lambda ts: [prediction(.55) for _ in ts])
        report = self.execute(h)
        ai_report = report['ai']
        self.assertEqual(ai_report['length_skipped_count'], 1)
        self.assertEqual((ai_report['length_skipped_laya'], ai_report['length_skipped_gemma']), (0, 1))
        item = ai_report['length_skipped'][0]
        self.assertEqual((item['stage'], item['source_id'], item['input_tokens']), ('gemma', 's1', 5000))
        self.assertEqual(item['token_limit'], 4096 - 192 - 64)
        self.assertEqual(self.chats, [], '길이 초과 입력은 모델 요청 없이 보류')
        self.assertTrue(report['needs_review'])

    def test_no_length_skip_when_inputs_fit(self):
        h = OfflineRun([source('s1')], predict=lambda ts: [prediction(.55) for _ in ts])
        ai_report = self.execute(h)['ai']
        self.assertEqual((ai_report['length_skipped_count'], ai_report['length_skipped']), (0, []))

    def test_full_precision_threshold_and_each_duplicate_owner_audit(self):
        for p in (.2, .20000000000001, .79999999999999, .8):
            with self.subTest(p=p):
                self.chats.clear()
                rows = [source('s1'), source('s2', context='두 번째 원문')]
                h = OfflineRun(rows, predict=lambda ts: [prediction(p) for _ in ts])
                t = self.telemetry(self.execute(h))
                _, records = self.audit(t)
                starts = [r for r in records if r['event'] == 'request_start']
                self.assertEqual(len(starts), 2 if max(p, 1-p) < .8 else 0)
                self.assertEqual(len(h.laya_inputs), 1)
                if starts:
                    self.assertEqual({r['source_id'] for r in starts}, {'s1', 's2'})
                    for key in ('owner_id', 'request_id'):
                        self.assertEqual(len({r[key] for r in starts}), 2, key)
                    for r in starts:
                        row = next(row for row in rows if row['source_id'] == r['source_id'])
                        # input_sha256는 중복 제거에 쓴 Laya 입력의 해시다.
                        self.assertEqual(r['input_sha256'], hashlib.sha256(
                            main.normalize(row['text']).encode()).hexdigest())
                        self.assertEqual(r['p'], p)
                        self.assertEqual(r['confidence'], max(p, 1-p))
                        self.assertLess(r['confidence'], r['threshold'])
                        self.assertEqual(r['source_sha256'], source_digest(
                            {'html': row['source_html'], 'context': row['source_context']}))
                self.assertEqual(t['gemma']['requests_started'], len(self.chats))

    def test_verify_extract_reuse_settles_without_another_http_request(self):
        h = OfflineRun([source('s1'), source('s2', text='환전 시간 안내')], candidate_ids=['s1'])
        t = self.telemetry(self.execute(h, detect_posted=True))
        _, records = self.audit(t)
        starts = [r for r in records if r['event'] == 'request_start']
        self.assertEqual(len(starts), 2)
        self.assertEqual({r['route_path'] for r in starts}, {'verify', 'extract'})
        self.assertEqual({r['source_id'] for r in starts}, {'s1', 's2'})
        g = t['gemma']
        self.assertEqual(g['eligible_extract'], 1)
        self.assertGreaterEqual(g['eligible_verify'], 1)
        self.assertEqual(g['reused'], 1)
        self.assertEqual(g['requests_started'], 2)
        self.assertEqual(g['responses_completed'], 2)
        self.assertEqual(g['remaining'], 0)
        self.assertEqual(g['in_flight'], 0)
        self.assertEqual(len([r for r in records if r['event'] == 'request_end']), 2)
        for start in starts:
            self.assertEqual(start['p'], .55)
            self.assertEqual(start['confidence'], .55)
            self.assertTrue(any(r['event'] == 'item_end' and r.get('request_id') == start['request_id'] for r in records))

    def test_engine_events_bracket_http_and_end_every_leaf_including_preflight(self):
        self.require_observer()
        parameter = inspect.signature(LocalServer).parameters.get('on_event')
        self.assertIsNotNone(parameter, 'LocalServer optional on_event 필요')
        self.assertIsNone(parameter.default)
        for task in ('verify', 'extract'):
            for fault in (None, 'transport', 'output', 'preflight'):
                with self.subTest(task=task, fault=fault):
                    self.fault = fault
                    self.chats.clear()
                    events, order = [], []
                    def event(value):
                        events.append(copy.deepcopy(value))
                        order.append(value['event'])
                    request_impl = self.request
                    def request(*args, **kwargs):
                        if args[0] == '/v1/chat/completions':
                            dispatch = kwargs.get('on_dispatch')
                            if dispatch is None:
                                order.append('http')
                            else:
                                def on_dispatch():
                                    dispatch()
                                    order.append('http')
                                kwargs['on_dispatch'] = on_dispatch
                        return request_impl(*args, **kwargs)
                    with patch.object(client_module, 'LocalServer', side_effect=self.server_factory), \
                         patch.object(LocalServer, '__enter__', lambda server: server), \
                         patch.object(LocalServer, '__exit__', lambda server, *args: None), \
                         patch.object(self, 'request', side_effect=request):
                        with client_module.GemmaClient(on_event=event) as client:
                            if task == 'verify':
                                client.verify({'candidate_id': 'owner-1', 'html': '<p>환전 안내</p>', 'context': ''})
                            else:
                                client.extract({'blocks': [dict(block_id='owner-1', html='<p>환전 안내</p>', context='')]})
                    starts = [e for e in events if e['event'] == 'request_start']
                    ends = [e for e in events if e['event'] == 'request_end']
                    leaves = [e for e in events if e['event'] == 'item_end']
                    self.assertEqual(len(starts), int(fault != 'preflight'))
                    self.assertEqual(len(ends), len(starts))
                    self.assertEqual(len(leaves), 1, 'preflight 실패도 leaf 종료 필요')
                    self.assertEqual(len(self.chats), len(starts))
                    for e in starts + ends + leaves:
                        for key in ('candidate_id', 'task', 'status', 'completion_received', 'request_count'):
                            self.assertIn(key, e)
                        self.assertEqual(e['candidate_id'], 'owner-1')
                    if starts:
                        self.assertLess(order.index('request_start'), order.index('http'))
                        self.assertLess(order.index('http'), order.index('request_end'))
                        self.assertLess(order.index('request_end'), order.index('item_end'))
                        self.assertTrue(starts[0].get('request_id'))
                        self.assertEqual(starts[0]['request_id'], ends[0]['request_id'])
                        self.assertEqual(starts[0]['request_id'], leaves[0]['request_id'])
                        self.assertEqual(ends[0]['completion_received'], fault != 'transport')
                    self.assertEqual(leaves[0]['request_count'], int(fault != 'preflight'))
                    self.chats.clear()
                    t = self.telemetry(self.execute(OfflineRun(
                        candidate_ids=[] if task == 'extract' else None), detect_posted=True))
                    g = t['gemma']
                    self.assertEqual(g['requests_started'], int(fault != 'preflight'))
                    self.assertEqual(g['responses_completed'], int(fault in (None, 'output')))
                    self.assertEqual(g['requests_failed'], int(fault == 'transport'))
                    self.assertEqual(g['output_failed'], int(fault == 'output'))
                    self.assertEqual(g['preflight_failed'], int(fault == 'preflight'))
                    self.assertEqual(g['remaining'], 0)
                    self.assertEqual(g['in_flight'], 0)
                    _, records = self.audit(t)
                    actual = [r for r in records if r['event'] == 'request_start']
                    self.assertEqual(len(actual), len(self.chats))
                    for r in records:
                        if r['event'] in ('request_start', 'request_end', 'item_end'):
                            self.assertEqual(r['p'], .55)
                            self.assertEqual(r['confidence'], .55)
                            expected_route = 'verify' if r.get('status') == 'reused' else task
                            self.assertEqual(r['route_path'], expected_route)
                            if r.get('status') == 'reused':
                                self.assertEqual(r['request_count'], 0)
                            self.assertEqual(r['source_id'], 's1')

    def test_jsonl_write_failure_does_not_stop_inference(self):
        import builtins
        original_open, path_open = builtins.open, Path.open
        def denied(file, *args, **kwargs):
            if str(file).endswith('.jsonl'):
                raise OSError('fixture-audit-write-denied')
            return original_open(file, *args, **kwargs)
        def denied_path(path, *args, **kwargs):
            if path.suffix == '.jsonl':
                raise OSError('fixture-audit-write-denied')
            return path_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=denied), patch.object(Path, 'open', denied_path):
            report = self.execute(OfflineRun())
        t = self.telemetry(report)
        self.assertTrue(t['audit_degraded'])
        self.assertGreater(t['audit_errors'], 0)
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(report['candidates'][0]['verdict'], 'ad')

    def test_observer_failure_does_not_stop_inference_and_is_reported(self):
        calls = []
        def broken(event):
            calls.append(event['event'])
            raise RuntimeError('fixture-observer-failure')
        report = self.execute(OfflineRun(), observer=broken)
        t = self.telemetry(report)
        self.assertTrue(calls)
        self.assertTrue(t['audit_degraded'])
        self.assertGreater(t['audit_errors'], 0)
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(report['candidates'][0]['verdict'], 'ad')
        self.audit(t)

    def test_unique_relative_logs_flush_midrun_and_publisher_forces_identity(self):
        self.require_observer()
        flushed = []
        original = ai.analyze
        def analyze(*args, **kwargs):
            kwargs['telemetry']({'event': 'auditdelivery', 'run_id': '../../forged', 'time': 'forged',
                                 'p': .55, 'confidence': .55, 'threshold': .8})
            return original(*args, **kwargs)
        def observer(event):
            self.observer(event)
            if event['event'] == 'progress' and event['snapshot']['gemma']['responses_completed']:
                rows = [json.loads(line) for path in (self.out / 'ai-audit').glob('*.jsonl')
                        for line in path.read_text(encoding='utf-8').splitlines()]
                flushed.append(any(r['event'] == 'request_start' for r in rows))
        # signature는 wrapper가 아닌 원래 제품 함수로 이미 검증했다.
        with patch.object(ai, 'analyze', new=analyze):
            with patch.object(self, 'require_observer'):
                first = self.telemetry(self.execute(OfflineRun(), observer=observer))
                path1, records1 = self.audit(first)
                bytes1 = path1.read_bytes()
                second = self.telemetry(self.execute(OfflineRun(), observer=observer))
        path2, records2 = self.audit(second)
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertNotEqual(path1, path2)
        self.assertEqual(path1.read_bytes(), bytes1)
        self.assertTrue(flushed and all(flushed), '콜백 시점 append/flush 필요')
        for records in (records1, records2):
            delivery = [r for r in records if r['event'] == 'auditdelivery']
            self.assertEqual(len(delivery), 1)
            self.assertNotEqual(delivery[0]['time'], 'forged')
        self.assertTrue(all(e.get('run_id') != '../../forged' and e.get('time') != 'forged' for e in self.observed))

    def test_optional_observer_keeps_existing_offline_run_compatible(self):
        self.require_observer()
        with OfflineRun(predict=lambda ts: [prediction(.99) for _ in ts]) as h:
            report = h.run(self.out)
        t = self.telemetry(report)
        self.assertEqual(report['candidates'][0]['verdict'], 'ad')
        self.assertEqual(t['gemma']['requests_started'], 0)
        self.assertEqual(t['audit_errors'], 0)
        self.assertFalse(t['audit_degraded'])

    def test_scan_job_snapshot_only_bounded_status_and_stale_started_token(self):
        server = importlib.import_module('ui.server')
        saved = []
        initial = dict(server.state, running=True, started=111, cancelling=False, ai_progress=None)
        snap = dict(stage='laya', laya=dict.fromkeys(LAYA, 0), gemma=dict.fromkeys(GEMMA, 0),
                    audit_degraded=False, audit_errors=0)
        def fake_run(*args, **kwargs):
            callback = kwargs.get('telemetry')
            self.assertTrue(callable(callback), 'scan_job telemetry callback 연결 필요')
            saved.append(callback)
            callback(dict(event='progress', snapshot=copy.deepcopy(snap), run_id='audit-id', time='t'))
            self.assertEqual(server.state.get('ai_progress'), snap, 'wrapper 아닌 snapshot만 저장')
            callback(dict(event='request_start', source_html='PRIVATE_RAW', p=.55))
            self.assertEqual(server.state['ai_progress'], snap)
            return {'needs_review': False}
        with patch.object(server, 'state', initial), \
             patch.object(server, 'HISTORY', self.out / 'history'), \
             patch.object(server, 'app_dir', return_value=self.out), \
             patch.object(server, 'run', side_effect=fake_run), \
             patch.object(server.shutil, 'copy2'):
            server.scan_job('https://fixture.invalid', True, 1)
            self.assertEqual(len(saved), 1)
            server.state.update(started=222, running=True, ai_progress=None)
            saved[0](dict(event='progress', snapshot=snap, run_id='audit-id', time='late'))
            self.assertIsNone(server.state['ai_progress'], '이전 실행 callback이 새 실행을 오염시킴')
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/api/status'
            response = []
            with patch.object(handler, 'trusted', return_value=True), \
                 patch.object(handler, 'send', side_effect=lambda code, payload, *args: response.append(payload)), \
                 patch.object(server, 'model_readiness', return_value={'files_present': True}):
                handler.do_GET()
            self.assertIsNone(response[0].get('ai_progress'))
            self.assertNotIn('PRIVATE_RAW', json.dumps(response[0]))


if __name__ == '__main__':
    unittest.main()
