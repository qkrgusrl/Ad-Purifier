"""실제 후보 수집→public run의 원문 소유권 계약. extract.js 문자열 검사는 하지 않는다."""
import tempfile
import unittest

from fixtures.ai_contract import OfflineRun, bound_reply, main, pages, source


class SourceCaptureTests(unittest.TestCase):
    def test_candidate_keeps_raw_html_context_and_owner(self):
        row = source(html='<a data-note="a > b" href="/x?a=1&amp;b=2">환전\u200b  안내</a>',
                     context="앞 문장.\r\n뒤 문장.")
        candidates = main.candidates_from(pages([row]), detect_posted=False)
        self.assertEqual(len(candidates), 1)
        for key in ("source_id", "source_html", "source_context"):
            self.assertEqual(candidates[0].get(key), row[key], key + " 원문 소실")
        self.assertIsInstance(candidates[0]["source_context"], str)

    def test_one_owner_with_two_techniques_keeps_shared_source_identity(self):
        row = source(signals=[["TRANSPARENT", "opacity:0"], ["OFFSCREEN", "display:none"]])
        candidates = main.candidates_from(pages([row]), detect_posted=False)
        self.assertEqual({c["technique"] for c in candidates}, {"TRANSPARENT", "OFFSCREEN"})
        self.assertEqual({c.get("source_id") for c in candidates}, {row["source_id"]})

    def test_duplicate_text_retains_distinct_dom_owners_before_submission_dedupe(self):
        rows = [source("s1"), source("s2"), source("s3")]
        candidates = main.candidates_from(pages(rows), detect_posted=False)
        self.assertEqual(len(candidates), 3)
        self.assertEqual({c.get("source_id") for c in candidates}, {"s1", "s2", "s3"})
        self.assertEqual({c["location"] for c in candidates}, {r["selector"] for r in rows})

    def test_context_string_is_not_score_weight_list_and_reaches_bridge_verbatim(self):
        row = source(html='<a title="환전" href="/x">환전  안내</a>', context="제목\r\n원본 주변 문맥 &amp;")
        h = OfflineRun([row], verify=lambda x: bound_reply(x, "clean"))
        with tempfile.TemporaryDirectory() as out, h:
            report = h.run(out)
        self.assertEqual(len(h.verify_inputs), 1, "Gemma 미호출")
        item = h.verify_inputs[0]
        self.assertEqual(set(item), {"candidate_id", "html", "context"})
        self.assertEqual(item["html"], row["source_html"])
        self.assertEqual(item["context"], row["source_context"])
        self.assertIsInstance(item["context"], str)
        c = report["candidates"][0]
        self.assertIsInstance(c.get("context", []), list)
        self.assertEqual(c["source_context"], row["source_context"])

    def test_missing_original_html_is_reviewed_not_synthesized(self):
        row = source()
        del row["source_html"]
        h = OfflineRun([row])
        with tempfile.TemporaryDirectory() as out, h:
            report = h.run(out)
        self.assertIs(report.get("needs_review"), True)
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")
        self.assertEqual(h.verify_inputs, [], "evidence_text로 가짜 HTML을 만들어 보내면 안 됨")

    def test_iframe_prefix_preserves_selector_and_original_html(self):
        row = source()
        prefix = 'iframe[src="https://fixture.invalid/frame"] >>> '
        candidates = main.candidates_from(pages([row], prefix=prefix), detect_posted=False)
        self.assertEqual(candidates[0]["location"], prefix + row["selector"])
        self.assertEqual(candidates[0].get("source_html"), row["source_html"])


if __name__ == "__main__":
    unittest.main()
