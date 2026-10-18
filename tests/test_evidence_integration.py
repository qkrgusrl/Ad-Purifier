"""실제 v15 ID 복원 → detector 통합 회귀. 모델의 HTTP raw 응답만 고정한다."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures.ai_contract import OfflineRun, main, prediction, source
from fixtures.reviewer_dom import capture
from local_llm.engine import LocalServer, source_digest
from local_llm.prototype import run_extract, run_verify


class EvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="evidence-integration-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.trace = []
        self.addCleanup(lambda: print("RAW " + self.id() + " " + json.dumps(
            self.trace, ensure_ascii=False, sort_keys=True)))

    def protocol(self, task, data, verdict, evidence_id):
        # __init__/__enter__는 서버 파일/프로세스 소유 단계이므로 실행하지 않는다.
        # infer/prompt_size/_isolated_extract/validation/복원/prototype는 모두 실제 코드다.
        server = LocalServer.__new__(LocalServer)
        server.ctx_size, server.cache_prompt = 4096, None
        requests = []

        def request(path, payload=None, **kwargs):
            requests.append({"path": path, "payload": copy.deepcopy(payload)})
            if path == "/apply-template":
                return {"prompt": json.dumps(payload["messages"], ensure_ascii=False)}
            if path == "/tokenize":
                return {"tokens": list(range(100))}
            if path != "/v1/chat/completions":
                raise AssertionError("예상하지 않은 transport: " + path)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
                {"verdict": verdict, "evidence_id": evidence_id})}}]}

        with patch.object(server, "request", side_effect=request):
            reply = (run_extract if task == "extract" else run_verify)(server, data, "v15")
        self.trace.append({"task": task, "input": copy.deepcopy(data),
                           "requests": requests, "reply": copy.deepcopy(reply)})
        return reply

    def run_case(self, harness, **kwargs):
        with harness:
            report = harness.run(self.out, **kwargs)
        self.assertEqual(json.loads((self.out / "report.json").read_text()),
                         json.loads(json.dumps(report)))
        self.trace.append({"laya_inputs": harness.laya_inputs,
                           "extract_requests": harness.extract_requests,
                           "verify_inputs": harness.verify_inputs, "report": report})
        return report

    def assert_protocol(self, trace, item, verdict, evidence_id, field):
        reply = trace["reply"]
        self.assertEqual((reply["task"], reply["version"], reply["status"]),
                         (trace["task"], "v15", "ok"))
        if trace["task"] == "extract":
            self.assertEqual(set(trace["input"]), {"blocks"})
            self.assertEqual(trace["input"]["blocks"], [item])
            self.assertEqual(reply["failures"], [])
            self.assertEqual(len(reply["candidates"]), 1)
            outer = reply["calls"][0]
            self.assertEqual(outer["validation"]["errors"], [])
            call = outer["block_calls"][0]
            binding = reply["candidates"][0]
            owner = item["block_id"]
            self.assertEqual(binding["block_id"], owner)
        else:
            self.assertEqual(trace["input"], item)
            self.assertEqual(len(reply["results"]), 1)
            binding = reply["results"][0]
            call = binding["call"]
            owner = item["candidate_id"]
            self.assertEqual(binding["candidate_id"], owner)
            self.assertIs(binding["needs_review"], False)
        self.assertEqual((call["status"], call["request_count"]), ("ok", 1))
        self.assertEqual(call["validation"], {"errors": [], "accepted": [], "rejected_count": 0})
        self.assertEqual(call["model_output"], {"verdict": verdict, "evidence_id": evidence_id})
        raw = call["raw_response"]["choices"][0]
        self.assertEqual(raw["finish_reason"], "stop")
        self.assertEqual(json.loads(raw["message"]["content"]), call["model_output"])
        self.assertEqual(set(call["input"]), {"candidate_id", "html", "context"})
        self.assertEqual(call["input"], {"candidate_id": owner, "html": item["html"],
                                          "context": item["context"]})
        self.assertEqual(set(call["model_input"]), {"candidate_id", "source_units"})
        choice = next(u for u in call["model_input"]["source_units"] if u["id"] == evidence_id)
        self.assertTrue(choice["selectable"])
        self.assertEqual(choice["source_field"], field)
        for result in (call["parsed"], binding):
            self.assertEqual(result["owner_id"], owner)
            self.assertEqual(result["source_sha256"], source_digest(item))
            self.assertEqual((result["source_field"], result["evidence_id"]), (field, evidence_id))
            self.assertEqual(result["evidence"], item[field][result["start_char"]:result["end_char"]])
            self.assertEqual(result["evidence"], choice["text"])
        chats = [r["payload"] for r in trace["requests"] if r["path"] == "/v1/chat/completions"]
        self.assertEqual(len(chats), 1)
        self.assertEqual(json.loads(chats[0]["messages"][1]["content"]), call["model_input"])

    def test_tagged_v15_html_recovery_rejoins_posted_judgment(self):
        text = "처방전 없이 수면제를 판매합니다"
        original = "<p>" + text + "</p>"
        page_data = capture(original)
        rows = page_data[0][1][0][1]["source_blocks"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["source_html"], row["text"]), (original, text))
        self.assertEqual(main.adscore.score(main.normalize(text))[0], 0)
        self.assertEqual(main.candidates_from(page_data, detect_posted=True), [])
        harness = OfflineRun(page_data=page_data,
                             predict=lambda ts: [prediction(.99) for _ in ts],
                             extract=lambda data: self.protocol("extract", data, "ad", "h0001"))
        report = self.run_case(harness, detect_posted=True)
        self.assertEqual(harness.laya_inputs, [main.normalize(text)])
        self.assertEqual(harness.extract_requests, [{"blocks": [{"block_id": row["source_id"],
            "html": original, "context": row["source_context"]}]}])
        self.assert_protocol(self.trace[0], harness.extract_requests[0]["blocks"][0], "ad", "h0001", "html")
        self.assertEqual(self.trace[0]["reply"]["candidates"][0]["evidence"], original,
                         "태그를 제거한 가짜 binding으로 계약 차이를 숨기면 안 됨")
        self.assertEqual(len(report["candidates"]), 1,
                         "실제 v15의 태그 포함 근거를 회수 거부함: POSTED 재합류 필요")
        candidate = report["candidates"][0]
        self.assertEqual((candidate["technique"], candidate["location"], candidate["evidence_text"]),
                         ("POSTED", row["selector"], text))
        self.assertEqual(candidate["source_html"], original)
        self.assertEqual(candidate["extraction"], self.trace[0]["reply"]["candidates"][0])
        self.assertEqual((candidate["verdict"], candidate["processing_status"], candidate["decided_by"]),
                         ("ad", "completed", "laya"))
        self.assertEqual(candidate["ml_prob"], .99)
        self.assertIs(report["needs_review"], False)
        self.assertEqual(report["unresolved_count"], 0)

    def border_case(self, context, verdict):
        row = source(text="환전 안내", html="<p>환전 안내</p>", context=context)
        row.update(selector="p", tag="p", href=None)
        initial = main.judge(main.candidates_from(
            OfflineRun([row]).page_data, detect_posted=False), "NONEXISTENT_TEST_WEIGHTS", False)
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0]["verdict"], "borderline")
        harness = OfflineRun([row], predict=lambda ts: [prediction(.55) for _ in ts],
                             verify=lambda item: self.protocol("verify", item, verdict, "c0001"))
        report = self.run_case(harness)
        self.assertEqual(harness.laya_inputs, [main.normalize(row["text"])])
        self.assertEqual(harness.extract_requests, [])
        self.assertEqual(len(harness.verify_inputs), 1)
        item = harness.verify_inputs[0]
        self.assertEqual((item["html"], item["context"]), (row["source_html"], context))
        self.assert_protocol(self.trace[0], item, verdict, "c0001", "context")
        self.assertEqual(len(report["candidates"]), 1)
        return report, report["candidates"][0]

    def test_context_ad_id_cannot_complete_other_source_as_ad(self):
        report, candidate = self.border_case("<p>카지노 바카라 가입코드</p>", "ad")
        self.assertEqual(candidate["verdict"], "uncertain",
                         "다른 문단 context의 유효한 광고 ID를 source 위치의 광고로 확정함")
        self.assertNotEqual(candidate["processing_status"], "completed")
        self.assertTrue(candidate["reason"])
        self.assertIs(report["needs_review"], True)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertTrue(report["ai"]["failures"])

    def test_prevention_context_clean_id_remains_allowed(self):
        report, candidate = self.border_case("<p>도박 예방교육 자료로 인용한 문구입니다.</p>", "clean")
        self.assertEqual((candidate["verdict"], candidate["processing_status"], candidate["decided_by"]),
                         ("clean", "completed", "gemma"))
        self.assertIs(report["needs_review"], False)
        self.assertEqual(report["unresolved_count"], 0)


if __name__ == "__main__":
    unittest.main()
