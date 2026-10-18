"""실제 Chromium의 프레임별 원문 캡처 계약. 모델/외부 HTTP/scan_page 없음.

실행: PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/python -B -m unittest discover
      -s tests -p test_dom_browser.py -v
load_extract_js는 매 테스트마다 현재 production JS를 읽는다. baseline RED를
입증할 때만 별도 실행기의 메모리 snapshot으로 이 함수 하나를 대체할 수 있다.
원문 oracle은 HTML 입력 문자열이 아니라 추출 직전 브라우저의 outerHTML이다.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def load_extract_js():
    return (ROOT / "detector/extract.js").read_text(encoding="utf-8")


class DOMBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        # 실제 브라우저. 설치/기동 실패를 skip이나 가짜 DOM 성공으로 바꾸지 않는다.
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self.blocked_urls = []

        def abort(route):
            self.blocked_urls.append(route.request.url)
            route.abort()

        self.context.route("**/*", abort)
        self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.js = load_extract_js()

    def content(self, html):
        self.page.set_content("<!doctype html><meta charset='utf-8'>" + html, wait_until="load")

    def capture(self, frame=None):
        frame = frame or self.page.main_frame
        result = frame.evaluate(self.js)
        self.assertIsInstance(result, dict)
        self.assertTrue("source_blocks" in result,
                        "실제 extract.js 반환값에 source_blocks가 없음: 원문 소유권 캡처 미구현")
        self.assertIsInstance(result["source_blocks"], list)
        self.assertTrue(result["source_blocks"], "텍스트가 있는 DOM의 source_blocks가 비어 있음")
        ids = []
        for row in result["source_blocks"]:
            self.assertIsInstance(row.get("source_id"), str)
            self.assertTrue(row["source_id"].strip())
            self.assertIsInstance(row.get("selector"), str)
            self.assertTrue(row["selector"].strip())
            self.assertIsInstance(row.get("source_html"), str)
            self.assertIsInstance(row.get("source_context"), str,
                                  "source_context는 점수 가중치 목록이 아닌 문맥 문자열이어야 함")
            ids.append(row["source_id"])
        self.assertEqual(len(ids), len(set(ids)), "한 프레임의 source_blocks ID가 중복됨")
        return result

    def owner(self, result, key, frame=None):
        frame = frame or self.page.main_frame
        rows = []
        for row in result["source_blocks"]:
            selected = frame.evaluate("""selector => {
                const nodes = document.querySelectorAll(selector);
                return {count: nodes.length,
                        owner: nodes.length === 1 ? nodes[0].getAttribute('data-test-owner') : null};
            }""", row["selector"])
            self.assertEqual(selected["count"], 1,
                             "수집기 selector가 정확히 한 요소를 선택해야 함: " + row["selector"])
            if selected["owner"] == key:
                rows.append(row)
        self.assertEqual(len(rows), 1, f"DOM 소유 요소 {key!r}의 source block이 정확히 하나여야 함")
        return rows[0]

    def original(self, key, frame=None):
        return (frame or self.page.main_frame).locator(f'[data-test-owner="{key}"]').evaluate("el => el.outerHTML")

    def test_01_real_chromium_fixture_aborts_external_requests(self):
        self.content("<p>로컬 fixture</p>")
        outcome = self.page.evaluate("""async () => {
            try { await fetch('https://fixture.invalid/blocked-proof'); return 'escaped'; }
            catch (_) { return 'blocked'; }
        }""")
        self.assertEqual(outcome, "blocked")
        self.assertIn("https://fixture.invalid/blocked-proof", self.blocked_urls)
        # baseline에서도 실제 JS 평가 자체는 성공했는지 따로 확인한다.
        result = self.page.evaluate(self.js)
        self.assertTrue(any(row["text"] == "로컬 fixture" for row in result["texts"]))

    def test_02_three_links_in_one_paragraph_have_separate_original_owners(self):
        self.content('''<article><p>안내 시작
          <a data-test-owner="one" href="/one">첫 링크</a> 와
          <a data-test-owner="two" href="/two">둘째 링크</a> 및
          <a data-test-owner="three" href="/three">셋째 링크</a> 끝.
        </p></article>''')
        expected = {key: self.original(key) for key in ("one", "two", "three")}
        result = self.capture()
        rows = [self.owner(result, key) for key in expected]
        self.assertEqual(len({row["source_id"] for row in rows}), 3)
        self.assertEqual(len({row["selector"] for row in rows}), 3)
        for key, row in zip(expected, rows):
            self.assertEqual(row["source_html"], expected[key], "문단 전체나 다른 링크를 원문으로 바인딩하면 안 됨")

    def test_03_outer_html_keeps_unicode_whitespace_entities_and_attribute_gt(self):
        self.content('''<p><a data-test-owner="raw" data-note="앞 > 뒤 &amp; 끝"
             href="/offer?a=1&amp;b=2">😀  한글\u200b\t예약\n  &lt;문의&gt;\u00a0끝</a></p>''')
        expected = self.original("raw")
        original_text = self.page.locator('[data-test-owner="raw"]').text_content()
        row = self.owner(self.capture(), "raw")
        self.assertEqual(row["source_html"], expected)
        self.assertEqual(self.original("raw"), expected, "캡처 과정에서 소유 요소 DOM이 바뀜")
        for token in ("😀", "한글", "\u200b", "\t", "\n", "  "):
            self.assertIn(token, row["source_html"])
        self.assertIn("\u00a0", original_text)
        # Chromium 버전별 속성의 > 직렬화 차이는 outerHTML oracle로 검증한다.
        self.assertEqual(self.page.locator('[data-test-owner="raw"]').get_attribute("data-note"), "앞 > 뒤 & 끝")

    def test_04_equal_text_in_different_elements_does_not_merge_source_ids(self):
        self.content('''<section><p><a data-test-owner="a" href="/same">같은  문구</a></p>
             <p><a data-test-owner="b" href="/same">같은  문구</a></p>
             <p><span data-test-owner="c">같은  문구</span></p></section>''')
        expected = {key: self.original(key) for key in ("a", "b", "c")}
        result = self.capture()
        rows = [self.owner(result, key) for key in expected]
        self.assertEqual(len({row["source_id"] for row in rows}), 3)
        for key, row in zip(expected, rows):
            self.assertEqual(row["source_html"], expected[key])

    def test_05_collectors_selectors_resolve_exactly_one_actual_element(self):
        self.content('''<article><section>
          <a class="같은:종류" data-test-owner="first" data-location="MODEL_LOCATION_DO_NOT_USE">첫 원문</a>
          <a class="같은:종류" data-test-owner="second" data-location="MODEL_LOCATION_DO_NOT_USE">둘 원문</a>
        </section><section><a class="같은:종류" data-test-owner="third">셋 원문</a></section></article>''')
        result = self.capture()
        for key in ("first", "second", "third"):
            row = self.owner(result, key)
            self.assertNotEqual(row["selector"], "MODEL_LOCATION_DO_NOT_USE")
            self.assertEqual(row["source_html"], self.original(key))
        # 모델 응답 없이 실제 DOM 수집 결과의 모든 위치를 역참조한다.
        for row in result["source_blocks"]:
            actual = self.page.evaluate("s => document.querySelector(s).outerHTML", row["selector"])
            self.assertEqual(row["source_html"], actual)

    def test_06_source_context_is_nearby_prose_string_not_scoring_weights(self):
        self.content('''<section><h2>문맥제목_상담안내</h2>
          <p>근접문맥_전화상담 전에 <a data-test-owner="target" href="/help">예약 문의</a> 준비물을 확인하세요.</p>
        </section>''')
        row = self.owner(self.capture(), "target")
        context = row["source_context"]
        self.assertIsInstance(context, str)
        self.assertIn("근접문맥_전화상담", context)
        self.assertIn("준비물을 확인하세요", context)
        self.assertNotEqual(context.strip(), "예약 문의", "주변 문맥이 없이 소유 요소 글자만 전달됨")

    def test_07_education_and_ad_sections_never_share_sibling_scope_context(self):
        for tag in ("article", "section"):
            with self.subTest(container=tag):
                self.content(f'''<main>
                  <{tag}><h2>교육범위_예방수업</h2>
                    <p>교육전용문맥_권유를 인용하고 위험을 설명합니다.</p>
                    <p><a data-test-owner="education">가입 문의</a></p>
                  </{tag}>
                  <{tag}><h2>광고범위_판매접수</h2>
                    <p>광고전용문맥_지금 주문하면 할인합니다.</p>
                    <p><a data-test-owner="advert">가입 문의</a></p>
                  </{tag}>
                </main>''')
                result = self.capture()
                education = self.owner(result, "education")["source_context"]
                advert = self.owner(result, "advert")["source_context"]
                self.assertIn("교육전용문맥", education, "대상 문단 밖의 가까운 같은 article/section 문맥 누락")
                self.assertNotIn("광고전용문맥", education)
                self.assertNotIn("광고범위", education)
                self.assertIn("광고전용문맥", advert)
                self.assertNotIn("교육전용문맥", advert)
                self.assertNotIn("교육범위", advert)

    def test_08_hidden_text_and_attribute_records_share_their_actual_owner_id(self):
        self.content('''<article><p>근접 안내
          <a data-test-owner="hidden" href="/x" title="속성의 다른 문구"
             style="opacity:0;position:absolute;left:-10000px">숨긴 원문</a>
          <a data-test-owner="visible" href="/y">숨긴 원문</a>
        </p></article>''')
        expected = self.original("hidden")
        result = self.capture()
        block = self.owner(result, "hidden")
        visible = self.owner(result, "visible")
        self.assertNotEqual(block["source_id"], visible["source_id"])
        hidden = [r for r in result["hidden"] if r["selector"] == block["selector"]]
        texts = [r for r in result["texts"] if r["selector"] == block["selector"]]
        self.assertTrue(hidden)
        self.assertTrue(any(r.get("attr") == "title" for r in texts), "속성 텍스트 캡처 누락")
        self.assertTrue(any(not r.get("attr") for r in texts), "직접 텍스트 캡처 누락")
        self.assertTrue({"TRANSPARENT", "OFFSCREEN"} <= {s[0] for r in hidden for s in r["signals"]})
        for row in hidden + texts:
            self.assertEqual(row.get("source_id"), block["source_id"])
            self.assertEqual(row.get("source_html"), expected)
            self.assertEqual(row.get("source_context"), block["source_context"])

    def test_09_iframe_capture_preserves_frame_local_owners_and_contexts(self):
        self.content('''<article>상위프레임전용문맥
          <a data-test-owner="parent">같은 문구</a></article>
          <iframe name="fixture-child"></iframe>''')
        child = self.page.frame(name="fixture-child")
        self.assertIsNotNone(child)
        child.set_content('''<section>하위프레임전용문맥
          <a data-test-owner="child">같은 문구</a></section>''', wait_until="load")
        parent_original = self.original("parent")
        child_original = self.original("child", child)
        parent_row = self.owner(self.capture(), "parent")
        child_row = self.owner(self.capture(child), "child", child)
        self.assertEqual(parent_row["source_html"], parent_original)
        self.assertEqual(child_row["source_html"], child_original)
        self.assertIn("상위프레임전용문맥", parent_row["source_context"])
        self.assertNotIn("하위프레임전용문맥", parent_row["source_context"])
        self.assertIn("하위프레임전용문맥", child_row["source_context"])
        self.assertNotIn("상위프레임전용문맥", child_row["source_context"])
        # frame-local ID는 서로 같아도 된다. 전역화/iframe 접두어는 scan_page 통합 계약이다.


if __name__ == "__main__":
    unittest.main()
