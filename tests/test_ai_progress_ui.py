"""실제 제품 JS를 Playwright로 실행한다. 모든 HTTP는 메모리 fixture로 응답."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def snapshot(stage='laya'):
    return {'stage': stage,
        'laya': dict(total=13, attempted=11, completed=7, failed=2, in_flight=2, remaining=4),
        'gemma': dict(eligible_verify=17, eligible_extract=19, settled=23, remaining=13,
                     in_flight=3, requests_started=29, responses_completed=24, requests_failed=2,
                     output_failed=1, preflight_failed=5, reused=7),
        'audit_degraded': False, 'audit_errors': 0}


class AIProgressFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {'PLAYWRIGHT_BROWSERS_PATH': '0'})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(service_workers='block')
        self.addCleanup(self.context.close)
        self.context.route_web_socket('**/*', lambda ws: ws.close())
        self.status = dict(running=True, phase='judge', run_id='run-a', elapsed=1,
            pages_done=1, pages_found=1, entry_url='https://fixture.invalid', current_url='',
            model_available=True, out_dir='fixture', validation_mode='complete')
        html = (ROOT / 'ui/static/index.html').read_text(encoding='utf-8')
        def route(r):
            url = urlsplit(r.request.url)
            if url.netloc != 'ai-progress.test':
                r.abort()
            elif url.path == '/':
                r.fulfill(content_type='text/html', body=html)
            elif url.path == '/api/status':
                r.fulfill(json=copy.deepcopy(self.status))
            elif url.path == '/api/runs':
                r.fulfill(json=[])
            else:
                r.abort()
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.goto('http://ai-progress.test/', wait_until='networkidle')
        self.page.evaluate('clearInterval(polling)')

    def render(self, progress):
        self.assertTrue(self.page.evaluate("typeof renderAIProgress === 'function'"),
                        'renderAIProgress(progress) 제품 seam 미구현')
        self.page.evaluate('p => renderAIProgress(p)', progress)
        for id_ in ('aiProgress', 'layaProgress', 'gemmaProgress', 'aiAuditStatus'):
            self.assertEqual(self.page.locator('#' + id_).count(), 1, id_)
        self.assertFalse(self.errors, str(self.errors))

    def test_actual_render_distinguishes_logical_items_and_http_requests(self):
        self.render(snapshot())
        laya = self.page.locator('#layaProgress').inner_text()
        gemma = self.page.locator('#gemmaProgress').inner_text()
        for number in (13, 11, 7, 2, 4):
            self.assertRegex(laya, rf'(?<!\d){number}(?!\d)')
        for number in (17, 19, 23, 13, 3, 29, 24, 2, 1, 5, 7):
            self.assertRegex(gemma, rf'(?<!\d){number}(?!\d)')
        self.assertRegex(gemma, '검증|verify')
        self.assertRegex(gemma, '추출|extract')
        self.assertRegex(gemma, '논리|항목')
        self.assertRegex(gemma, '실제.*요청|HTTP|requests')

    def test_old_server_null_clears_stale_counts_and_marks_uninstrumented(self):
        self.render(snapshot())
        self.render(None)
        text = self.page.locator('#aiProgress').inner_text()
        self.assertIn('이번 실행은 AI 세부 진행 미계측', text)
        self.assertNotRegex(text, r'(?<!\d)(0|13|17|19|29)(?!\d)')
        self.assertNotIn('완료', text)

    def test_zero_queue_waiting_is_not_complete_and_audit_degradation_is_visible(self):
        p = snapshot('laya')
        p['gemma'] = dict.fromkeys(p['gemma'], 0)
        p['audit_degraded'], p['audit_errors'] = True, 4
        self.render(p)
        gemma = self.page.locator('#gemmaProgress').inner_text()
        self.assertNotRegex(gemma, '전체 완료|처리 완료|검사 완료|검증 완료|모두 완료')
        self.assertRegex(self.page.locator('#layaProgress').inner_text(), 'Laya.*처리 중|선별 중|대기')
        audit = self.page.locator('#aiAuditStatus').inner_text()
        self.assertRegex(audit, '누락|오류|실패|불완전')
        self.assertRegex(audit, r'(?<!\d)4(?!\d)')

    def test_poll_new_run_resets_previous_counts_and_old_server_fallback(self):
        self.status['ai_progress'] = snapshot()
        self.page.evaluate('pollStatus()')
        self.assertTrue(self.page.evaluate("typeof renderAIProgress === 'function'"), 'UI seam 미구현')
        self.assertRegex(self.page.locator('#gemmaProgress').inner_text(), r'(?<!\d)29(?!\d)')
        self.status.update(run_id='run-b', started=222, phase='crawl', ai_progress=None)
        self.page.evaluate('pollStatus()')
        text = self.page.locator('#aiProgress').inner_text()
        self.assertNotRegex(text, r'(?<!\d)(17|19|29)(?!\d)')
        self.assertRegex(text, '집계 전|미계측|수집 중|입력.*대기')
        self.status.pop('ai_progress')
        self.status.update(run_id='run-c', phase='judge')
        self.page.evaluate('pollStatus()')
        self.assertIn('이번 실행은 AI 세부 진행 미계측', self.page.locator('#aiProgress').inner_text())
        self.assertFalse(self.errors, str(self.errors))


if __name__ == '__main__':
    unittest.main()
