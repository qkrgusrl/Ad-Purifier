"""단일 소유 요소의 br/meta 근거 계약. 합성 원문, 오프라인 더블만 사용."""
import tempfile
import unittest
from pathlib import Path

from fixtures.ai_contract import OfflineRun, bound_reply, source
from detector.ai import digest, matching_recovery_text, valid_binding


def bound(raw, text, tag="p", *, evidence=None, owner="owner", **changes):
    item = {"candidate_id": "owner", "html": raw, "context": "합성 문맥"}
    evidence = raw if evidence is None else evidence
    start = raw.index(evidence)
    binding = {"owner_id": owner, "source_sha256": digest(item),
               "evidence_id": "h0001", "source_field": "html",
               "start_char": start, "end_char": start + len(evidence),
               "evidence": evidence}
    binding.update(changes)
    row = {"source_html": raw, "text": text, "tag": tag}
    return item, binding, row


def accepted(case):
    item, binding, row = case
    # 소유권/해시는 matching helper의 책임으로 오인하지 않는다.
    return valid_binding(binding, item, item["candidate_id"]) and matching_recovery_text(binding, row)


class GemmaOwnerVoidTests(unittest.TestCase):
    def test_br_and_self_closing_br_add_no_text(self):
        for br in ("<br>", "<br/>", "<br />"):
            with self.subTest(br=br):
                raw = "<p>합성" + br + "안내</p>"
                self.assertTrue(accepted(bound(raw, "합성안내")))
                self.assertFalse(accepted(bound(raw, "합성 안내")))

    def test_consecutive_br_and_entities_match_whole_html_own_text(self):
        raw = "<p>합성&amp;안내<br><br/>다음&nbsp;문구</p>"
        self.assertTrue(accepted(bound(raw, "합성&안내다음 문구")))
        self.assertFalse(accepted(bound(raw, "합성&안내")))

    def test_br_attributes_outside_root_children_and_multiple_roots_reject(self):
        for raw in ('<p>합성<br title="설명">안내</p>',
                    '<p>합성<br data-x="1"/>안내</p>',
                    '<p>합성안내</p><br>', '<p>합성<span>안내</span></p>',
                    '<p>합성</p><p>안내</p>'):
            with self.subTest(raw=raw):
                self.assertFalse(accepted(bound(raw, "합성안내")))

    def test_single_meta_uses_unique_content_with_whole_html_only(self):
        for raw in ('<meta property="og:description" content="합성 &amp; 안내">',
                    '<meta content="합성 &amp; 안내" property="og:description"/>'):
            with self.subTest(raw=raw):
                self.assertTrue(accepted(bound(raw, "합성 & 안내", "meta")))
                self.assertFalse(accepted(bound(raw, "다른 문구", "meta")))

    def test_meta_duplicate_empty_partial_and_other_attribute_reject(self):
        cases = [
            ('<meta content="합성안내" content="합성안내">', "합성안내", None),
            ('<meta content="합성안내" CONTENT="다른문구">', "합성안내", None),
            ('<meta content="합성안내" title="다른 광고">', "합성안내", None),
            ('<meta content="합성안내" alt="다른 광고">', "합성안내", None),
            ('<meta content="합성안내" aria-label="다른 광고">', "합성안내", None),
            ('<meta content="">', "", None),
            ('<meta content="   ">', "", None),
            ('<meta content="합성안내">', "합성안내", "합성안내"),
            ('<meta content="합성안내">', "합성", "합성"),
            ('<meta property="합성안내" content="다른문구">', "합성안내", None),
            ('<meta property="합성안내" content="다른문구">', "합성안내", "합성안내"),
            ('<meta content="합성안내"><meta content="합성안내">', "합성안내", None),
        ]
        for raw, text, evidence in cases:
            with self.subTest(raw=raw, evidence=evidence):
                self.assertFalse(accepted(bound(raw, text, "meta", evidence=evidence)))

    def test_binding_gate_rejects_foreign_owner_hash_and_context(self):
        for raw, tag in (("<p>합성<br>안내</p>", "p"),
                         ('<meta content="합성안내">', "meta")):
            for changes in ({"owner": "foreign"}, {"source_sha256": "0" * 64},
                            {"source_field": "context"}):
                with self.subTest(tag=tag, changes=changes):
                    self.assertFalse(accepted(bound(raw, "합성안내", tag, **changes)))

    def test_gemma_clean_whole_html_reaches_completed_for_br_and_meta(self):
        for raw, text, tag in (("<p>환전<br>안내</p>", "환전안내", "p"),
                               ('<meta property="og:description" content="환전안내">',
                                "환전안내", "meta")):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as folder:
                row = source(text=text, html=raw)
                row.update(tag=tag, selector=tag, href=None)

                def verify(item):
                    reply = bound_reply(item, verdict="clean")
                    _, binding, _ = bound(item["html"], text, tag)
                    binding.update(owner_id=item["candidate_id"], source_sha256=digest(item))
                    reply["results"][0].update(binding)
                    return reply

                with OfflineRun([row], verify=verify) as harness:
                    report = harness.run(Path(folder))
                result, = report["candidates"]
                self.assertEqual(len(harness.verify_inputs), 1)
                self.assertEqual((result["verdict"], result["decided_by"], result["processing_status"]),
                                 ("clean", "gemma", "completed"))

    def test_gemma_clean_foreign_meta_binding_stays_review(self):
        raw = '<meta property="og:description" content="환전안내">'
        row = source(text="환전안내", html=raw)
        row.update(tag="meta", selector="meta", href=None)

        def verify(item):
            reply = bound_reply(item, verdict="clean")
            _, binding, _ = bound(raw, "환전안내", "meta", owner="foreign")
            binding["source_sha256"] = digest(item)
            reply["results"][0].update(binding)
            return reply

        with tempfile.TemporaryDirectory() as folder:
            with OfflineRun([row], verify=verify) as harness:
                report = harness.run(Path(folder))
        result, = report["candidates"]
        self.assertEqual(len(harness.verify_inputs), 1)
        self.assertTrue(report["needs_review"])
        self.assertNotEqual(result["verdict"], "clean")

    def test_extract_preserves_inner_context_clean_error_owner_and_request_metrics(self):
        import json
        from unittest.mock import patch
        from fixtures.reviewer_dom import capture
        from local_llm.engine import LocalServer
        from local_llm.prototype import run_extract

        replies, completions = [], []

        def extract(data):
            server = LocalServer.__new__(LocalServer)
            server.ctx_size, server.cache_prompt = 4096, None

            def request(path, payload=None, **kwargs):
                if path == "/apply-template":
                    return {"prompt": json.dumps(payload["messages"], ensure_ascii=False)}
                if path == "/tokenize":
                    return {"tokens": list(range(100))}
                self.assertEqual(path, "/v1/chat/completions")
                completions.append(payload)
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps({"verdict": "clean", "evidence_id": "c0001"})}}]}

            with patch.object(server, "request", side_effect=request):
                reply = run_extract(server, data, "v15")
            replies.append(reply)
            return reply

        # 실제 수집 대신 기존 합성 DOM fixture; 실제 infer/ID 검증/adapter는 실행.
        page_data = capture("<p>처방전 없이 수면제를 판매합니다</p>")
        for _, frames in page_data:
            for _, frame in frames:
                for row in frame["source_blocks"]:
                    row["source_context"] = "<p>합성 예방교육에서 인용한 문구입니다.</p>"
        with tempfile.TemporaryDirectory() as folder:
            with OfflineRun(page_data=page_data, extract=extract) as harness:
                report = harness.run(Path(folder), detect_posted=True)
        self.assertEqual(len(completions), 1)
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        inner, = reply["calls"][0]["block_calls"]
        owner = harness.extract_requests[0]["blocks"][0]["block_id"]
        self.assertEqual(inner["input"]["candidate_id"], owner)
        self.assertIn("clean_requires_target_html", inner["error"])
        metrics = report["ai"]["gemma_requests"]
        self.assertEqual({k: metrics[k] for k in
                          ("attempted", "succeeded", "output_failed", "transport_failed")},
                         {"attempted": 1, "succeeded": 0, "output_failed": 1, "transport_failed": 0})
        failures = [f for f in reply["failures"] if owner in f["block_ids"]]
        self.assertTrue(failures, "실패한 block ID가 outer failures에 보존되어야 함")
        self.assertTrue(any("clean_requires_target_html" in str(f["error"]) for f in failures),
                        "inner 오류를 빈 []로 잃으면 안 됨")
