"""끝나지 않는 페이지가 점검 전체를 멈추지 않는 계약.

연수구청 synap 문서 미리보기에서 extract.js가 큰 div 하나의 글자 조각 수천 개마다 그 div 전체를 문맥으로 만들어
evaluate가 끝나지 않았고, 크롤러는 페이지 안 작업에 제한 시간이 없어 526쪽에서 영원히 기다렸다.
- 큰 컨테이너의 문맥 HTML은 상한 안으로 좁히되 대상 원문은 문맥에 정확히 한 번 남는다 (실제 Chromium).
- 프레임 추출이 끝나지 않으면 제한 시간 뒤 수집 실패로 남기고 다음 페이지로 간다 (실제 Chromium).
- 마감 뒤 유예 시간이 지나도 끝나지 않는 페이지가 있으면 브라우저를 닫아 크롤링을 끝낸다 (브라우저 경계만 대체).
"""
import asyncio
import os
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from detector import main

ROOT = Path(__file__).resolve().parents[1]
CONTEXT_HTML_MAX = 20000  # extract.js와 같은 값


class RealChromiumHangTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = await main.launch_browser(self.pw)
        self.addAsyncCleanup(self.browser.close)

    async def test_viewer_like_container_keeps_context_bounded_and_target_anchored(self):
        # synap 미리보기처럼 div 하나에 위치 지정 span 수천 개 (span 하나 ~150자, 합계 수백 KB)
        spans = "".join(f'<span style="position:absolute;left:{i % 90}px;top:{i}px;font-size:12px;'
                        f'letter-spacing:0px;white-space:pre">줄{i} 공고문 본문 글자 조각</span>' for i in range(3000))
        page = await self.browser.new_page()
        self.addAsyncCleanup(page.close)
        await page.set_content(f"<!doctype html><meta charset='utf-8'><div id='doc'>{spans}</div>"
                               "<div><p>앞 문단</p><p id='t'>보통 문단 대상</p><p>뒤 문단</p></div>")
        t0 = time.perf_counter()
        result = await asyncio.wait_for(page.evaluate((ROOT / "detector/extract.js").read_text(encoding="utf-8")), 60)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 30, f"큰 컨테이너 추출이 {elapsed:.1f}초 걸림")
        rows = result["texts"]
        self.assertGreaterEqual(len(rows), 3000)
        for row in rows:
            self.assertLessEqual(len(row["source_context"]), CONTEXT_HTML_MAX, row["text"])
            self.assertEqual(row["source_context"].count(row["source_html"]), 1,
                             "Gemma 앞뒤 문맥 창은 문맥 안에서 대상 원문을 정확히 한 번 찾아야 한다")
        # 상한 안의 보통 문단은 지금처럼 문맥 요소(p 자신)와 앞뒤 형제를 그대로 쓴다
        normal = next(r for r in rows if r["text"] == "보통 문단 대상")
        expected = await page.evaluate("""() => { const t = document.querySelector('#t');
            return [t.previousElementSibling, t, t.nextElementSibling].map(n => n.outerHTML).join('\\n'); }""")
        self.assertEqual(normal["source_context"], expected)

    async def test_frame_extraction_that_never_ends_becomes_collection_failure(self):
        ctx = await main.secure_context(self.browser, main.DEFAULT_UA)
        self.addAsyncCleanup(ctx.close)
        await ctx.route("https://hang.invalid/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><meta charset='utf-8'><p>문서 미리보기</p>"))
        with patch.object(main, "EXTRACT_JS", "() => { for (;;) {} }"), patch.object(main, "EXTRACT_TIMEOUT_S", 2):
            t0 = time.perf_counter()
            final, frames, status = await asyncio.wait_for(main.scan_page(ctx, "https://hang.invalid/", 5000), 60)
        self.assertLess(time.perf_counter() - t0, 30)
        self.assertEqual(status, 200)
        self.assertTrue(frames)
        self.assertIn("초과", frames[0][1]["collection_error"])


class DeadlineForcedStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_that_never_finishes_is_closed_after_deadline_grace(self):
        closed = asyncio.Event()
        context = SimpleNamespace(add_init_script=AsyncMock(), on=Mock(), route=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context),
                                  close=AsyncMock(side_effect=lambda: closed.set()))
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        manager = AsyncMock()
        manager.__aenter__.return_value = playwright

        async def scan(ctx, url, timeout_ms, on_site=None, guard=None):
            await closed.wait()  # 브라우저가 닫히기 전에는 끝나지 않는 페이지
            raise RuntimeError("Target page, context or browser has been closed")

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "async_playwright", return_value=manager))
            stack.enter_context(patch.object(main, "scan_page", side_effect=scan))
            stack.enter_context(patch.object(main, "_fetch_sitemap", new=AsyncMock(return_value=[])))
            stack.enter_context(patch.object(main, "DEADLINE_GRACE_S", 0.1))
            pages, issues = await asyncio.wait_for(main.crawl(
                "https://hang.invalid/", concurrency=2, timeout_ms=10, deadline=time.perf_counter() + 0.1), 10)
        self.assertEqual(pages, [])
        self.assertEqual(issues["failed"], [], "강제 종료로 닫힌 페이지는 접속 실패가 아니다")
        self.assertTrue(issues["stopped_by_time"])


if __name__ == "__main__":
    unittest.main()
