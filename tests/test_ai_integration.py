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

    def test_reviewer_fixture_uses_precreated_loop_and_still_blocks_network(self):
        import asyncio
        import socket

        policy = asyncio.get_event_loop_policy()
        original_run = asyncio.run
        h = OfflineRun([source(text="도서관 안내")])
        with h:
            self.assertFalse(h.loop.is_closed())
            h.run(self.out)
            with socket.socket() as sock, self.assertRaisesRegex(AssertionError, "네트워크 금지"):
                sock.connect(("203.0.113.1", 443))
            with self.assertRaisesRegex(AssertionError, "네트워크 금지"):
                socket.create_connection(("203.0.113.1", 443))
            self.assertIs(asyncio.run, original_run, "전역 asyncio 실행기를 바꾸면 안 됨")
            self.assertIs(asyncio.get_event_loop_policy(), policy)
        self.assertTrue(h.loop.is_closed(), "fixture loop 누수")
        self.assertIs(main.asyncio.run, original_run)

    def test_reviewer_visible_zero_score_recovery_rejoins_as_posted(self):
        from fixtures.reviewer_dom import capture

        text = "처방전 없이 수면제를 판매합니다"
        page_data = capture('<p>' + text + '</p>')
        self.assertEqual(main.adscore.score(main.normalize(text))[0], 0)
        self.assertEqual(main.candidates_from(page_data, detect_posted=True), [])
        h = OfflineRun(page_data=page_data, predict=lambda ts: [prediction(.99) for _ in ts], extract=recover)
        report = self.run_case(h, detect_posted=True)
        self.assertEqual(h.extract_inputs + h.verify_inputs, [], "고신뢰 잔여는 Gemma 없이 결정론 회수")
        self.assertEqual(len(report["candidates"]), 1, "원문 결합 회수를 같은 키워드 필터로 다시 버리면 안 됨")
        c = report["candidates"][0]
        row = page_data[0][1][0][1]["source_blocks"][0]
        self.assertEqual((c["technique"], c["location"], c["evidence_text"]), ("POSTED", row["selector"], text))
        self.assertEqual(c["source_html"], row["source_html"])
        self.assertEqual(c["verdict"], "ad", "회수 근거가 있는데 규칙 0점만으로 정상 확정하면 안 됨")

    def test_reviewer_detect_posted_false_also_applies_to_residual_recovery(self):
        from fixtures.reviewer_dom import capture

        page_data = capture('<p>환전 안내</p><span style="opacity:0">카지노 바카라</span>')
        h = OfflineRun(page_data=page_data, extract=recover)
        report = self.run_case(h, detect_posted=False)
        self.assertFalse(any(c["technique"] == "POSTED" for c in report["candidates"]),
                         "잔여 재합류가 detect_posted=False를 무시함")
        self.assertTrue(any(c["technique"] == "TRANSPARENT" and c["verdict"] == "ad"
                            for c in report["candidates"]), "기존 은닉 탐지는 유지해야 함")
        self.assertEqual(report["extra_count"], 0)
        self.assertFalse((self.out / "result_extra.json").exists())

    def test_reviewer_dom_attribute_residual_not_discarded_with_used_body(self):
        from fixtures.reviewer_dom import capture

        residual_text = "처방전 없이 수면제를 판매합니다"
        page_data = capture('<a title="' + residual_text + '">환전 안내</a>')

        def recover_attribute(data):
            recovered = []
            for b in data["blocks"]:
                if residual_text in b["html"]:
                    start = b["html"].index(residual_text)
                    recovered.append({**bound_source(b), "block_id": b["block_id"],
                                      "evidence": residual_text, "start_char": start,
                                      "end_char": start + len(residual_text)})
            return extract_reply(data, candidates=recovered)

        h = OfflineRun(page_data=page_data, extract=recover_attribute)
        report = self.run_case(h, detect_posted=True)
        self.assertIn(residual_text, [c["evidence_text"] for c in report["candidates"]],
                      "본문이 후보에 쓰였다는 이유로 같은 요소의 미처리 속성 문구를 잔여에서 누락함")

    def test_reviewer_explicit_relative_model_path_is_relative_to_app_root(self):
        from contextlib import chdir
        from app_paths import app_root

        expected = app_root() / "models" / "custom"
        h = OfflineRun(predict=lambda ts: [prediction(.99) for _ in ts])
        with chdir(self.out):
            self.run_case(h, model="models/custom")
        self.assertEqual([Path(p) for p in h.model_paths], [expected],
                         "명시적 상대 모델 경로도 cwd가 아닌 app root 기준으로 해석해야 함")

    def test_reviewer_legacy_history_restores_review_for_unfinished_work(self):
        from unittest.mock import patch
        from ui import server

        cases = {
            "borderline": {"candidates": [{"url": URL, "location": "p", "technique": "POSTED",
                                            "evidence_text": "환전 안내", "verdict": "borderline"}]},
            "failed": {"failed_pages": [{"url": URL + "/failed", "reason": "timeout"}]},
            "unvisited": {"left_in_queue": 2, "stopped_by_time": True},
        }
        history = self.out / "history"
        with patch.object(server, "HISTORY", history):
            for index, (case, updates) in enumerate(cases.items(), 1):
                run_id = "20261018-12000%d" % index
                folder = history / run_id
                folder.mkdir(parents=True)
                report = {"meta": {"entry_url": URL, "started_at": "2026-10-18T12:00:00+09:00", "elapsed_sec": 1},
                          "pages": 1, "candidates": [], "failed_pages": [], "left_in_queue": 0,
                          "stopped_by_time": False, **updates}
                path = folder / "report.json"
                original = json.dumps(report, ensure_ascii=False).encode()
                path.write_bytes(original)
                with self.subTest(case=case, surface="load_run"):
                    loaded = server.load_run(run_id)
                    self.assertIs(loaded.get("needs_review"), True, "과거 미처리 결과를 정상으로 복원함")
                with self.subTest(case=case, surface="list_runs"):
                    summary = next(r for r in server.list_runs() if r["id"] == run_id)
                    self.assertIs(summary.get("needs_review"), True, "이력 목록에서 보류가 정상으로 표시됨")
                self.assertEqual(path.read_bytes(), original, "조회가 과거 증거 파일을 덮어쓰면 안 됨")

    def test_reviewer_same_element_review_keys_isolate_evidence_and_keep_legacy_review(self):
        import hashlib
        from unittest.mock import patch
        from ui import server

        rows = [{"url": URL, "location": "a", "technique": "POSTED", "verdict": "ad",
                 "source_id": "dom-1", "attr": attr, "evidence_text": text, "raw": text}
                for attr, text in (("", "환전 안내 하나"), ("title", "환전 안내 둘"),
                                   ("aria-label", "환전 안내 셋"))]
        keys = [server.cand_key(c) for c in rows]
        with self.subTest(check="distinct_keys"):
            self.assertEqual(len(set(keys)), 3, "동일 요소의 서로 다른 근거가 같은 검토 키를 공유함")
        legacy = {"url": URL, "location": "p.legacy", "technique": "POSTED",
                  "verdict": "ad", "evidence_text": "예전 보고서 문구"}
        old_key = hashlib.sha1(f"{URL}|p.legacy|POSTED".encode()).hexdigest()[:12]
        history = self.out / "history"
        run_id = "20261018-130000"
        folder = history / run_id
        folder.mkdir(parents=True)
        report = {"meta": {"entry_url": URL, "started_at": "2026-10-18T13:00:00+09:00", "elapsed_sec": 1},
                  "pages": 1, "candidates": rows + [legacy]}
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (folder / "review.json").write_text(json.dumps({keys[0]: {"status": "false_positive", "memo": "첫 문구만"},
                                                         old_key: {"status": "resolved", "memo": "기존 검토"}}),
                                            encoding="utf-8")
        with patch.object(server, "HISTORY", history):
            loaded = server.load_run(run_id)["candidates"]
        with self.subTest(check="review_isolation"):
            self.assertEqual([(c["review"], c["memo"]) for c in loaded[:3]],
                             [("false_positive", "첫 문구만"), ("new", ""), ("new", "")],
                             "한 문구의 검토 상태/메모가 다른 근거에 전파됨")
        with self.subTest(check="legacy_fallback"):
            self.assertEqual((loaded[3]["review"], loaded[3]["memo"]), ("resolved", "기존 검토"),
                             "source_id 없는 과거 검토 키 보존 또는 명시 fallback 필요")

    def test_reviewer_extract_context_only_evidence_cannot_create_owner_ad(self):
        context_ad = "카지노 바카라 가입코드"
        row = source(text="환전 안내", html="<p>환전 안내</p>", context="주변의 다른 요소: " + context_ad,
                     signals=[])
        row.update(selector="p", tag="p", href=None)

        def context_recovery(data):
            candidates = []
            for b in data["blocks"]:
                start = b["context"].index(context_ad)
                candidates.append({**bound_source(b), "block_id": b["block_id"], "source_field": "context",
                                   "evidence_id": "c0001", "evidence": context_ad,
                                   "start_char": start, "end_char": start + len(context_ad)})
            return extract_reply(data, candidates=candidates)

        h = OfflineRun([row], candidate_ids=[], extract=context_recovery)
        report = self.run_case(h, detect_posted=True)
        with self.subTest(stage="extract_location"):
            self.assertFalse(any(c["verdict"] == "ad" for c in report["candidates"]),
                             "context에만 있는 광고 근거를 source_html 위치의 광고로 보고함")
        with self.subTest(stage="extract_review"):
            self.assert_review(report)

        # 예방 문맥은 읽되 clean의 선택 근거는 대상 HTML에 결합해야 한다.
        prevention = "도박 예방교육 자료로 인용한 문구입니다."
        normal = source(context=prevention)

        def verify_context(item):
            response = bound_reply(item, "clean")
            binding = {"source_field": "context", "evidence_id": "c0001", "evidence": prevention,
                       "start_char": 0, "end_char": len(prevention)}
            response["results"][0].update(binding)
            response["results"][0]["call"]["parsed"].update(binding)
            return response

        with self.subTest(stage="verify_context_only_review"):
            verified = self.run_case(OfflineRun([normal], verify=verify_context))
            self.assert_review(verified)
            self.assertEqual(verified["candidates"][0]["verdict"], "uncertain")

        with self.subTest(stage="verify_related_context_target_html_allowed"):
            verified = self.run_case(OfflineRun([normal], verify=lambda item: bound_reply(item, "clean")))
            self.assertEqual(verified["candidates"][0]["verdict"], "clean")
            self.assertIs(verified["needs_review"], False)

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

    def test_laya_error_stays_review_without_gemma(self):
        def fail(texts):
            raise RuntimeError("fixture_laya_error")
        h = OfflineRun(predict=fail, verify=lambda x: bound_reply(x, "clean"))
        report = self.run_case(h)
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(h.entered, 0)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

    def test_short_laya_batch_keeps_each_unreturned_item_review(self):
        rows = [source("s1", "환전 안내 하나"), source("s2", "환전 안내 둘")]
        h = OfflineRun(rows, predict=lambda ts: [prediction(.99)])
        report = self.run_case(h)
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(h.entered, 0)
        missing_text = sorted(h.laya_inputs)[1]
        missing = next(c for c in report["candidates"] if c["evidence_text"] == missing_text)
        self.assertEqual(missing["verdict"], "uncertain")
        self.assertEqual(sorted(c["verdict"] for c in report["candidates"]), ["ad", "uncertain"])
        self.assert_review(report)

    def test_truncated_laya_output_must_not_be_confident(self):
        h = OfflineRun(predict=lambda ts: [prediction(.99, truncated=True) for _ in ts])
        report = self.run_case(h)
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(h.entered, 0)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

    def test_educational_borderline_without_laya_stays_review(self):
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
        self.assertEqual(h.verify_inputs + h.extract_inputs, [])
        self.assertEqual(h.entered, 0)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assert_review(report)

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
