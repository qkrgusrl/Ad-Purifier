"""실제 public run의 RED/GREEN 계약. unittest, 모델/네트워크/브라우저 불필요."""
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path

from fixtures.ai_contract import OfflineRun, URL, bound_reply, bound_source, extract_reply, main, prediction, recover, source


class AIIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ad-purifier-tests-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def run_case(self, h, **kwargs):
        with h:
            report = h.run(self.out, **kwargs)
        saved = json.loads((self.out / "report.json").read_text())
        self.assertEqual(saved, json.loads(json.dumps(report)))
        return report

    def assert_review(self, report, count=1):
        self.assertIs(report.get("needs_review"), True, "미처리/보류가 보고서에서 정상으로 표시됨")
        self.assertIs(type(report.get("unresolved_count")), int)
        self.assertEqual(report["unresolved_count"], count)
        self.assertIsInstance(report.get("ai"), dict)
        for c in report["candidates"]:
            self.assertIn(c["verdict"], {"ad", "clean", "uncertain"})
            if c["verdict"] == "uncertain":
                self.assertTrue(c.get("processing_status"))
                self.assertNotEqual(c["processing_status"], "completed")
                self.assertTrue(c.get("reason"))

    def test_rule_extremes_stay_rules_and_call_neither_model(self):
        rows = [source("s1", "카지노 바카라 가입코드"), source("s2", "도서관 안내")]
        h = OfflineRun(rows)
        report = self.run_case(h)
        self.assertEqual({c["evidence_text"]: c["verdict"] for c in report["candidates"]},
                         {rows[0]["text"]: "ad", rows[1]["text"]: "clean"})
        self.assertTrue(all(c["decided_by"] == "rules" for c in report["candidates"]))
        self.assertEqual(h.laya_inputs, [])
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])

    def test_borderline_low_confidence_reaches_gemma_and_can_reverse_laya(self):
        h = OfflineRun(verify=lambda x: bound_reply(x, "clean"))
        report = self.run_case(h)
        self.assertEqual(len(h.verify_inputs), 1, "실제 run에서 낮은 confidence가 Gemma에 도달하지 않음")
        self.assertEqual(report["candidates"][0]["verdict"], "clean")
        self.assertEqual(h.entered, h.exited)
        self.assertGreater(h.entered, 0)

    def test_low_confidence_clean_is_uncertain_when_gemma_abstains(self):
        h = OfflineRun(predict=lambda ts: [prediction(.45) for _ in ts],
                       verify=lambda x: bound_reply(x, "uncertain", "needs_review"))
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain",
                         "Laya 0.45를 검증 없이 clean으로 확정하면 안 됨")
        self.assert_review(report)

    def test_confident_borderline_uses_laya_without_gemma(self):
        for p, verdict in ((.8, "ad"), (.1, "clean")):
            with self.subTest(p=p):
                h = OfflineRun(predict=lambda ts: [prediction(p) for _ in ts])
                report = self.run_case(h)
                self.assertEqual(len(h.laya_inputs), 1)
                self.assertEqual(h.verify_inputs, [])
                self.assertEqual(report["candidates"][0]["verdict"], verdict)

    def test_threshold_is_configurable_without_changing_rule_thresholds(self):
        self.assertIn("laya_confidence_threshold", inspect.signature(main.run).parameters,
                      "public run에 초기값 0.8의 설정 가능한 seam 필요")
        self.assertEqual(inspect.signature(main.run).parameters["laya_confidence_threshold"].default, .8)
        h = OfflineRun(predict=lambda ts: [prediction(.85) for _ in ts])
        self.run_case(h, laya_confidence_threshold=.9)
        self.assertEqual(len(h.verify_inputs), 1)
        self.assertEqual((main.adscore.LOW, main.adscore.HIGH), (1.5, 4.0))

    def test_completed_report_distinguishes_clean_from_review(self):
        h = OfflineRun(verify=lambda x: bound_reply(x, "clean"))
        report = self.run_case(h)
        self.assertIs(report.get("needs_review"), False)
        self.assertIs(type(report.get("unresolved_count")), int)
        self.assertEqual(report["unresolved_count"], 0)
        self.assertIsInstance(report.get("ai"), dict)
        c = report["candidates"][0]
        self.assertEqual(c["verdict"], "clean")
        self.assertEqual(c.get("processing_status"), "completed")
        self.assertTrue(c.get("reason"))

    def test_laya_error_is_verified_by_gemma(self):
        def fail(texts):
            raise RuntimeError("fixture_laya_error")
        h = OfflineRun(predict=fail, verify=lambda x: bound_reply(x, "clean"))
        report = self.run_case(h)
        self.assertEqual(len(h.verify_inputs), 1, "Laya 오류 후보가 검증 없이 누락됨")
        self.assertEqual(report["candidates"][0]["verdict"], "clean")

    def test_short_laya_batch_verifies_each_unreturned_item(self):
        rows = [source("s1", "환전 안내 하나"), source("s2", "환전 안내 둘")]
        h = OfflineRun(rows, predict=lambda ts: [prediction(.99)])
        report = self.run_case(h)
        self.assertEqual(len(h.verify_inputs), 1, "zip으로 짧은 배치 출력의 나머지를 버리면 안 됨")
        verified_html = h.verify_inputs[0]["html"]
        missing_text = sorted(h.laya_inputs)[1]
        self.assertIn(missing_text, verified_html)
        self.assertTrue(all(c["verdict"] == "ad" for c in report["candidates"]))

    def test_truncated_laya_output_must_not_be_confident(self):
        h = OfflineRun(predict=lambda ts: [prediction(.99, truncated=True) for _ in ts])
        self.run_case(h)
        self.assertEqual(len(h.verify_inputs), 1, "잘린 Laya 입력/출력의 0.99를 확정하면 안 됨")

    def test_educational_borderline_skips_laya_but_reaches_gemma(self):
        row = source(text="카지노 바카라 가입코드")
        row["signals"] = []
        data = {"frame_url": URL, "hidden": [], "texts": [row], "links": [],
                "head": [{"selector": "head > title", "text": "도박 예방 안내", "raw": "도박 예방 안내"}],
                "source_blocks": [row]}
        # 9점 - page-context 4점 = 5점은 확정이므로, 6점 - 4점 = 2점 경계 문구를 사용한다.
        row.update(text="카지노 바카라", raw="카지노 바카라",
                   source_html='<a href="/lesson">카지노 바카라</a>')
        h = OfflineRun(page_data=[(URL, [("", data)])], verify=lambda x: bound_reply(x, "clean"))
        report = self.run_case(h, detect_posted=True)
        self.assertEqual(h.laya_inputs, [], "기존 예방 문맥 Laya 우회가 바뀜")
        self.assertEqual(len(h.verify_inputs), 1, "교육 문맥 경계 후보도 Gemma 검증 필요")
        self.assertEqual(report["candidates"][0]["verdict"], "clean")

    def test_gemma_error_keeps_uncertain_and_out_of_result(self):
        def fail(item):
            raise TimeoutError("fixture_gemma_timeout")
        h = OfflineRun(verify=fail)
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)
        self.assertEqual(json.loads((self.out / "result.json").read_text())["findings"], [])
        self.assertEqual(h.entered, h.exited)

    def test_review_status_cannot_be_overridden_by_clean_verdict(self):
        h = OfflineRun(verify=lambda x: bound_reply(x, "clean", "needs_review"))
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

    def test_empty_verify_results_are_unprocessed_not_clean(self):
        h = OfflineRun(predict=lambda ts: [prediction(.45) for _ in ts],
                       verify=lambda x: {"task": "verify", "version": "v15", "status": "ok", "results": []})
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

    def test_verify_result_cannot_bind_to_foreign_candidate_id(self):
        def foreign(item):
            response = bound_reply(item, "clean")
            response["results"][0]["candidate_id"] = "not-requested"
            return response
        h = OfflineRun(predict=lambda ts: [prediction(.45) for _ in ts], verify=foreign)
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

    def test_deadline_marks_unprocessed_borderline_review_without_model_calls(self):
        h = OfflineRun()
        h.after_crawl = lambda: setattr(h, "clock", 10000.)
        report = self.run_case(h)
        self.assert_review(report)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assertEqual(h.laya_inputs + h.verify_inputs + h.extract_inputs, [])

    def test_cancel_before_judgment_preserves_existing_files(self):
        cancel = threading.Event()
        for name in ("result.json", "report.json", "result_extra.json"):
            (self.out / name).write_text("old-" + name)
        h = OfflineRun()
        h.after_crawl = cancel.set
        with h, self.assertRaises(main.ScanCancelled):
            h.run(self.out, cancel=cancel)
        for name in ("result.json", "report.json", "result_extra.json"):
            self.assertEqual((self.out / name).read_text(), "old-" + name)
        self.assertEqual(h.laya_inputs + h.verify_inputs, [])

    def test_cancel_during_gemma_closes_client_and_does_not_save(self):
        cancel = threading.Event()
        def verify(item):
            cancel.set()
            return bound_reply(item)
        h = OfflineRun(verify=verify)
        with h, self.assertRaises(main.ScanCancelled):
            h.run(self.out, cancel=cancel)
        self.assertFalse((self.out / "report.json").exists())
        self.assertFalse((self.out / "result.json").exists())
        self.assertGreater(h.entered, 0)
        self.assertEqual(h.entered, h.exited)

    def test_gemma_cancelled_error_becomes_scan_cancelled_and_preserves_files(self):
        import asyncio

        cancel = threading.Event()
        originals = {name: ('{"existing": "%s"}\n' % name).encode()
                     for name in ("result.json", "report.json")}
        for name, content in originals.items():
            (self.out / name).write_bytes(content)

        def verify(item):
            cancel.set()
            raise asyncio.CancelledError("fixture_budget_cancelled")

        h = OfflineRun(verify=verify)
        caught = None
        try:
            with h:
                h.run(self.out, cancel=cancel)
        except BaseException as exc:
            # CancelledError는 Exception 밖에 있다. runner 오류 대신 아래 타입 assertion으로 RED를 남긴다.
            caught = exc

        self.assertTrue(cancel.is_set(), "Gemma 취소 경로가 실행되지 않음")
        self.assertEqual(len(h.verify_inputs), 1)
        self.assertEqual((h.entered, h.exited), (1, 1), "취소 시 client context 정리 필요")
        for name, content in originals.items():
            self.assertEqual((self.out / name).read_bytes(), content, name + " 기존 파일 변경됨")
        self.assertIsInstance(caught, main.ScanCancelled,
                              "공개 run은 UI가 처리할 ScanCancelled로 변환해야 함; 실제: "
                              + type(caught).__name__)

    def test_no_ml_is_explicit_diagnostic_not_clean_certification(self):
        h = OfflineRun()
        report = self.run_case(h, use_ml=False)
        self.assertEqual(h.laya_inputs + h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)
        self.assertEqual(report["ai"].get("mode"), "diagnostic")

    def test_residual_three_anchors_rejoin_judge_and_gemma(self):
        rows = [source("s%d" % i, "환전 안내 %d" % i) for i in range(1, 4)]
        paragraph = '<p class="article">' + " / ".join(r["source_html"] for r in rows) + '</p>'
        for row in rows:
            row["source_context"] = paragraph
        h = OfflineRun(rows, candidate_ids=[], extract=recover)
        report = self.run_case(h)
        self.assertEqual(len(report["candidates"]), 3, "한 문단의 잔여 a 세 개가 회수되지 않음")
        self.assertEqual({c.get("source_id") for c in report["candidates"]}, {"s1", "s2", "s3"})
        self.assertEqual(len(h.verify_inputs), 3)
        self.assertTrue(all(c["verdict"] == "ad" for c in report["candidates"]))
        self.assertEqual(len(h.extract_inputs), 3, "동일 source를 재추출하면 안 됨")
        self.assertTrue(all(set(req) == {"blocks"} for req in h.extract_requests))
        self.assertEqual({b["context"] for b in h.extract_inputs}, {paragraph})
        for block in h.extract_inputs:
            before = next(i for i, e in enumerate(h.events) if e[0] == "extract" and block in e[1])
            text = bound_source(block)["evidence"]
            self.assertTrue(any(e[0] == "laya" and any(text in t for t in e[1]) for e in h.events[:before]),
                            "잔여 소유 단위도 먼저 Laya를 통과해야 함")

    def test_residual_excludes_used_source_id_only_not_duplicate_text(self):
        rows = [source("s1"), source("s2")]
        h = OfflineRun(rows, candidate_ids=["s1"], extract=recover)
        report = self.run_case(h)
        self.assertEqual([b["block_id"] for b in h.extract_inputs], ["s2"],
                         "문구가 같아도 다른 소유 단위를 잔여에서 제외하면 안 됨")
        # 기존 제출 dedupe를 유지해도 보고서에서 원래 소유권은 추적 가능해야 한다.
        owners = {c.get("source_id") for c in report["candidates"] + report.get("duplicates", [])}
        self.assertEqual(owners, {"s1", "s2"})

    def test_residual_high_confidence_clean_does_not_call_gemma(self):
        h = OfflineRun([source(text="도서관 안내")], candidate_ids=[],
                       predict=lambda ts: [prediction(.01) for _ in ts])
        report = self.run_case(h)
        self.assertEqual(len(h.laya_inputs), 1, "잔여 원문은 Laya screening이 필요")
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(report["candidates"], [])

    def test_partial_extract_reports_unresolved_owner(self):
        def partial(data):
            return extract_reply(data, failures=[{"block_ids": [b["block_id"] for b in data["blocks"]],
                                                 "error": "generation_incomplete:length",
                                                 "action": "재검토 필요"}],
                                 calls=[{"status": "error", "error": "generation_incomplete:length"}])
        h = OfflineRun(candidate_ids=[], extract=partial)
        report = self.run_case(h)
        self.assert_review(report)
        self.assertEqual(report["candidates"], [])
        self.assertIn("s1", json.dumps(report["ai"]))

    def test_empty_extract_response_does_not_silently_clear_suspicious_residual(self):
        # 선별된 block에 candidates/failures/calls가 모두 비어 있으면 미처리다.
        h = OfflineRun(candidate_ids=[])
        report = self.run_case(h)
        self.assert_review(report)
        self.assertEqual(report["candidates"], [])

    def test_residual_deadline_keeps_owner_unresolved(self):
        h = OfflineRun(candidate_ids=[])
        h.after_crawl = lambda: setattr(h, "clock", 10000.)
        report = self.run_case(h)
        self.assert_review(report)
        self.assertEqual(h.laya_inputs + h.verify_inputs + h.extract_inputs, [])
        self.assertIn("s1", json.dumps(report["ai"]))

    def test_deadline_after_laya_does_not_start_gemma(self):
        h = OfflineRun()
        def predict(texts):
            h.clock = 10000.
            return [prediction(.55) for _ in texts]
        h.predict = predict
        report = self.run_case(h)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)
        self.assertEqual(h.verify_inputs, [])

    def test_recovered_rule_ad_stays_rule_ad_without_gemma_verify(self):
        h = OfflineRun([source(text="카지노 바카라 가입코드")], candidate_ids=[], extract=recover)
        report = self.run_case(h)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["verdict"], "ad")
        self.assertEqual(report["candidates"][0]["decided_by"], "rules")
        self.assertGreater(len(h.laya_inputs), 0, "회수 전 잔여 Laya screening은 필요")
        self.assertEqual(h.verify_inputs, [], "회수 뒤에도 기존 확정 rule ad는 Gemma 대상이 아님")

    def test_forged_recovered_id_is_rejected_and_never_verified(self):
        def forged(data):
            return extract_reply(data, candidates=[{"block_id": "not-collected", "evidence": "환전 안내",
                                                     "selector": "body", "technique": "HOMOGLYPH"}])
        h = OfflineRun(candidate_ids=[], extract=forged)
        report = self.run_case(h)
        self.assert_review(report)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(h.verify_inputs, [])

    def test_recovered_model_cannot_overwrite_selector_or_technique(self):
        def contaminated(data):
            response = recover(data)
            for item in response["candidates"]:
                item.update(selector="body", location="body", technique="HOMOGLYPH")
            return response
        row = source()
        h = OfflineRun([row], candidate_ids=[], extract=contaminated)
        report = self.run_case(h)
        # 부가 필드 무시 또는 전체 응답 보류 모두 안전하다. 원문 소유권 누락은 불가.
        self.assertTrue(report["candidates"] or report.get("needs_review"), "회수 결과를 조용히 버리면 안 됨")
        for c in report["candidates"]:
            self.assertEqual(c["location"], row["selector"])
            self.assertEqual(c["technique"], "TRANSPARENT")

    def test_repeated_recovery_of_same_source_is_finite(self):
        def twice(data):
            response = recover(data)
            response["candidates"] *= 2
            return response
        h = OfflineRun(candidate_ids=[], extract=twice)
        report = self.run_case(h)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(len(h.extract_inputs), 1)
        self.assertEqual(len(h.verify_inputs), 1)

    def test_result_retains_four_techniques_and_excludes_uncertain(self):
        rows = [source("s1", "카지노 바카라", signals=[["TRANSPARENT", "opacity:0"],
                                                        ["OFFSCREEN", "display:none"]]),
                source("s2", "환전 안내")]
        h = OfflineRun(rows, verify=lambda x: bound_reply(x, "uncertain", "needs_review"))
        report = self.run_case(h)
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(set(result), {"meta", "findings"})
        self.assertEqual({f["technique"] for f in result["findings"]}, {"TRANSPARENT", "OFFSCREEN"})
        self.assertEqual(len(result["findings"]), 2)
        for f in result["findings"]:
            self.assertEqual(set(f), {"id", "url", "is_violation", "location", "evidence_text", "technique"})
        self.assert_review(report)


if __name__ == "__main__":
    unittest.main()
