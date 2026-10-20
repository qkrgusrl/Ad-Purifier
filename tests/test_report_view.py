"""실제 Handler/Chromium의 경량 이력 조회 계약. RED 실행은 부모 담당.

실행: cd Ad-Purifier && PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/python -B \
    -m unittest discover -s tests -p test_report_view.py -v
운영 history/report/model을 읽지 않고 loopback 임의 포트만 사용한다.
"""
from copy import deepcopy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "20261020-010203"
LARGE = "PRIVATE_LARGE_PAYLOAD_" + "x" * (1024 * 1024)


def report_fixture():
    techniques = ["HOMOGLYPH", "JAMO", "TRANSPARENT", "OFFSCREEN", "POSTED"]
    candidates = []
    for i in range(17):
        candidates.append({
            "key": f"{i:012x}", "url": "https://fixture.invalid/page",
            "location": f"main > a:nth-child({i + 1})", "technique": techniques[i % 5],
            "evidence_text": f"원문_{i:02d}  한글\u200b\t<&>",
            "normalized": f"복원_{i:02d}", "verdict": ["ad", "clean", "uncertain"][i % 3],
            "review": ["new", "confirmed", "false_positive", "resolved"][i % 4],
            "memo": f"검토 메모 {i}", "score": .91, "reason": "검토 근거",
            "processing_status": "completed" if i % 3 != 2 else "review",
            "source_id": f"source-{i}", "signals": [techniques[i % 5]],
            "also_on": [{"url": "https://fixture.invalid/duplicate"}],
        })
    candidates[0].update(source_html=LARGE, source_context=LARGE, context=LARGE,
                         raw=LARGE, gemma={"response": LARGE, "raw": LARGE})
    return {
        "id": RUN_ID, "meta": {"entry_url": "https://fixture.invalid/",
        "started_at": "2026-10-20T01:02:03+09:00", "elapsed_sec": 12.5},
        "pages": 23, "model": "fixture-model", "candidates": candidates,
        "needs_review": True, "unresolved_count": 9, "crawl_incomplete": True,
        "entry_failed": False, "stopped_by_time": True, "left_in_queue": 11,
        "coverage": {"visited_pages": 23, "discovered_pages": 42, "pending_pages": 11,
                     "failed_pages": 8, "failed_frames": 3},
        "failed_pages": [{"url": f"https://fixture.invalid/fail/{i}", "reason": "失敗"} for i in range(8)],
        "failed_frames": [{"url": f"https://fixture.invalid/frame/{i}", "reason": "frame error"} for i in range(3)],
        "http_errors": [{"url": f"https://fixture.invalid/http/{i}", "status": 503} for i in range(7)],
        "ai": {"mode": "diagnostic", "unresolved_sources": [
            {"source_id": f"pending-{i}", "reason": "응답 보류"} for i in range(9)],
            "gemma": {"batches": [{"response": LARGE, "raw": LARGE}]}, "raw": LARGE},
        "source_html": LARGE, "source_context": LARGE, "context": LARGE, "raw": LARGE,
    }


def compact_fixture(full):
    """API 구현과 독립적인 UI 입력 oracle: 화면에 필요한 정보는 모두 유지."""
    value = deepcopy(full)
    for key in ("source_html", "source_context", "context", "raw"):
        value.pop(key)
    for row in value["candidates"]:
        for key in ("source_html", "source_context", "context", "raw", "gemma"):
            row.pop(key, None)
    value["ai"].pop("gemma")
    value["ai"].pop("raw")
    value["ai"]["unresolved_sources"] = deepcopy(full["ai"]["unresolved_sources"][:5])
    value["ai"]["unresolved_sources_count"] = len(full["ai"]["unresolved_sources"])
    return value


class ReportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        self.server_module = importlib.import_module("ui.server")
        self.full = report_fixture()
        self.before = deepcopy(self.full)
        self.load = self.enterContext(patch.object(self.server_module, "load_run", return_value=self.full))
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="report-view-test-"))
        self.enterContext(patch.object(self.server_module, "HISTORY", Path(self.temp)))
        saved = Path(self.temp) / RUN_ID
        saved.mkdir()
        self.saved = {saved / "report.json": b'{"saved":"report sentinel"}',
                      saved / "result.json": b'{"saved":"result sentinel"}'}
        for path, data in self.saved.items():
            path.write_bytes(data)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server_module.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def get(self, suffix=""):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("GET", f"/api/runs/{RUN_ID}{suffix}")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn("application/json", response.getheader("Content-Type"))
            return body, json.loads(body)
        finally:
            connection.close()

    def assert_untouched(self):
        self.assertEqual(self.full, self.before, "경량화가 load_run 반환 객체를 변경함")
        for path, data in self.saved.items():
            self.assertEqual(path.read_bytes(), data, "조회가 저장 파일을 변경함")

    def test_ui_http_preserves_all_candidates_reviews_metadata_and_warning_totals(self):
        body, ui = self.get("?view=ui")
        expected = compact_fixture(self.before)
        self.assertEqual(ui["id"], RUN_ID)
        for key in ("meta", "pages", "model", "needs_review", "unresolved_count", "coverage",
                    "entry_failed", "stopped_by_time", "left_in_queue", "crawl_incomplete"):
            self.assertEqual(ui[key], expected[key], key)
        self.assertEqual(len(ui["candidates"]), 17)
        for actual, original in zip(ui["candidates"], expected["candidates"]):
            for key, value in original.items():
                self.assertEqual(actual[key], value, key)
        for key in ("failed_pages", "failed_frames", "http_errors"):
            self.assertEqual(ui[key], expected[key], "경고 항목/총수 유실: " + key)
        self.assertEqual(ui["ai"]["unresolved_sources"], expected["ai"]["unresolved_sources"])
        self.assertLessEqual(len(ui["ai"]["unresolved_sources"]), 5)
        self.assertEqual(ui["ai"]["unresolved_sources_count"], 9)
        self.assertEqual(ui["ai"]["mode"], "diagnostic")
        self.assert_untouched()
        self.assertNotIn(b"PRIVATE_LARGE_PAYLOAD_", body, "원문/AI 중첩 대형 필드가 HTTP 응답에 남음")
        self.assertLess(len(body), 100_000, "1MB 원문을 가진 fixture의 UI 응답이 경량화되지 않음")
        for obj in [ui, *ui["candidates"]]:
            for key in ("source_html", "source_context", "context", "raw"):
                self.assertNotIn(key, obj)

    def test_default_http_still_returns_full_report_after_ui_read_without_mutation(self):
        self.get("?view=ui")
        body, full = self.get()
        self.assertEqual(full, self.before)
        self.assertEqual(len(full["ai"]["unresolved_sources"]), 9)
        self.assertIn(b"PRIVATE_LARGE_PAYLOAD_", body)
        self.assertEqual(full["ai"]["gemma"]["batches"][0]["response"], LARGE)
        self.assert_untouched()
        self.assertEqual(self.load.call_args.args, (RUN_ID,))


class ReportFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.payload = compact_fixture(report_fixture())
        self.body = json.dumps(self.payload, ensure_ascii=False)
        self.requests = []
        self.errors = []
        html = (ROOT / "ui/static/index.html").read_text(encoding="utf-8")

        def route(request_route):
            url = urlsplit(request_route.request.url)
            if url.netloc != "report-view.test":
                request_route.abort()
                return
            self.requests.append(request_route.request.url)
            if url.path == "/":
                request_route.fulfill(status=200, content_type="text/html", body=html)
            elif url.path == "/api/status":
                request_route.fulfill(json={"running": False, "model_available": True, "out_dir": "fixture"})
            elif url.path == "/api/runs":
                request_route.fulfill(json=[dict(id=RUN_ID, entry_url=self.payload["meta"]["entry_url"],
                    started_at=self.payload["meta"]["started_at"], pages=23, findings=6, needs_review=True)])
            elif url.path == f"/api/runs/{RUN_ID}":
                request_route.fulfill(status=200, content_type="application/json", body=self.body)
            else:
                request_route.abort()

        self.context.route("**/*", route)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto("http://report-view.test/", wait_until="networkidle")
        self.page.locator(f'#history li[data-id="{RUN_ID}"]').wait_for()

    def open(self):
        # 실제 제품 함수를 호출하고 예외만 관찰한다. 함수 교체/가짜 렌더 없음.
        return self.page.evaluate("async id => {try {await openRun(id); return null;} catch(e) {return String(e);}}", RUN_ID)

    def test_compact_response_requests_ui_and_renders_all_candidates_and_details(self):
        error = self.open()
        self.assertIsNone(error)
        requests = [urlsplit(u) for u in self.requests if urlsplit(u).path == f"/api/runs/{RUN_ID}"]
        self.assertTrue(requests)
        self.assertEqual(parse_qs(requests[-1].query).get("view"), ["ui"])
        self.assertTrue(self.page.locator("#resultPanel").is_visible())
        self.assertIn(self.payload["meta"]["entry_url"], self.page.locator("#resultTitle").inner_text())
        self.page.locator("#showExcluded").check()
        self.assertEqual(self.page.locator("#rows tr.row").count(), 17)
        self.assertEqual(self.page.evaluate("current.candidates"), self.payload["candidates"])
        for candidate in self.payload["candidates"]:
            row = self.page.locator(f'#rows tr.row[data-key="{candidate["key"]}"]')
            self.assertEqual(row.locator("td.text").evaluate("el => el.firstChild.textContent"), candidate["evidence_text"])
        candidate = self.payload["candidates"][0]
        self.page.locator(f'#rows tr.row[data-key="{candidate["key"]}"]').click()
        self.assertEqual(self.page.locator("#memoText").input_value(), candidate["memo"])
        warning = self.page.locator("#runWarn").inner_text()
        for item in self.payload["ai"]["unresolved_sources"]:
            self.assertIn(item["source_id"], warning)
            self.assertIn(item["reason"], warning)
        for text in ("8개", "AI 보류 9개", "발견 42페이지", "프레임 수집 실패 3", "외 4건"):
            self.assertIn(text, warning)
        self.assertFalse(self.errors)

    def assert_rejected(self):
        previous = self.page.evaluate("current")
        title = self.page.locator("#resultTitle").text_content()
        error = self.open()
        self.assertEqual(self.page.evaluate("current"), previous, "불완전 응답을 current에 대입함")
        self.assertIsNone(error, "렌더 예외 대신 명시적 오류를 표시해야 함")
        self.assertIn("이력을 열 수 없습니다", self.page.locator("#toast").inner_text())
        self.assertTrue(self.page.locator("#toast").evaluate("el => el.classList.contains('err')"))
        self.assertEqual(self.page.locator("#resultTitle").text_content(), title)
        self.assertFalse(self.errors)
        if previous is None:
            self.assertFalse(self.page.locator("#resultPanel").is_visible(), "실패를 빈 결과로 렌더함")
            self.assertTrue(self.page.locator("#guidePanel").is_visible())

    def test_malformed_json_reports_error_and_preserves_previous_current(self):
        self.assertIsNone(self.open())
        self.body = '{"meta":'
        self.assert_rejected()

    def test_required_shape_errors_never_replace_current_or_render_blank_results(self):
        variants = [{}, {**self.payload, "meta": None},
                    {**self.payload, "meta": {"started_at": "2026-10-20"}},
                    {**self.payload, "candidates": {}},
                    {**self.payload, "candidates": [None]},
                    {**self.payload, "candidates": [{}]},
                    {**self.payload, "http_errors": {}},
                    {**self.payload, "failed_pages": {}}]
        for payload in variants:
            with self.subTest(payload_shape=str(type(payload.get("candidates")))):
                self.page.evaluate("showHome()")
                self.body = json.dumps(payload, ensure_ascii=False)
                self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
