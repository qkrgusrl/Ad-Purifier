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
        self.assertEqual(set(call["model_input"]), {"candidate_id", "source_units",
                                                          "context_before", "context_after", "context_status"})
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

    def border_case(self, context, verdict, evidence_id="c0001"):
        row = source(text="환전 안내", html="<p>환전 안내</p>", context=context)
        row.update(selector="p", tag="p", href=None)
        initial = main.judge(main.candidates_from(
            OfflineRun([row]).page_data, detect_posted=False), "NONEXISTENT_TEST_WEIGHTS", False)
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0]["verdict"], "borderline")
        harness = OfflineRun([row], predict=lambda ts: [prediction(.55) for _ in ts],
                             verify=lambda item: self.protocol("verify", item, verdict, evidence_id))
        report = self.run_case(harness)
        self.assertEqual(harness.laya_inputs, [main.normalize(row["text"])])
        self.assertEqual(harness.extract_requests, [])
        self.assertEqual(len(harness.verify_inputs), 1)
        item = harness.verify_inputs[0]
        self.assertEqual((item["html"], item["context"]), (row["source_html"], context))
        if evidence_id.startswith("h"):
            self.assert_protocol(self.trace[0], item, verdict, evidence_id, "html")
        else:
            # 문맥 ID는 이제 복원 단계부터 거부한다. 소유 검증의 보류 결과는 유지한다.
            trace = self.trace[0]
            reply = trace["reply"]
            self.assertNotEqual(reply["status"], "ok")
            result, = reply["results"]
            self.assertEqual(result["verdict"], "uncertain")
            self.assertTrue(result["needs_review"])
            self.assertIsNone(result["evidence"])
            call = result["call"]
            self.assertEqual(call["input"], item)
            self.assertEqual(call["request_count"], 1)
            self.assertEqual(call["model_output"], {"verdict": verdict, "evidence_id": evidence_id})
            raw = call["raw_response"]["choices"][0]
            self.assertEqual(raw["finish_reason"], "stop")
            self.assertEqual(json.loads(raw["message"]["content"]), call["model_output"])
            self.assertEqual(set(call["model_input"]), {"candidate_id", "source_units",
                                                        "context_before", "context_after", "context_status"})
            chats = [r["payload"] for r in trace["requests"] if r["path"] == "/v1/chat/completions"]
            self.assertEqual(len(chats), 1)
            self.assertEqual(json.loads(chats[0]["messages"][1]["content"]), call["model_input"])
            self.assertFalse(any(u["id"] == evidence_id and u["selectable"]
                                 for u in call["model_input"]["source_units"]))
        self.assertEqual(len(report["candidates"]), 1)
        return report, report["candidates"][0]

    def test_context_ad_id_cannot_complete_other_source_as_ad(self):
        report, candidate = self.border_case("<p>카지노 바카라 가입코드</p>", "ad")
        self.assertEqual(candidate["verdict"], "uncertain",
                         "거부해야 할 context 광고 ID로 source 위치의 광고를 확정함")
        self.assertNotEqual(candidate["processing_status"], "completed")
        self.assertTrue(candidate["reason"])
        self.assertIs(report["needs_review"], True)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertTrue(report["ai"]["failures"])

    def test_child_html_ad_id_cannot_complete_parent_owner_as_ad(self):
        from html.parser import HTMLParser
        from local_llm.prompts import source_units

        class TextOwners(HTMLParser):
            # 이 한 줄 fixture의 텍스트 노드를 독립 파서로 offset/조상 경로에 결합한다.
            def __init__(self):
                super().__init__()
                self.stack, self.nodes = [], []

            def handle_starttag(self, tag, attrs):
                self.stack.append(tag)

            def handle_endtag(self, tag):
                self.stack.pop()

            def handle_data(self, data):
                self.nodes.append((self.getpos()[1], data, tuple(self.stack)))

        shapes = (("p", "카지노 바카라 가입코드", "h0003", "ad"),
                  ("span", "환전 안내", "h0004", "borderline"))
        for tag, child_text, evidence_id, child_initial in shapes:
            with self.subTest(child_tag=tag, same_text=tag == "span"):
                child_html = f"<{tag}>{child_text}</{tag}>"
                original = "<div>환전 안내" + child_html + "</div>"
                page_data = capture(original)
                rows = page_data[0][1][0][1]["source_blocks"]
                self.assertEqual(len(rows), 2)
                parent = next(row for row in rows if row["tag"] == "div")
                child = next(row for row in rows if row["tag"] == tag)
                self.assertNotEqual(parent["source_id"], child["source_id"])
                self.assertNotEqual(parent["selector"], child["selector"])
                self.assertEqual((parent["text"], parent["source_html"]), ("환전 안내", original))
                self.assertEqual((child["text"], child["source_html"]), (child_text, child_html))
                initial = main.judge(main.candidates_from(page_data, detect_posted=True),
                                     "NONEXISTENT_TEST_WEIGHTS", False)
                self.assertEqual({c["source_id"]: c["verdict"] for c in initial},
                                 {parent["source_id"]: "borderline", child["source_id"]: child_initial})
                units = source_units({"html": original, "context": parent["source_context"]})
                child_start = original.index(child_html)
                # span은 태그와 본문이 별도 unit이므로 두 번째 동일 문구의 bare span을 선택한다.
                selected_start = child_start + len(f"<{tag}>") if tag == "span" else child_start
                evidence = child_text if tag == "span" else child_html
                chosen = next(u for u in units if u["source_field"] == "html"
                              and u["start_char"] == selected_start
                              and u["end_char"] == selected_start + len(evidence))
                self.assertEqual((chosen["id"], chosen["text"], chosen["selectable"]),
                                 (evidence_id, evidence, True))
                parser = TextOwners()
                parser.feed(original)
                parser.close()
                self.assertEqual(parser.nodes, [(5, parent["text"], ("div",)),
                    (child_start + len(f"<{tag}>"), child_text, ("div", tag))])
                parent_offset, _, parent_path = parser.nodes[0]
                child_offset, _, child_path = parser.nodes[1]
                self.assertEqual((len(parent_path), len(child_path)), (1, 2))
                self.assertLess(parent_offset, chosen["start_char"])
                self.assertLessEqual(chosen["start_char"], child_offset)
                self.assertGreaterEqual(chosen["end_char"], child_offset + len(child_text))
                if tag == "span":
                    direct = next(u for u in units if u["id"] == "h0002")
                    self.assertEqual(direct["start_char"], parent_offset)
                    self.assertEqual(direct["text"], chosen["text"])
                    self.assertEqual(main.normalize(chosen["text"]), main.normalize(parent["text"]))
                    self.assertNotEqual(direct["start_char"], chosen["start_char"])

                def verify(item):
                    if item["html"] == original:
                        return self.protocol("verify", item, "ad", evidence_id)
                    # 동일 텍스트의 자식도 경계 후보다. 자기 본문 ID의 clean으로 분리한다.
                    return self.protocol("verify", item, "clean", "h0002")

                trace_start = len(self.trace)
                harness = OfflineRun(page_data=page_data,
                                     predict=lambda ts: [prediction(.55) for _ in ts], verify=verify)
                report = self.run_case(harness, detect_posted=True)
                self.trace.append({"shape": tag, "page_data": page_data,
                                   "initial_candidates": initial, "parent_source_units": units,
                                   "chosen_child_unit": chosen, "text_owner_nodes": parser.nodes})
                self.assertEqual(harness.laya_inputs, sorted({main.normalize(row["text"])
                                 for row, verdict in ((parent, "borderline"), (child, child_initial))
                                 if verdict == "borderline"}))
                self.assertEqual(harness.extract_requests, [])
                self.assertEqual(len(harness.verify_inputs), 2 if tag == "span" else 1)
                item = next(i for i in harness.verify_inputs if i["html"] == original)
                self.assertEqual(item, {"candidate_id": parent["source_id"] + ":0",
                                        "html": original, "context": parent["source_context"]})
                traces = [t for t in self.trace[trace_start:] if t.get("task") == "verify"]
                trace = next(t for t in traces if t["input"] == item)
                self.assert_protocol(trace, item, "ad", evidence_id, "html")
                binding = trace["reply"]["results"][0]
                self.assertEqual((binding["start_char"], binding["end_char"]),
                                 (chosen["start_char"], chosen["end_char"]))
                self.assertEqual(binding["evidence"], evidence)
                if tag == "span":
                    child_trace = next(t for t in traces if t["input"]["html"] == child_html)
                    self.assert_protocol(child_trace, child_trace["input"], "clean", "h0002", "html")
                candidates = {c["source_id"]: c for c in report["candidates"]}
                self.assertEqual(set(candidates), {parent["source_id"], child["source_id"]})
                candidate = candidates[parent["source_id"]]
                self.assertEqual((candidate["location"], candidate["evidence_text"], candidate["ml_prob"]),
                                 (parent["selector"], parent["text"], .55))
                self.assertEqual((candidates[child["source_id"]]["verdict"],
                                  candidates[child["source_id"]]["processing_status"]),
                                 ("clean" if tag == "span" else "ad", "completed"))
                self.assertEqual(candidate["verdict"], "uncertain",
                                 "요청 owner/hash/문구가 맞아도 하위 DOM owner의 근거로 부모를 광고 확정하면 안 됨")
                self.assertNotEqual(candidate["processing_status"], "completed")
                self.assertTrue(candidate["reason"])
                self.assertIs(report["needs_review"], True)
                self.assertEqual(report["unresolved_count"], 1)
                self.assertTrue(any(f.get("source_id") == parent["source_id"]
                                    for f in report["ai"]["failures"]))

    def test_extract_child_bare_text_cannot_recover_parent_owner(self):
        from local_llm.prompts import source_units

        text = "시립 도서관 안내"
        original = f"<div>{text}<span>{text}</span></div>"
        captured = capture(original)
        rows = captured[0][1][0][1]["source_blocks"]
        self.assertEqual(len(rows), 2)
        parent = next(row for row in rows if row["tag"] == "div")
        child = next(row for row in rows if row["tag"] == "span")
        self.assertNotEqual(parent["source_id"], child["source_id"])
        self.assertNotEqual(parent["selector"], child["selector"])
        self.assertEqual((parent["text"], child["text"]), (text, text))
        self.assertEqual(parent["source_html"], original)
        self.assertEqual(child["source_html"], f"<span>{text}</span>")
        self.assertEqual([main.adscore.score(main.normalize(r["text"]))[0] for r in rows], [0, 0])
        self.assertEqual(main.candidates_from(captured, detect_posted=True), [])

        # 실제 수집 원문은 보존하고 이번 잔여 처리의 대상 owner만 부모로 제한한다.
        page_data = copy.deepcopy(captured)
        page_data[0][1][0][1]["source_blocks"] = [copy.deepcopy(parent)]
        block = {"block_id": parent["source_id"], "html": original, "context": parent["source_context"]}
        units = source_units(block)
        chosen = next(u for u in units if u["id"] == "h0004")
        direct = next(u for u in units if u["id"] == "h0002")
        self.assertEqual((chosen["source_field"], chosen["selectable"], chosen["text"]),
                         ("html", True, text))
        self.assertEqual(direct["text"], chosen["text"])
        self.assertEqual(direct["start_char"], len("<div>"))
        self.assertEqual((chosen["start_char"], chosen["end_char"]),
                         (original.index("<span>") + len("<span>"), original.index("</span>")))
        self.assertNotEqual(direct["start_char"], chosen["start_char"])
        harness = OfflineRun(page_data=page_data, predict=lambda ts: [prediction(.55) for _ in ts],
                             extract=lambda data: self.protocol("extract", data, "ad", chosen["id"]),
                             # 잘못 회수되어 verify까지 진행되면 동일한 자식 ID 응답만 재사용한다.
                             verify=lambda item: self.protocol("verify", item, "ad", chosen["id"]))
        report = self.run_case(harness, detect_posted=True)
        self.trace.append({"captured_page_data": captured, "controlled_page_data": page_data,
                           "parent_source_units": units, "chosen_child_unit": chosen})
        self.assertEqual(harness.laya_inputs, [main.normalize(text)])
        self.assertEqual(harness.extract_requests, [{"blocks": [block]}])
        self.assert_protocol(self.trace[0], block, "ad", "h0004", "html")
        binding = self.trace[0]["reply"]["candidates"][0]
        self.assertEqual((binding["evidence"], binding["start_char"], binding["end_char"]),
                         (text, chosen["start_char"], chosen["end_char"]))
        for trace in self.trace:
            if trace.get("task") == "verify":
                self.assert_protocol(trace, trace["input"], "ad", "h0004", "html")
        self.assertIs(report["needs_review"], True)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertEqual([c["source_id"] for c in report["candidates"]], [],
                         "extract가 자식의 동일 bare text를 부모 광고 근거로 회수함: verify 보류로는 회수 오류가 해결되지 않음")
        self.assertEqual(harness.verify_inputs, [], "DOM owner 불일치는 회수 전에 보류해야 함")
        self.assertEqual(len(report["ai"]["unresolved_sources"]), 1)
        unresolved = report["ai"]["unresolved_sources"][0]
        self.assertEqual((unresolved["source_id"], unresolved["processing_status"]),
                         (parent["source_id"], "review"))
        self.assertTrue(unresolved["reason"])

    def test_whole_root_with_separate_text_attribute_cannot_complete_body_as_ad(self):
        from local_llm.prompts import source_units

        body_text, attr_text = "환전 안내", "카지노 바카라 가입코드"
        shapes = [(name, f' {name}="{attr_text}"', name)
                  for name in ("title", "alt", "aria-label")]
        shapes += [("plain", "", None),
                   ("structural", ' id="offer" class="notice" style="color: black"', None)]
        for shape, attributes, text_attr in shapes:
            with self.subTest(root_attributes=shape):
                original = f"<p{attributes}>{body_text}</p>"
                page_data = capture(original)
                rows = page_data[0][1][0][1]["source_blocks"]
                self.assertEqual(len(rows), 2 if text_attr else 1)
                body = next(row for row in rows if not row.get("attr"))
                self.assertEqual((body["source_html"], body["text"]), (original, body_text))
                expected_initial = {body["source_id"]: "borderline"}
                if text_attr:
                    attribute = next(row for row in rows if row.get("attr") == text_attr)
                    self.assertNotEqual(attribute["source_id"], body["source_id"])
                    self.assertEqual((attribute["source_html"], attribute["text"]),
                                     (original, attr_text))
                    expected_initial[attribute["source_id"]] = "ad"
                initial = main.judge(main.candidates_from(page_data, detect_posted=True),
                                     "NONEXISTENT_TEST_WEIGHTS", False)
                self.assertEqual({c["source_id"]: c["verdict"] for c in initial}, expected_initial)
                units = source_units({"html": original, "context": body["source_context"]})
                chosen = next(u for u in units if u["id"] == "h0001")
                self.assertEqual((chosen["source_field"], chosen["selectable"], chosen["text"],
                                  chosen["start_char"], chosen["end_char"]),
                                 ("html", True, original, 0, len(original)))
                if text_attr:
                    attr_start = original.index(attr_text)
                    self.assertLess(attr_start + len(attr_text), original.index(">"))
                    self.assertLess(chosen["start_char"], attr_start)
                    self.assertGreater(chosen["end_char"], attr_start + len(attr_text))

                trace_start = len(self.trace)
                harness = OfflineRun(page_data=page_data,
                                     predict=lambda ts: [prediction(.55) for _ in ts],
                                     verify=lambda item: self.protocol("verify", item, "ad", "h0001"))
                report = self.run_case(harness, detect_posted=True)
                self.trace.append({"shape": shape, "page_data": page_data,
                                   "initial_candidates": initial, "source_units": units})
                self.assertEqual(harness.laya_inputs, [main.normalize(body_text)])
                self.assertEqual(harness.extract_requests, [])
                self.assertEqual(len(harness.verify_inputs), 1)
                item = harness.verify_inputs[0]
                self.assertEqual(item, {"candidate_id": body["source_id"] + ":0",
                                        "html": original, "context": body["source_context"]})
                self.assert_protocol(self.trace[trace_start], item, "ad", "h0001", "html")
                self.assertEqual(self.trace[trace_start]["reply"]["results"][0]["evidence"], original)
                candidates = {c["source_id"]: c for c in report["candidates"]}
                self.assertEqual(set(candidates), set(expected_initial))
                candidate = candidates[body["source_id"]]
                self.assertEqual((candidate["evidence_text"], candidate["location"]),
                                 (body_text, body["selector"]))
                if text_attr:
                    attr_candidate = candidates[attribute["source_id"]]
                    self.assertEqual((attr_candidate["verdict"], attr_candidate["decided_by"],
                                      attr_candidate["processing_status"]), ("ad", "rules", "completed"))
                    self.assertEqual(candidate["verdict"], "uncertain",
                                     "별도 소유 텍스트 속성까지 포함한 전체 h0001을 본문 광고 근거로 확정함")
                    self.assertEqual(candidate["processing_status"], "review")
                    self.assertTrue(candidate["reason"])
                    self.assertIs(report["needs_review"], True)
                    self.assertEqual(report["unresolved_count"], 1)
                else:
                    self.assertEqual((candidate["verdict"], candidate["processing_status"],
                                      candidate["decided_by"]), ("ad", "completed", "gemma"))
                    self.assertIs(report["needs_review"], False)
                    self.assertEqual(report["unresolved_count"], 0)

    def test_prevention_context_with_target_html_clean_id_remains_allowed(self):
        # context는 역할 판단에 사용하고 최종 근거는 대상 HTML에 결합한다.
        report, candidate = self.border_case(
            "<p>아래 환전 안내는 도박 예방교육 자료로 인용한 문구입니다.</p>", "clean", "h0001")
        self.assertEqual((candidate["verdict"], candidate["processing_status"], candidate["decided_by"]),
                         ("clean", "completed", "gemma"))
        self.assertIs(report["needs_review"], False)
        self.assertEqual(report["unresolved_count"], 0)


if __name__ == "__main__":
    unittest.main()
