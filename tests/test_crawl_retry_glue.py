"""페이지 상한에서도 첫 timeout 재시도의 슬롯을 보존하는 수집 계약.

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
                      cancel=None, before_timeout=None, max_pages=1):
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
                ENTRY, max_pages=max_pages, concurrency=concurrency, timeout_ms=10,
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

    async def test_first_timeout_retries_same_page_at_max_pages_one(self):
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

    async def test_five_workers_preserve_slot_against_competing_pending_page(self):
        pages, issues = await self.collect(
            ["timeout", "success"], concurrency=5, sitemap=[COMPETING])
        self.assertEqual(self.calls, [ENTRY, ENTRY],
                         "상한의 예약 슬롯은 같은 URL 재시도에만 사용해야 한다")
        self.assert_success(pages, issues, retried=1, pending=1)

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


    async def test_slow_mode_finishes_reserved_retry_with_and_without_competitor(self):
        for competing in (False, True):
            with self.subTest(competing=competing):
                a, b, c = ENTRY, "https://retry.invalid/b", "https://retry.invalid/c"
                fresh = "https://retry.invalid/fresh"
                counts = {a: 0, b: 0, c: 0}
                calls, timeouts = [], []
                all_started = asyncio.Event()
                a_failed = asyncio.Event()
                ab_failed = asyncio.Event()
                context = SimpleNamespace(
                    add_init_script=AsyncMock(), on=Mock(), route=AsyncMock())
                browser = SimpleNamespace(
                    new_context=AsyncMock(return_value=context), close=AsyncMock())
                manager = AsyncMock()
                manager.__aenter__.return_value = SimpleNamespace(
                    chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

                def progress(done, discovered, url):
                    # progress는 except/finally 처리 뒤 호출되므로 timeout 관측 순서를 보장한다.
                    if done >= 1:
                        a_failed.set()
                    if done >= 2:
                        ab_failed.set()

                async def scan(ctx, url, timeout_ms, on_site=None, guard=None):
                    calls.append(url)
                    self.assertIs(ctx, context)
                    if url not in counts:
                        self.fail("예약한 3개 슬롯 밖의 새 URL을 방문함: " + url)
                    counts[url] += 1
                    if all(counts.values()):
                        all_started.set()
                    if url == a:
                        await all_started.wait()
                    elif url == b:
                        await a_failed.wait()
                    else:
                        await ab_failed.wait()
                        if counts[c] == 2:
                            return c, [("", {"links": []})], 200
                    if counts[url] > 2:
                        self.fail("기존 1회 재시도 한도를 초과함")
                    timeouts.append(url)
                    raise TimeoutError("Timeout fixture slow transition")

                sitemap = [b, c] + ([fresh] if competing else [])
                with ExitStack() as stack:
                    stack.enter_context(patch.object(main, "async_playwright", return_value=manager))
                    stack.enter_context(patch.object(main, "scan_page", side_effect=scan))
                    stack.enter_context(patch.object(
                        main, "_fetch_sitemap", new=AsyncMock(return_value=sitemap)))
                    pages, issues = await asyncio.wait_for(main.crawl(
                        a, max_pages=3, concurrency=5, timeout_ms=10,
                        progress=progress), timeout=1.0)

                self.assertEqual(timeouts, [a, a, b, b, c],
                                 "자연스러운 다섯 번째 timeout에서 C의 예약 재시도가 느린 모드에 진입")
                self.assertEqual(calls, [a, b, c, a, b, c])
                self.assertEqual(counts, {a: 2, b: 2, c: 2})
                self.assertNotIn(fresh, calls)
                self.assertEqual([url for url, _ in pages], [c])
                self.assertEqual(len(issues["failed"]), 2)
                self.assertEqual({row["url"] for row in issues["failed"]}, {a, b})
                self.assertTrue(all("Timeout" in row["reason"] for row in issues["failed"]))
                self.assertEqual(issues["retried"], 3)
                self.assertEqual(issues["left_in_queue"], int(competing))
                self.assertTrue(issues["slow_mode"])
                self.assertFalse(issues["stopped_by_time"])
                self.assertEqual(len(issues["visit_log"]), 1)
                self.assertTrue(browser.close.await_count)


    async def test_finite_cap_preserves_retry_slot_before_competitor_uses_last_slot(self):
        pages, issues = await self.collect(
            ["timeout", "success", "success"], max_pages=2,
            concurrency=1, sitemap=[COMPETING])
        self.assertEqual(self.calls, [ENTRY, ENTRY, COMPETING])
        self.assertEqual([url for url, _ in pages], [ENTRY, COMPETING])
        self.assertEqual(issues["failed"], [])
        self.assertEqual(issues["left_in_queue"], 0)
        self.assertEqual(issues["retried"], 1)
        self.assertEqual(len(issues["visit_log"]), 2)
        self.assertFalse(issues["stopped_by_time"])


if __name__ == "__main__":
    unittest.main()
