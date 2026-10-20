"""windows-installer-20261018: v15 이식 계약, 실제 모델/네트워크 없음.

기존 engine.LocalServer와 prototype의 응답 envelope를 보존하는
local_llm.client.GemmaClient.verify(data)/extract(data)를 검증한다.
서버 생성/시작/HTTP만 대체하고 실제 infer, ID 복원, 청크 처리는 실행한다.
규칙/Laya 경계 후보 선별 정책은 이 테스트의 변경 대상이 아니다.
"""
import asyncio
import copy
import hashlib
import importlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
# 참조 이력: 2026-10-20 사용자 승인 Gemma 연결 계약 변경.
# 원본 v15 verify SHA: 18bac24cb7d59ca7d9eca88fcdd729ad5f66c579933f635ecc73bf3a61508547
# 원본 참조: 이전 커밋의 local_llm/prompts.py
# 변경은 clean 지시 한 문장: context를 함께 읽되 evidence_id는 대상 html에서 선택.
# 아래는 해당 변경 후 UTF-8 프롬프트의 고정 reference SHA이며 실행 시 산출하지 않는다.
V15_SHA = {
    "verify": "d85ae6b92880d65bae1da47fc464b23dc1ba009f6994d810cf47afa9716326f6",
    "extract": "16105e207ac8e74bfc1398d64e8b05ba979ac6876672c351b8799edbe40e9872",
}


def required_module(name):
    path = ROOT / (name.replace(".", "/") + ".py")
    assert path.is_file(), f"Gemma 런타임 계약 미구현: {path.relative_to(ROOT)} 필요"
    importlib.invalidate_caches()
    return importlib.import_module(name)


class GemmaProtocolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), *sys.path]))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("실제 모델 실행 금지")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("네트워크 금지")))
        self.enterContext(patch.object(socket.socket, "bind", side_effect=AssertionError("서버 실행 금지")))
        if importlib.util.find_spec("psutil") is None:
            # 참조 engine의 import 의존성만 대체. 프로세스/모델 관측은 호출하면 실패.
            module = types.ModuleType("psutil")
            module.Process = Mock(side_effect=AssertionError("실제 프로세스 접근 금지"))
            self.enterContext(patch.dict(sys.modules, {"psutil": module}))

    def runtime(self):
        return required_module("local_llm.engine"), required_module("local_llm.prompts")

    def fake_transport(self, content=None, finish="stop", tokens=100):
        engine, prompts = self.runtime()
        state = {"created": 0, "entered": 0, "exited": 0, "requests": [], "servers": []}

        def init(server, *args, **kwargs):
            state["created"] += 1
            state["servers"].append(server)
            server.ctx_size = kwargs.get("ctx_size", 4096)
            server.cache_prompt = None

        def enter(server):
            state["entered"] += 1
            return server

        def leave(server, *args):
            state["exited"] += 1

        def request(server, path, payload=None, **kwargs):
            state["requests"].append((path, copy.deepcopy(payload)))
            if path == "/apply-template":
                return {"prompt": json.dumps(payload["messages"], ensure_ascii=False)}
            if path == "/tokenize":
                return {"tokens": list(range(tokens))}
            if path == "/health":
                return {"status": "ok"}
            self.assertEqual(path, "/v1/chat/completions")
            value = content(payload) if callable(content) else content
            if isinstance(value, BaseException):
                raise value
            if value is None:
                data = json.loads(payload["messages"][1]["content"])
                unit = next(u for u in data["source_units"] if u["selectable"])
                value = {"verdict": "ad", "evidence_id": unit["id"]}
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return {"choices": [{"finish_reason": finish, "message": {"content": raw}}]}

        cls = engine.LocalServer
        for attr, replacement in (("__init__", init), ("__enter__", enter), ("__exit__", leave), ("request", request)):
            self.enterContext(patch.object(cls, attr, replacement))
        return required_module("local_llm.client").GemmaClient, state, prompts

    @staticmethod
    def data(owner="c1"):
        return {"candidate_id": owner, "html": "<p>😀 주문하기.</p><p>😀 주문하기.</p>", "context": ""}

    def test_v15_prompts_are_byte_identical_to_selected_reference(self):
        prompts = required_module("local_llm.prompts")
        self.assertEqual(prompts.DEFAULT_VERSION, "v15")
        for task, expected in V15_SHA.items():
            with self.subTest(task=task):
                self.assertEqual(hashlib.sha256(prompts.PROMPTS["v15"][task].encode()).hexdigest(), expected)

    def test_verify_restores_second_duplicate_and_excludes_labels(self):
        def choose_second(payload):
            self.assertEqual(hashlib.sha256(payload["messages"][0]["content"].encode()).hexdigest(), V15_SHA["verify"])
            source = json.loads(payload["messages"][1]["content"])
            self.assertEqual(set(source), {"candidate_id", "source_units",
                                           "context_before", "context_after", "context_status"})
            self.assertNotIn("LABEL_MUST_NOT_LEAK", json.dumps(source))
            return {"verdict": "ad", "evidence_id": "h0002"}

        Client, state, _ = self.fake_transport(choose_second)
        data = {**self.data(), "gold": "clean", "annotation_reason": "LABEL_MUST_NOT_LEAK"}
        with Client() as client:
            result = client.verify(data)
        row = result["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(row["owner_id"], "c1")
        self.assertEqual(row["evidence_id"], "h0002")
        self.assertEqual(row["source_field"], "html")
        self.assertEqual(row["evidence"], "<p>😀 주문하기.</p>")
        self.assertEqual(row["end_char"], len(data["html"]))
        self.assertEqual(row["start_char"], len("<p>😀 주문하기.</p>"))
        self.assertEqual(data[row["source_field"]][row["start_char"]:row["end_char"]], row["evidence"])
        expected = hashlib.sha256(json.dumps({k: data[k] for k in ("html", "context")},
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(row["source_sha256"], expected)
        self.assertEqual(state["exited"], 1)

    def test_context_only_clean_is_rejected_even_with_related_education_context(self):
        Client, state, _ = self.fake_transport({"verdict": "clean", "evidence_id": "c0001"})
        data = {"candidate_id": "quote-owner", "html": "<p>환전 안내</p>",
                "context": "<p>위 환전 안내는 도박 예방교육에서 인용한 광고 문구입니다.</p>"}
        with Client() as client:
            result = client.verify(data)
        self.assertEqual(result["status"], "needs_review")
        row, = result["results"]
        self.assertEqual(row["verdict"], "uncertain")
        self.assertTrue(row["needs_review"])
        self.assertIsNone(row["evidence"])
        self.assertEqual(sum(path == "/v1/chat/completions" for path, _ in state["requests"]), 1)

    def test_invalid_id_malformed_json_and_truncated_generation_fail_closed(self):
        cases = [("forged-id", {"verdict": "clean", "evidence_id": "h9999"}, "stop"),
                 ("json", '{"verdict":', "stop"),
                 ("length", {"verdict": "clean", "evidence_id": "h0001"}, "length"),
                 ("extra-field", {"verdict": "ad", "evidence_id": "h0001", "evidence": "조작된 원문"}, "stop")]
        for name, content, finish in cases:
            with self.subTest(case=name):
                # 매번 등록된 patch는 addCleanup으로 되돌리며 실제 프로세스는 사용하지 않는다.
                Client, _, _ = self.fake_transport(content, finish)
                with Client() as client:
                    result = client.verify(self.data())
                self.assertEqual(result["status"], "needs_review")
                row = result["results"][0]
                self.assertEqual(row["verdict"], "uncertain")
                self.assertTrue(row["needs_review"])
                self.assertIsNone(row["evidence"])

    def test_overflow_and_empty_input_never_generate_or_confirm_clean(self):
        for raw, tokens in (("", 100), ("<p>자르지 않을 원문.</p>" * 500, 4096)):
            with self.subTest(empty=not raw):
                Client, state, _ = self.fake_transport(tokens=tokens)
                with Client() as client:
                    result = client.verify({"candidate_id": "c1", "html": raw, "context": ""})
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual(result["results"][0]["verdict"], "uncertain")
                self.assertFalse(any(p == "/v1/chat/completions" for p, _ in state["requests"]))

    def test_extract_keeps_identical_text_bound_to_each_owner_and_reuses_server(self):
        Client, state, _ = self.fake_transport({"verdict": "ad", "evidence_id": "h0002"})
        blocks = [{"block_id": owner, "html": self.data()["html"], "context": ""} for owner in ("b1", "b2")]
        with Client() as client:
            client.verify(self.data())
            result = client.extract({"blocks": blocks})
            client.verify(self.data("c2"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual({x["block_id"] for x in result["candidates"]}, {"b1", "b2"})
        for candidate in result["candidates"]:
            self.assertEqual(candidate["owner_id"], candidate["block_id"])
            self.assertEqual(candidate["evidence_id"], "h0002")
            self.assertEqual(candidate["start_char"], len("<p>😀 주문하기.</p>"))
        self.assertEqual((state["created"], state["entered"], state["exited"]), (1, 1, 1))

    def test_extract_rejects_duplicate_owners_before_any_generation(self):
        Client, state, _ = self.fake_transport()
        block = {"block_id": "same", "html": "<p>원문</p>", "context": ""}
        with Client() as client:
            with self.assertRaises(ValueError):
                client.extract({"blocks": [block, dict(block)]})
        self.assertFalse(any(p == "/v1/chat/completions" for p, _ in state["requests"]))

    def test_binding_rejects_foreign_owner_and_stale_source(self):
        engine, _ = self.runtime()
        prototype = required_module("local_llm.prototype")
        data = self.data("b1")
        bound = engine.resolve_sentence_choice(data, {"verdict": "ad", "evidence_id": "h0002"}, "v15")
        for owner, html in (("b2", data["html"]), ("b1", data["html"] + "변조")):
            with self.subTest(owner=owner, stale=html != data["html"]):
                with self.assertRaises(ValueError):
                    prototype.bind_candidates([{"block_id": owner, "html": html, "context": ""}],
                                              [{"block_id": owner, "evidence": bound["evidence"]}], {owner: bound})

    def test_extract_overflow_is_review_with_no_silent_loss_or_model_request(self):
        Client, state, _ = self.fake_transport(tokens=4096)
        with Client() as client:
            result = client.extract({"blocks": [{"block_id": "large", "html": "한글" * 5000, "context": ""}]})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any("large" in failure["block_ids"] for failure in result["failures"]))
        self.assertFalse(any(p == "/v1/chat/completions" for p, _ in state["requests"]))

    def test_cancellation_during_request_exits_owned_server(self):
        Client, state, _ = self.fake_transport(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            with Client() as client:
                client.verify(self.data())
        self.assertEqual((state["created"], state["entered"], state["exited"]), (1, 1, 1))

    def test_real_server_exit_terminates_owned_process_and_removes_session_key(self):
        engine, _ = self.runtime()
        with tempfile.TemporaryDirectory(prefix="세션 키 ") as folder:
            server = object.__new__(engine.LocalServer)
            server.stop_event = Mock()
            server.monitor = Mock()
            server.proc = Mock()
            server.proc.poll.return_value = None
            server.proc.wait.side_effect = [subprocess.TimeoutExpired("fixture-server", 10), 0]
            server.log = Mock()
            server.key_file = Path(folder) / ".session-key"
            server.key_file.write_text("fixture-key-not-a-secret", encoding="utf-8")
            engine.LocalServer.__exit__(server, asyncio.CancelledError, None, None)
            server.proc.terminate.assert_called_once_with()
            server.proc.kill.assert_called_once_with()
            self.assertEqual(server.proc.wait.call_count, 2)
            server.stop_event.set.assert_called_once_with()
            server.log.close.assert_called_once_with()
            self.assertFalse(server.key_file.exists())


if __name__ == "__main__":
    unittest.main()
