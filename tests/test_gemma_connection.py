"""Gemma 연결 RED 계약. 실제 infer/prototype/analyze, HTTP 응답만 대체.

ai.gemma_requests 계약: attempted = succeeded + transport_failed + output_failed.
succeeded는 JSON/schema/ID 복원까지 성공한 요청이다. preflight_failed는
템플릿/토큰 사전 검사 실패 수이며 attempted에 포함하지 않는다.
중첩 extract.block_calls만 실제 요청으로 세고 외부 합계는 다시 더하지 않는다.
기존 gemma_verify/extract는 논리 호출 호환 지표로 유지한다.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures.ai_contract import OfflineRun, prediction, source
from fixtures.reviewer_dom import capture
from local_llm.engine import LocalServer
from local_llm.prototype import run_extract, run_verify


class GemmaConnectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="gemma-connection-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.chats, self.replies = [], []

    def protocol(self, task, data, verdict="ad", evidence_id="h0001", *, fault=None, tokens=100):
        server = LocalServer.__new__(LocalServer)
        server.ctx_size, server.cache_prompt = 4096, None

        def request(path, payload=None, **kwargs):
            if path == "/apply-template":
                if fault == "preflight":
                    raise OSError("fixture preflight failure")
                return {"prompt": json.dumps(payload["messages"], ensure_ascii=False)}
            if path == "/tokenize":
                return {"tokens": list(range(tokens))}
            if path != "/v1/chat/completions":
                raise AssertionError("예상하지 않은 HTTP 경계: " + path)
            if kwargs.get("on_dispatch") is not None:
                kwargs["on_dispatch"]()
            self.chats.append(copy.deepcopy(payload))
            if fault == "transport":
                raise OSError("fixture transport failure")
            content = "{broken" if fault == "output" else json.dumps(
                {"verdict": verdict, "evidence_id": evidence_id})
            return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}

        with patch.object(server, "request", side_effect=request):
            reply = (run_extract if task == "extract" else run_verify)(server, data, "v15")
        self.replies.append(copy.deepcopy(reply))
        return reply

    def run_case(self, h, **kwargs):
        with h:
            report = h.run(self.out, **kwargs)
        self.assertEqual(json.loads((self.out / "report.json").read_text()),
                         json.loads(json.dumps(report)))
        return report

    def residual(self, p=.55, extract=None):
        # 기존 EvidenceIntegrationTests와 같은 원문 수집/게시형 재합류 경로.
        page_data = capture("<p>처방전 없이 수면제를 판매합니다</p>")
        return OfflineRun(page_data=page_data, predict=lambda ts: [prediction(p) for _ in ts],
                          extract=extract or (lambda d: self.protocol("extract", d)),
                          verify=lambda d: self.protocol("verify", d))

    def border(self, context="<p>오늘 도서관 운영시간 안내입니다.</p>"):
        row = source(text="환전 안내", html="<p>환전 안내</p>", context=context)
        row.update(selector="p", tag="p", href=None)
        return row

    def metrics(self, report, attempted=0, succeeded=0, transport_failed=0,
                output_failed=0, preflight_failed=0, reused=0):
        expected = dict(attempted=attempted, succeeded=succeeded,
                        transport_failed=transport_failed, output_failed=output_failed,
                        preflight_failed=preflight_failed, reused=reused)
        actual = report["ai"].get("gemma_requests")
        self.assertIsInstance(actual, dict, "실제 HTTP 요청 계측 ai.gemma_requests 필요")
        for key, value in expected.items():
            self.assertEqual(actual.get(key), value, key)
        self.assertEqual(attempted, len(self.chats))
        self.assertEqual(actual["attempted"], sum(actual[k] for k in
                         ("succeeded", "transport_failed", "output_failed")))
        self.assertIn("gemma_verify", report["ai"])
        self.assertIn("gemma_extract", report["ai"])

    def test_extract_ad_reuses_successful_inner_verify_and_preserves_owner_binding(self):
        h = self.residual()
        report = self.run_case(h, detect_posted=True)
        self.assertEqual(h.verify_inputs, [], "extract 내부 성공 검증을 재호출함")
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(h.laya_inputs, ["처방전 없이 수면제를 판매합니다"])
        c, = report["candidates"]
        self.assertEqual((c["verdict"], c["decided_by"], c["processing_status"]),
                         ("ad", "gemma", "completed"))
        original = self.replies[0]["candidates"][0]
        self.assertEqual(c["extraction"], original)
        self.assertEqual(original["owner_id"], original["block_id"])
        self.assertEqual(c["gemma"]["results"][0]["owner_id"], original["owner_id"],
                         "재사용하려고 원 owner_id를 새 candidate_id로 덮어쓰면 안 됨")
        self.metrics(report, attempted=1, succeeded=1, reused=1)

    def test_reuse_occurs_only_after_existing_laya_confidence_route(self):
        for p, verdict in ((.99, "ad"), (.01, "clean")):
            with self.subTest(p=p):
                self.chats.clear()
                h = self.residual(p)
                report = self.run_case(h, detect_posted=True)
                self.assertEqual(h.verify_inputs, [])
                if p > .5:
                    c, = report["candidates"]
                    self.assertEqual((c["verdict"], c["decided_by"]), (verdict, "laya"))
                    self.assertEqual(len(self.chats), 0)
                    self.metrics(report)
                else:
                    self.assertEqual(h.extract_requests, [])
                    self.assertEqual(report["ai"]["residual_screened_clean"], 1)
                    self.metrics(report)

    def test_corrupt_inner_proof_cannot_be_reused(self):
        mutations = {
            "owner": lambda b: b.update(owner_id="foreign-owner"),
            "hash": lambda b: b.update(source_sha256="0" * 64),
            "evidence": lambda b: b.update(evidence="다른 문구"),
            "html": lambda b: b["input"].update(html="<p>다른 원문</p>"),
            "context": lambda b: b["input"].update(context="변경된 문맥"),
            "failed": lambda b: b.update(status="error"),
        }
        for name, mutate in mutations.items():
            with self.subTest(fault=name):
                self.chats.clear()
                def extract(data):
                    reply = self.protocol("extract", data)
                    bc = reply["calls"][0]["block_calls"][0]
                    target = bc["parsed"] if name in ("owner", "hash", "evidence") else bc
                    mutate(target)
                    return reply
                h = self.residual(extract=extract)
                report = self.run_case(h, detect_posted=True)
                # 검증 재호출 또는 보류만 허용. 오염된 내부 proof의 확정 재사용 금지.
                self.assertTrue(h.verify_inputs or report["needs_review"], name)
                self.assertEqual(len(self.chats), 1 + len(h.verify_inputs))
                self.assertEqual(report["ai"]["extraction"]["candidates"][0]["owner_id"],
                                 h.extract_requests[0]["blocks"][0]["block_id"])

    def test_clean_context_only_selection_is_review_in_verify_and_extract(self):
        for task in ("verify", "extract"):
            with self.subTest(task=task):
                row = self.border()
                h = OfflineRun([row], candidate_ids=[] if task == "extract" else None,
                               verify=lambda d: self.protocol("verify", d, "clean", "c0001"),
                               extract=lambda d: self.protocol("extract", d, "clean", "c0001"))
                report = self.run_case(h, detect_posted=True)
                self.assertTrue(report["needs_review"])
                self.assertFalse(any(c["verdict"] == "clean" for c in report["candidates"]))
                self.assertFalse(any(r.get("verdict") == "clean" for r in report["ai"]["source_results"]))
                self.assertNotEqual(self.replies[-1]["status"], "ok",
                                    "서버/prototype도 context-only clean을 확정하면 안 됨")

    def test_related_education_context_with_target_html_selection_stays_clean(self):
        for task in ("verify", "extract"):
            with self.subTest(task=task):
                row = self.border()
                row["source_context"] = "<p>아래 환전 안내는 도박 예방교육에서 인용한 광고 문구입니다.</p>" + row["source_html"]
                h = OfflineRun([row], candidate_ids=[] if task == "extract" else None,
                               verify=lambda d: self.protocol("verify", d, "clean", "h0001"),
                               extract=lambda d: self.protocol("extract", d, "clean", "h0001"))
                report = self.run_case(h, detect_posted=True)
                self.assertFalse(report["needs_review"])
                result = (report["candidates"] if task == "verify" else report["ai"]["source_results"])[0]
                self.assertEqual((result["verdict"], result["decided_by"]), ("clean", "gemma"))
                inp = json.loads(self.chats[-1]["messages"][1]["content"])
                self.assertEqual(inp["context_before"], "아래 환전 안내는 도박 예방교육에서 인용한 광고 문구입니다.")
                self.assertEqual(inp["context_after"], "")
                self.assertTrue(inp["context_status"])

    def dom_child_clean_case(self, task):
        from local_llm.prompts import source_units

        child_text = "오늘 도서관 운영시간 안내입니다."
        raw = "<div>환전 안내<span>" + child_text + "</span></div>"
        page_data = capture(raw)
        rows = page_data[0][1][0][1]["source_blocks"]
        parent, = [r for r in rows if r["tag"] == "div"]
        child, = [r for r in rows if r["tag"] == "span"]
        self.assertNotEqual(parent["source_id"], child["source_id"])
        self.assertNotEqual(parent["selector"], child["selector"])
        self.assertEqual((parent["text"], parent["source_html"]), ("환전 안내", raw))
        self.assertEqual((child["text"], child["source_html"]),
                         (child_text, "<span>" + child_text + "</span>"))
        selected = next(u for u in source_units({"html": raw}) if u["id"] == "h0004")
        self.assertEqual((selected["source_field"], selected["text"], selected["selectable"]),
                         ("html", child_text, True))
        self.assertEqual(selected["start_char"], raw.index("<span>") + len("<span>"))
        child_choice = next(u for u in source_units({"html": child["source_html"]})
                            if u["id"] == "h0002")
        self.assertEqual((child_choice["text"], child_choice["selectable"]), (child_text, True))
        if task == "extract":
            # 원본 source_blocks/HTML/owner는 그대로 두고 두 소유 단위를 잔여 경로로 보낸다.
            for _, frames in page_data:
                for _, data in frames:
                    for field in ("texts", "hidden", "links", "head"):
                        data[field] = []

        def verify(item):
            # 공통 단위에는 부모 직접 문구만 있다. 자식 ID 강제 응답은 계속 거절한다.
            evidence_id = "h9999" if item["html"] == raw else "h0001"
            return self.protocol("verify", item, "clean", evidence_id)

        def extract(data):
            # 두 owner를 한 실제 run_extract 호출로 보내 중첩 검증 경로를 유지한다.
            server = LocalServer.__new__(LocalServer)
            server.ctx_size, server.cache_prompt = 4096, None
            def request(path, payload=None, **kwargs):
                if path == "/apply-template":
                    return {"prompt": json.dumps(payload["messages"], ensure_ascii=False)}
                if path == "/tokenize":
                    return {"tokens": list(range(100))}
                self.assertEqual(path, "/v1/chat/completions")
                if kwargs.get("on_dispatch") is not None:
                    kwargs["on_dispatch"]()
                self.chats.append(copy.deepcopy(payload))
                model_input = json.loads(payload["messages"][1]["content"])
                owner = model_input["candidate_id"]
                self.assertEqual(owner, 'target')
                own = ''.join(u['text'] for u in model_input['source_units'])
                choice = 'h0001' if own == child_text else 'h9999'
                return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
                    {"verdict": "clean", "evidence_id": choice})}}]}
            with patch.object(server, "request", side_effect=request):
                return run_extract(server, data, "v15")

        h = OfflineRun(page_data=page_data, predict=lambda ts: [prediction(.55) for _ in ts],
                       verify=verify, extract=extract)
        report = self.run_case(h, detect_posted=True)
        if task == "verify":
            self.assertTrue(any(i["html"] == raw for i in h.verify_inputs))
            candidates = {c["source_id"]: c for c in report["candidates"]}
            # 단순 정상 자식은 초기 규칙 후보가 아니므로 잔여 source_results에서 확인한다.
            self.assertNotIn(child["source_id"], candidates)
            child_result, = [r for r in report["ai"]["source_results"]
                             if r["source_id"] == child["source_id"]]
            self.assertEqual((child_result["verdict"], child_result["decided_by"]),
                             ("clean", "gemma"))
            self.assertEqual(child_result["binding"]["owner_id"], child["source_id"])
            self.assertEqual(child_result["binding"]["evidence"], child_text)
            parent_result = candidates[parent["source_id"]]
            self.assertEqual(parent_result["verdict"], "uncertain",
                             "부모의 html 안에 있는 자식 정상 문구로 부모를 clean 확정함")
            self.assertNotEqual(parent_result["processing_status"], "completed")
            self.assertTrue(parent_result["reason"])
        else:
            self.assertEqual({b["block_id"] for request in h.extract_requests for b in request["blocks"]},
                             {parent["source_id"], child["source_id"]})
            results = {r["source_id"]: r for r in report["ai"]["source_results"]}
            child_result = results[child["source_id"]]
            self.assertEqual((child_result["verdict"], child_result["decided_by"]), ("clean", "gemma"))
            self.assertEqual(child_result["binding"]["owner_id"], child["source_id"])
            self.assertEqual(child_result["binding"]["evidence"], child_text)
            self.assertFalse(any(r.get("source_id") == parent["source_id"] and r.get("verdict") == "clean"
                                 for r in report["ai"]["source_results"]),
                             "잔여 부모를 자식 정상 문구로 clean 확정함")
            unresolved = [r for r in report["ai"]["unresolved_sources"]
                          if r["source_id"] == parent["source_id"]]
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["processing_status"], "review")
            self.assertTrue(unresolved[0]["reason"])
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["unresolved_count"], 1)

    def test_verify_parent_cannot_use_child_clean_html_but_child_stays_clean(self):
        self.dom_child_clean_case("verify")

    def test_extract_parent_cannot_use_child_clean_html_but_child_stays_clean(self):
        self.dom_child_clean_case("extract")

    def test_ad_context_evidence_keeps_owner_rejection(self):
        row = self.border("<p>카지노 바카라 가입코드</p>")
        h = OfflineRun([row], verify=lambda d: self.protocol("verify", d, "ad", "c0001"))
        report = self.run_case(h)
        self.assertTrue(report["needs_review"])
        self.assertEqual(report["candidates"][0]["verdict"], "uncertain")

    def test_request_metrics_distinguish_success_transport_and_output_failure(self):
        for fault, expected in ((None, "succeeded"), ("transport", "transport_failed"),
                                ("output", "output_failed")):
            with self.subTest(fault=fault):
                self.chats.clear()
                h = OfflineRun([self.border()], verify=lambda d: self.protocol("verify", d, fault=fault))
                report = self.run_case(h)
                self.metrics(report, attempted=1, **{expected: 1})
                self.assertEqual(report["ai"]["gemma_verify"], 1)

    def test_nested_extract_request_failure_metrics_are_not_double_counted(self):
        for fault, expected in ((None, "succeeded"), ("transport", "transport_failed"),
                                ("output", "output_failed")):
            with self.subTest(fault=fault):
                self.chats.clear()
                h = self.residual(.55, extract=lambda d: self.protocol("extract", d, fault=fault))
                report = self.run_case(h, detect_posted=True)
                self.metrics(report, attempted=1, reused=int(fault is None), **{expected: 1})
                self.assertEqual(report["ai"]["gemma_extract"], 1)
                self.assertEqual(report["ai"]["gemma_verify"], 0)

    def test_overflow_and_preflight_failure_count_zero_chat_requests(self):
        for task in ("verify", "extract"):
            for fault in ("overflow", "preflight"):
                with self.subTest(task=task, fault=fault):
                    self.chats.clear()
                    row = self.border()
                    original = row["source_html"]
                    def respond(d):
                        return self.protocol(task, d, fault=fault,
                                             tokens=4096 if fault == "overflow" else 100)
                    h = OfflineRun([row], candidate_ids=[] if task == "extract" else None,
                                   verify=respond, extract=respond)
                    report = self.run_case(h, detect_posted=True)
                    self.assertTrue(report["needs_review"])
                    self.metrics(report, preflight_failed=int(fault == "preflight"))
                    sent = h.verify_inputs[0] if task == "verify" else h.extract_requests[0]["blocks"][0]
                    self.assertEqual(sent["html"], original)

    def test_prompt_requires_clean_evidence_from_target_html_while_reading_context(self):
        h = OfflineRun([self.border()], verify=lambda d: self.protocol("verify", d, "clean"))
        self.run_case(h)
        prompt = self.chats[0]["messages"][0]["content"]
        # 고정 해시/문장 복제 대신 의미를 담은 한 지시문을 검사한다.
        instructions = prompt.replace("\n", ".").split(".")
        self.assertTrue(any("clean" in s and "html" in s and
                            any(word in s for word in ("선택", "근거")) for s in instructions),
                        "clean 근거 ID를 대상 html에서 선택하라는 지시 필요")
        self.assertIn("context", prompt)


if __name__ == "__main__":
    unittest.main()
