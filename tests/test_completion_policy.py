"""완료 우선 정책 계약. 실제 run/analyze, OfflineRun 모델·수집 더블만 사용한다."""
import inspect
import io
import sys
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures.ai_contract import OfflineRun, bound_reply, main, prediction, recover, source
from detector.laya_runtime import LayaCleanupError


class CompletionPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="completion-policy-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def execute(self, h, **options):
        with h:
            report = h.run(self.out, **options)
        # allow_nan=False는 내부 무제한 deadline이 보고서에 유출되는 것도 검출한다.
        json.dumps(report, allow_nan=False)
        for path in self.out.glob("*.json"):
            def reject_constant(value):
                self.fail("저장 JSON에 비표준 숫자: " + value)
            json.loads(path.read_text(), parse_constant=reject_constant)
        self.assertEqual(json.loads((self.out / "report.json").read_text()), report)
        return report

    def instrument_deadlines(self, h):
        captured = {"laya": [], "gemma": []}
        runner, client = h.LayaRunner, h.GemmaClient

        class Runner(runner):
            def __init__(self, *args, **kwargs):
                captured["laya"].append(kwargs.get("deadline"))
                super().__init__(*args, **kwargs)

        class Client(client):
            def __init__(self, *args, **kwargs):
                captured["gemma"].append(kwargs.get("deadline"))
                super().__init__(*args, **kwargs)

        h.LayaRunner, h.GemmaClient = Runner, Client
        return captured

    def owners(self, report):
        return {c["source_id"]: c for c in report["candidates"] + report.get("duplicates", [])}

    def test_public_default_is_bounded_and_reports_mode(self):
        parameter = inspect.signature(main.run).parameters.get("complete_validation")
        self.assertIsNotNone(parameter, "main.run에 complete_validation=False 필요")
        self.assertIs(parameter.default, False)
        h = OfflineRun()
        h.after_crawl = lambda: setattr(h, "clock", 10000.)
        report = self.execute(h)
        self.assertEqual(report.get("validation_mode"), "bounded")
        self.assertTrue(report["needs_review"])
        self.assertEqual(h.laya_inputs + h.verify_inputs + h.extract_inputs, [])

    def test_complete_finishes_collected_items_after_collection_budget(self):
        rows = [source("s1", "환전 안내 하나"), source("s2", "환전 안내 둘")]
        h = OfflineRun(rows)
        captured = self.instrument_deadlines(h)
        h.after_crawl = lambda: setattr(h, "clock", 10000.)
        with h:
            report = h.run(self.out, complete_validation=True)
            crawl_call = main.crawl.call_args
        crawl_deadline = crawl_call.kwargs.get("deadline", crawl_call.args[5] if len(crawl_call.args) > 5 else None)
        self.assertEqual(crawl_deadline, 100. + max(60, 600 - 60 - main.JUDGE_RESERVE),
                         "완료 우선이어도 수집 deadline은 기존 계산 유지")
        self.assertEqual(captured["laya"], [None])
        self.assertEqual(len(captured["gemma"]), 1)
        self.assertEqual(captured["gemma"][0], math.inf,
                         "Gemma None은 1800초 기본값이므로 명시적 inf 필요")
        self.assertEqual(report.get("validation_mode"), "complete")
        self.assertEqual(len(h.verify_inputs), 2)
        self.assertTrue(all(c["processing_status"] == "completed" for c in report["candidates"]))
        self.assertFalse(report["needs_review"])
        json.dumps(report, allow_nan=False)
        for path in self.out.glob("*.json"):
            json.loads(path.read_text(), parse_constant=lambda value: self.fail("비표준 JSON " + value))

    def test_complete_continues_when_clock_expires_during_laya_and_gemma(self):
        rows = [source("s1", "환전 안내 하나"), source("s2", "환전 안내 둘")]
        h = OfflineRun(rows)
        def predict(texts):
            h.clock += 10000.
            return [prediction() for _ in texts]
        def verify(item):
            h.clock += 10000.
            return bound_reply(item)
        h.predict, h.verify_reply = predict, verify
        report = self.execute(h, complete_validation=True)
        self.assertEqual(len(h.verify_inputs), 2)
        self.assertFalse(report["needs_review"])
        self.assertEqual(h.entered, h.exited)

    def test_same_hidden_text_is_verified_once_and_verdict_copied_to_each_owner(self):
        # 2026-10-21 정책 변경: 같은 문구·같은 은닉 기법은 Gemma를 대표 1건만 돌리고 판정을 복사한다.
        # 위치가 다르면 출력은 각각 남는다(요강 집계 단위 url + location + technique).
        rows = [source("s1", context="예방 교육 자료"), source("s2", context="상품 안내 게시판")]
        h = OfflineRun(rows, verify=lambda item: bound_reply(item, "ad"))
        report = self.execute(h)
        self.assertEqual(h.laya_inputs, ["환전 안내"], "Laya 실제 text가 같으면 HTML/context와 무관하게 1회")
        self.assertEqual(len(h.verify_inputs), 1, "같은 문구·기법은 Gemma 1회")
        owners = self.owners(report)
        self.assertEqual({sid: c["verdict"] for sid, c in owners.items()}, {"s1": "ad", "s2": "ad"})
        verified = h.verify_inputs[0]["html"]
        rep = next(c for c in owners.values() if c["source_html"] == verified)
        copy = next(c for c in owners.values() if c is not rep)
        self.assertEqual(rep["decided_by"], "gemma")
        self.assertEqual(copy["decided_by"], "gemma_reused")
        self.assertEqual(copy["reused_from"]["source_id"], rep["source_id"])
        self.assertEqual(report["ai"]["gemma_text_reused"], 1)
        submitted = json.loads((self.out / "result.json").read_text(encoding="utf-8"))["findings"]
        self.assertEqual(len(submitted), 2, "위치가 다르면 같은 문구라도 각각 제출")

    def test_same_text_reuses_representative_review(self):
        rows = [source("s1"), source("s2", context="다른 문맥")]
        h = OfflineRun(rows, verify=lambda item: bound_reply(item, "uncertain", "needs_review"))
        report = self.execute(h)
        self.assertEqual(len(h.verify_inputs), 1)
        self.assertEqual({c["verdict"] for c in self.owners(report).values()}, {"uncertain"})
        self.assertTrue(report["needs_review"], "대표가 보류면 복사본도 보류")

    def test_gemma_reuse_key_keeps_posted_context_apart(self):
        from detector.ai import gemma_reuse_key
        hidden = {"technique": "TRANSPARENT", "context": [("page-context", -3)]}
        self.assertEqual(gemma_reuse_key(hidden, "t"), gemma_reuse_key({"technique": "TRANSPARENT"}, "t"),
                         "은닉 4종은 문구+기법만 본다")
        plain = {"technique": "POSTED", "context": []}
        news = {"technique": "POSTED", "context": [("page-context", -3)]}
        self.assertNotEqual(gemma_reuse_key(plain, "t"), gemma_reuse_key(news, "t"),
                            "게시형은 보도·교육 맥락이 다르면 따로 검증")
        self.assertEqual(gemma_reuse_key(news, "t"), gemma_reuse_key(dict(news), "t"))
        self.assertNotEqual(gemma_reuse_key(hidden, "t"), gemma_reuse_key({"technique": "OFFSCREEN"}, "t"))

    def test_same_text_initial_and_residual_reuse_laya_but_verify_in_order_once(self):
        rows = [source("s1", context="초기 후보"), source("s2", context="잔여 원문")]
        h = OfflineRun(rows, candidate_ids=["s1"], extract=recover)
        report = self.execute(h)
        self.assertEqual(h.laya_inputs, ["환전 안내"])
        model_events = [e[0] for e in h.events if e[0] in ("verify", "extract")]
        self.assertEqual(model_events, ["verify", "extract", "verify"],
                         "초기 후보 검증 → 잔여 추출 → 회수 pending 1회 검증 순서 필요")
        self.assertEqual([i["html"] for i in h.verify_inputs], [r["source_html"] for r in rows])
        self.assertEqual(set(self.owners(report)), {"s1", "s2"})

    def test_foreign_binding_cannot_reuse_same_text_owner_verdict(self):
        # 같은 문구·같은 기법은 대표 1건만 검증하므로, 각자 검증되는 다른 기법으로 남의 결합 재사용을 확인한다.
        rows = [source("s1", context="문맥 하나"),
                source("s2", context="문맥 둘", signals=[["OFFSCREEN", "left:-9999px"]])]
        replies = []
        def verify(item):
            if not replies:
                replies.append(bound_reply(item, "clean"))
            return replies[0]
        h = OfflineRun(rows, verify=verify)
        report = self.execute(h)
        self.assertEqual(h.laya_inputs, ["환전 안내"])
        self.assertEqual(len(h.verify_inputs), 2)
        self.assertEqual(sorted(c["verdict"] for c in self.owners(report).values()), ["clean", "uncertain"])
        self.assertTrue(report["needs_review"])

    def test_truncated_or_missing_shared_prediction_keeps_every_owner_review(self):
        for output in ([], [prediction(.99, truncated=True)]):
            with self.subTest(output=output):
                rows = [source("s1", context="문맥 하나"), source("s2", context="문맥 둘")]
                h = OfflineRun(rows, predict=lambda texts: output,
                               verify=lambda item: bound_reply(item, "uncertain", "needs_review"))
                report = self.execute(h)
                self.assertEqual(h.laya_inputs, ["환전 안내"])
                self.assertEqual(h.verify_inputs + h.extract_inputs, [], "유효 예측 없는 모든 owner는 Gemma 없이 보류")
                self.assertEqual(h.entered, 0)
                self.assertEqual(set(self.owners(report)), {"s1", "s2"})
                self.assertTrue(all(c["verdict"] == "uncertain" for c in self.owners(report).values()))
                self.assertEqual(report["unresolved_count"], 2)

    def test_complete_has_no_automatic_candidate_cap(self):
        # 실제 웹/모델 없이 129개 소유 단위로 여러 Laya batch와 모든 검증을 통과한다.
        rows = [source("s%d" % i, "환전 안내 %03d" % i) for i in range(129)]
        h = OfflineRun(rows)
        report = self.execute(h, complete_validation=True)
        self.assertEqual(len(h.laya_inputs), 129)
        self.assertEqual(len(h.verify_inputs), 129)
        self.assertEqual(set(self.owners(report)), {r["source_id"] for r in rows})
        self.assertFalse(report["needs_review"])

    def test_complete_cancel_preserves_files_and_closes_gemma(self):
        cancel = threading.Event()
        originals = {name: "old:" + name for name in ("report.json", "result.json", "result_extra.json")}
        for name, text in originals.items():
            (self.out / name).write_text(text)
        def verify(item):
            cancel.set()
            return bound_reply(item)
        h = OfflineRun(verify=verify)
        with h, self.assertRaises(main.ScanCancelled):
            h.run(self.out, complete_validation=True, cancel=cancel)
        self.assertEqual(len(h.verify_inputs), 1)
        self.assertEqual((h.entered, h.exited), (1, 1))
        for name, text in originals.items():
            self.assertEqual((self.out / name).read_text(), text)

    def test_complete_real_gemma_error_stays_review(self):
        def fail(item):
            raise RuntimeError("fixture_actual_failure")
        h = OfflineRun(verify=fail)
        report = self.execute(h, complete_validation=True)
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["unresolved_count"], 1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["verdict"], "uncertain")
        self.assertIn("fixture_actual_failure", candidate["reason"])
        self.assertEqual(candidate["processing_status"], "error")
        self.assertEqual(h.entered, h.exited)

    def test_app_cli_forwards_complete_option_separately_from_collection_limit(self):
        import app
        from ui import server

        for complete in (False, True):
            with self.subTest(complete=complete):
                forwarded = []
                def serve():
                    forwarded.extend(sys.argv[1:])
                argv = ["--no-browser", "--complete-validation" if complete else "--no-complete-validation"]
                with patch.object(app, "bootstrap"), patch.object(app, "ensure_writable_output"), patch.object(
                    server, "main", side_effect=serve
                ):
                    self.assertEqual(app.main(argv), 0)
                self.assertEqual("--complete-validation" in forwarded, complete)
                self.assertEqual("--no-complete-validation" in forwarded, not complete)
                self.assertNotIn("--no-time-limit", forwarded,
                                 "AI 완료 우선 옵션이 수집 시간 제한 해제로 바뀌면 안 됨")

    def test_detector_cli_complete_option_runs_actual_offline_pipeline(self):
        h = OfflineRun()
        h.after_crawl = lambda: setattr(h, "clock", 10000.)
        argv = ["detector", "https://fixture.invalid/article", "--out", str(self.out),
                "--model", "NONEXISTENT_TEST_WEIGHTS", "--complete-validation"]
        with h, patch.object(sys, "argv", argv), patch.object(sys, "stdout", io.StringIO()):
            main.main()
        report = json.loads((self.out / "report.json").read_text())
        self.assertEqual(report.get("validation_mode"), "complete")
        self.assertEqual(len(h.verify_inputs), 1)
        self.assertFalse(report["needs_review"])

    def test_complete_laya_cleanup_failure_blocks_all_gemma_calls(self):
        h = OfflineRun([source("s1"), source("s2")], candidate_ids=["s1"], extract=recover)
        runner = h.LayaRunner
        class BrokenCleanup(runner):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise LayaCleanupError("fixture_cleanup_failed")
        h.LayaRunner = BrokenCleanup
        report = self.execute(h, complete_validation=True)
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(h.entered, 0)
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["unresolved_count"], 2)
        self.assertIn("fixture_cleanup_failed", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
