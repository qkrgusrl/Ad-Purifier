"""유효 Laya confidence < .8만 Gemma에 전달하는 실제 run 통합 계약."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures.ai_contract import OfflineRun, URL, main, pages, prediction, recover, source


class GemmaLowConfidenceTests(unittest.TestCase):
    def execute(self, harness, **options):
        with tempfile.TemporaryDirectory(prefix="gemma-low-") as folder:
            with harness:
                report = harness.run(Path(folder), **options)
            self.assertEqual(json.loads((Path(folder) / "report.json").read_text()),
                             json.loads(json.dumps(report)))
            return report

    def assert_no_gemma(self, h):
        self.assertEqual(h.verify_inputs, [])
        self.assertEqual(h.extract_inputs, [])
        self.assertEqual(h.entered, 0, "제외 경로에서 Gemma client도 열면 안 됨")

    def test_low_extract_preserves_existing_rule_verdict_with_about_context(self):
        for text, expected in (("카지노 바카라 가입코드", "ad"), ("환전 안내", "clean")):
            with self.subTest(text=text, expected=expected):
                row = source(text=text, signals=[])
                row["source_near_text"] = "도박 예방 안내"
                data = {"frame_url": URL, "hidden": [], "texts": [row], "head": [], "links": []}
                baseline, = main.judge(main.candidates_from(
                    [(URL, [("", data)])], detect_posted=True, recover_posted=True),
                    "NONEXISTENT_TEST_WEIGHTS", False)
                self.assertEqual((baseline["verdict"], baseline["decided_by"]), (expected, "rules"))
                self.assertTrue(any(n in ("page-context", "body-context")
                                    for n, _ in baseline["context"]))
                h = OfflineRun([row], candidate_ids=[],
                               predict=lambda ts: [prediction(.55) for _ in ts], extract=recover)
                with patch.dict(sys.modules, {"detector.main": main}):
                    report = self.execute(h, detect_posted=True)
                self.assertEqual(len(h.extract_requests), 1)
                self.assertEqual(h.verify_inputs, [])
                candidate, = report["candidates"]
                self.assertEqual((candidate["verdict"], candidate["decided_by"]),
                                 (baseline["verdict"], baseline["decided_by"]))

    def test_candidate_and_residual_probability_boundaries(self):
        for residual in (False, True):
            for p in (0, .2, .2001, .5, .7999, .8, 1):
                with self.subTest(residual=residual, p=p):
                    row = source()
                    h = OfflineRun([row], candidate_ids=[] if residual else ["s1"],
                                   predict=lambda ts, p=p: [prediction(p) for _ in ts], extract=recover)
                    report = self.execute(h)
                    low = max(p, 1 - p) < .8
                    self.assertEqual(len(h.extract_inputs), int(residual and low))
                    self.assertEqual(len(h.verify_inputs), int(low))
                    if not low:
                        self.assert_no_gemma(h)
                    if residual and p <= .2:
                        self.assertEqual(report["candidates"], [])
                    else:
                        self.assertEqual(len(report["candidates"]), 1)
                        c = report["candidates"][0]
                        self.assertEqual(c["verdict"], "ad" if low or p >= .5 else "clean")
                        self.assertEqual(c["ml_prob"], p)
                        self.assertAlmostEqual(c["laya_confidence"], max(p, 1 - p))
                        self.assertEqual(c["source_html"], row["source_html"])
                        self.assertEqual(c["location"], row["selector"])
                    self.assertFalse(report["needs_review"])

    def test_invalid_or_failed_laya_never_enters_gemma_and_keeps_owner_review(self):
        def fail(texts):
            raise RuntimeError("fixture_laya_failure")
        cases = {"missing": lambda ts: [], "missing_field": lambda ts: [{} for _ in ts],
                 "nan": lambda ts: [prediction(float("nan")) for _ in ts],
                 "negative": lambda ts: [prediction(-.01) for _ in ts],
                 "above_one": lambda ts: [prediction(1.01) for _ in ts],
                 "truncated": lambda ts: [prediction(.99, truncated=True) for _ in ts],
                 "runtime_error": fail}
        for residual in (False, True):
            for case, predict in cases.items():
                with self.subTest(residual=residual, case=case):
                    row = source()
                    h = OfflineRun([row], candidate_ids=[] if residual else ["s1"], predict=predict,
                                   extract=recover)
                    report = self.execute(h)
                    self.assert_no_gemma(h)
                    self.assertTrue(report["needs_review"])
                    self.assertEqual(report["unresolved_count"], 1)
                    if residual:
                        unresolved = report["ai"]["unresolved_sources"]
                        self.assertEqual(len(unresolved), 1)
                        self.assertEqual(unresolved[0]["source_id"], "s1")
                        self.assertEqual(unresolved[0]["source_html"], row["source_html"])
                    else:
                        c = report["candidates"][0]
                        self.assertEqual(c["verdict"], "uncertain")
                        self.assertNotEqual(c["processing_status"], "completed")
                        self.assertTrue(c["reason"])
                        self.assertEqual(c["source_html"], row["source_html"])

    def test_about_without_valid_laya_stays_review_without_gemma(self):
        row = source(text="카지노 바카라", signals=[])
        data = {"frame_url": URL, "hidden": [], "texts": [row], "links": [],
                "head": [{"selector": "head > title", "text": "도박 예방 안내", "raw": "도박 예방 안내"}],
                "source_blocks": [row]}
        h = OfflineRun(page_data=[(URL, [("", data)])], predict=lambda ts: [{} for _ in ts])
        report = self.execute(h, detect_posted=True)
        self.assertEqual(h.laya_inputs, [main.normalize(row['text'])])
        self.assert_no_gemma(h)
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")

    def test_high_confidence_ad_recovers_zero_rule_posted_and_preserves_origin(self):
        from fixtures.reviewer_dom import capture
        text = "처방전 없이 수면제를 판매합니다"
        page_data = capture('<p>' + text + '</p>')
        row = page_data[0][1][0][1]["source_blocks"][0]
        self.assertEqual(main.candidates_from(page_data, detect_posted=True), [])
        self.assertEqual(main.adscore.score(main.normalize(text))[0], 0)
        for p in (.8, 1):
            with self.subTest(p=p):
                h = OfflineRun(page_data=page_data, predict=lambda ts: [prediction(p) for _ in ts])
                report = self.execute(h, detect_posted=True)
                self.assert_no_gemma(h)
                self.assertEqual(h.laya_inputs, [main.normalize(text)], "회수 후 모델 재진입 금지")
                self.assertEqual(len(report["candidates"]), 1)
                c = report["candidates"][0]
                self.assertEqual((c["verdict"], c["decided_by"], c["ml_prob"], c["laya_confidence"]),
                                 ("ad", "laya", p, p))
                self.assertEqual((c["technique"], c["location"], c["evidence_text"]),
                                 ("POSTED", row["selector"], text))
                self.assertEqual(c["source_id"], row["source_id"])
                self.assertEqual(c["source_html"], row["source_html"])
                self.assertEqual(c["source_context"], row["source_context"])
                self.assertFalse(report["needs_review"])

    def test_high_confidence_ad_unrecoverable_keeps_source_ad_origin_and_review(self):
        row = source(signals=[])
        row.update(tag="script", source_html='<script>환전 안내</script>')
        h = OfflineRun([row], candidate_ids=[], predict=lambda ts: [prediction(.99) for _ in ts])
        recovery_main = importlib.import_module("detector.main")
        original = recovery_main.candidates_from
        def unavailable(*args, **kwargs):
            return [] if kwargs.get("recover_posted") else original(*args, **kwargs)
        with patch.object(recovery_main, "candidates_from", side_effect=unavailable) as recovery:
            report = self.execute(h, detect_posted=True)
        self.assertTrue(any(c.kwargs.get("recover_posted") for c in recovery.call_args_list),
                        "기존 candidates_from 결정론 회수를 시도해야 함")
        self.assert_no_gemma(h)
        self.assertEqual(report["candidates"], [])
        self.assertTrue(report["needs_review"])
        result = next(r for r in report["ai"]["source_results"] if r["source_id"] == "s1")
        self.assertEqual(result["verdict"], "ad")
        self.assertEqual(result["source_html"], row["source_html"])
        self.assertEqual(result["ml_prob"], .99)
        self.assertEqual(result["laya_confidence"], .99)
        self.assertEqual(report["unresolved_count"], 1)

    def test_detect_posted_false_excludes_visible_but_retains_hidden_high_ad(self):
        from fixtures.reviewer_dom import capture
        page_data = capture('<p>처방전 없이 수면제를 판매합니다</p><span style="opacity:0">환전 안내</span>')
        h = OfflineRun(page_data=page_data, predict=lambda ts: [prediction(.99) for _ in ts])
        report = self.execute(h, detect_posted=False)
        self.assert_no_gemma(h)
        self.assertFalse(any(c["technique"] == "POSTED" for c in report["candidates"]))
        self.assertTrue(any(c["technique"] == "TRANSPARENT" and c["verdict"] == "ad"
                            for c in report["candidates"]))
        self.assertEqual(report["extra_count"], 0)
        self.assertFalse(report["needs_review"])


if __name__ == "__main__":
    unittest.main()
