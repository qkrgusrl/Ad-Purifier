"""첫 timeout 재시도는 한 번만, 중단 시에는 대기열에 남기는 수집 계약 (페이지 수 제한 없음).

실제 crawl/Frontier 사용. 브라우저 경계, scan_page, sitemap만 대체한다.
모델, 실제 브라우저 및 네트워크를 사용하지 않는다.
"""
import asyncio
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from detector import main


ENTRY = "https://retry.invalid/"
COMPETING = "https://retry.invalid/other"


class CrawlRetryGlueTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, outcomes, *, concurrency=1, sitemap=(), deadline=None,
                      cancel=None, before_timeout=None):
        self.calls = []
        context = SimpleNamespace(
            add_init_script=AsyncMock(), on=Mock(), route=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(
            launch=AsyncMock(return_value=browser)))
        manager = AsyncMock()
        manager.__aenter__.return_value = playwright
        remaining = iter(outcomes)

        async def scan(ctx, url, timeout_ms, on_site=None, guard=None):
            self.calls.append(url)
            self.assertIs(ctx, context)
            # 양보하여 경쟁 worker들이 상한을 확인할 기회를 준다.
            await asyncio.sleep(0)
            try:
                outcome = next(remaining)
            except StopIteration:
                self.fail("예약 페이지 이외의 방문 또는 불필요한 추가 재시도")
            if outcome == "download":
                raise main.PageLoadError("Error: Page.goto: Download is starting")
            if outcome == "timeout":
                if before_timeout is not None:
                    await before_timeout()
                raise TimeoutError("Timeout fixture")
            self.assertEqual(outcome, "success")
            return url, [("", {"links": []})], 200

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "async_playwright", return_value=manager))
            stack.enter_context(patch.object(main, "scan_page", side_effect=scan))
            stack.enter_context(patch.object(main, "_fetch_sitemap", new=AsyncMock(return_value=list(sitemap))))
            pages, issues = await main.crawl(
                ENTRY, concurrency=concurrency, timeout_ms=10,
                deadline=deadline, cancel=cancel)
        self.assertTrue(browser.close.await_count, "중단 또는 완료 후 브라우저 정리 필요")
        return pages, issues

    def assert_success(self, pages, issues, *, retried, pending=0):
        self.assertEqual([url for url, _ in pages], [ENTRY])
        self.assertEqual(issues["failed"], [])
        self.assertEqual(issues["retried"], retried)
        self.assertEqual(issues["left_in_queue"], pending)
        self.assertFalse(issues["stopped_by_time"])
        self.assertEqual(len(issues["visit_log"]), 1)

    async def test_first_timeout_retries_same_page_once(self):
        pages, issues = await self.collect(["timeout", "success"])
        self.assertEqual(self.calls, [ENTRY, ENTRY])
        self.assert_success(pages, issues, retried=1)

    async def test_second_timeout_is_one_failure_without_pending_retry(self):
        pages, issues = await self.collect(["timeout", "timeout"])
        self.assertEqual(self.calls, [ENTRY, ENTRY])
        self.assertEqual(pages, [])
        self.assertEqual(len(issues["failed"]), 1)
        self.assertEqual(issues["failed"][0]["url"], ENTRY)
        self.assertIn("Timeout", issues["failed"][0]["reason"])
        self.assertEqual(issues["retried"], 1)
        self.assertEqual(issues["left_in_queue"], 0)
        self.assertEqual(issues["visit_log"], [])
        self.assertFalse(issues["stopped_by_time"])

    async def test_first_success_is_scanned_and_recorded_only_once(self):
        pages, issues = await self.collect(["success"])
        self.assertEqual(self.calls, [ENTRY])
        self.assert_success(pages, issues, retried=0)

    async def test_file_download_link_is_skipped_not_failed(self):
        # down.php처럼 파일을 내려주는 주소: 다운로드를 막아 두어 goto가 실패하지만 접속 실패로 세지 않고 다시 시도하지도 않는다
        pages, issues = await self.collect(["success", "download"], sitemap=[COMPETING])
        self.assertEqual(self.calls, [ENTRY, COMPETING])
        self.assertEqual([url for url, _ in pages], [ENTRY])
        self.assertEqual(issues["failed"], [])
        self.assertEqual(issues["downloads"], [{"url": COMPETING}])
        self.assertEqual(issues["retried"], 0)

    async def test_entry_download_stays_entry_failure(self):
        pages, issues = await self.collect(["download"])
        self.assertEqual(pages, [])
        self.assertEqual([f["url"] for f in issues["failed"]], [ENTRY])
        self.assertEqual(issues["downloads"], [])

    async def test_deadline_and_cancel_allow_stop_and_keep_retry_pending_visible(self):
        for mode in ("deadline", "cancel"):
            with self.subTest(mode=mode):
                cancel = threading.Event() if mode == "cancel" else None
                deadline = time.perf_counter() + 0.05 if mode == "deadline" else None

                async def stop():
                    if cancel is not None:
                        cancel.set()
                    else:
                        await asyncio.sleep(max(0, deadline - time.perf_counter()) + 0.002)

                pages, issues = await self.collect(
                    ["timeout"], concurrency=5, deadline=deadline,
                    cancel=cancel, before_timeout=stop)
                self.assertEqual(self.calls, [ENTRY])
                self.assertEqual(pages, [])
                self.assertEqual(issues["failed"], [])
                self.assertEqual(issues["retried"], 1)
                self.assertEqual(issues["left_in_queue"], 1,
                                 "중단된 예약 재시도를 대기 건수에서 잃으면 안 된다")
                self.assertEqual(issues["stopped_by_time"], mode == "deadline")
                self.assertEqual(issues["visit_log"], [])


if __name__ == "__main__":
    unittest.main()
