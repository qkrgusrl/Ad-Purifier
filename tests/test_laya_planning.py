"""Laya 입력 분할 계획 계약. 모델 추론 없이 가짜 tokenizer만 사용한다."""
import importlib
import math
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = {"q": "질문"}


def fake_laya_modules(hooks=(), counter=None):
    """토큰 수 = 정규화 조각 길이의 절반 올림. room = max_len - 질문 길이."""
    def encode_text(tok, text, add_special_tokens=False):
        count = counter(text) if counter else math.ceil(len(text) / 2)
        return {"input_ids": [0] * count}

    common = types.ModuleType("laya.common")
    common.encode_text = encode_text
    common.serialize_state = lambda text: text
    common.state_room = lambda tok, question, max_len, head_max_len: max_len - len(question)
    hooks_module = types.ModuleType("laya.hooks")
    hooks_module.default_hooks = lambda: list(hooks)
    return {"laya": types.ModuleType("laya"), "laya.common": common, "laya.hooks": hooks_module}


def fake_agent(max_len=40):
    agent = Mock()
    agent.tok = types.SimpleNamespace(mask_token="[M]")
    agent.cfg = {"max_len": max_len, "head_max_len": 8}
    agent.hooks = []
    agent._to_internal = lambda question: question
    agent.predict_batch.side_effect = lambda texts, questions, batch_size: [{"text": t} for t in texts]
    return agent


def planning_worker_target(conn, model_path):
    """실제 spawn 자식에서 _model_worker를 가짜 Agent로 실행한다."""
    agent_module = types.ModuleType("laya.agent")
    agent_module.Agent = lambda path, device: fake_agent()
    sys.modules.update(fake_laya_modules())
    sys.modules["laya.agent"] = agent_module
    importlib.import_module("detector.laya_runtime")._model_worker(conn, model_path)


class LayaPlanningTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(ROOT), str(ROOT / "tests"), *sys.path]))
        self.runtime = importlib.import_module("detector.laya_runtime")
        self.textnorm = importlib.import_module("detector.textnorm")

    def plan(self, texts, max_chars=1024, agent=None, modules=None):
        with patch.dict(sys.modules, modules or fake_laya_modules()):
            return self.runtime._plan_texts(agent or fake_agent(), texts, QUESTIONS, max_chars)

    def assert_valid(self, text, plan, max_chars, modules=None):
        self.assertNotIn("error", plan)
        position = 0
        for segment in plan["segments"]:
            self.assertEqual(segment["start_char"], position)
            self.assertGreater(segment["end_char"], position)
            self.assertLessEqual(segment["end_char"] - position, max_chars)
            normalized = self.textnorm.normalize(text[position:segment["end_char"]])
            counted = (modules or fake_laya_modules())["laya.common"].encode_text(
                None, normalized)["input_ids"]
            self.assertEqual(segment["input_tokens"], len(counted))
            self.assertLessEqual(segment["input_tokens"], plan["token_limit"])
            position = segment["end_char"]
        self.assertEqual(position, len(text))

    def test_short_input_is_one_segment(self):
        plan, = self.plan(["짧은 문구"])
        self.assertEqual(plan["segments"], [{"start_char": 0, "end_char": 5, "input_tokens": 3}])
        self.assertEqual(plan["token_limit"], 38)

    def test_long_korean_and_english_cover_every_char_within_both_limits(self):
        korean = "불법 도박 사이트 바로가기 " * 40
        english = "Visit this casino site now and win. " * 30
        for max_chars in (1024, 50):
            plans = self.plan([korean, english], max_chars=max_chars)
            for text, plan in zip((korean, english), plans):
                with self.subTest(max_chars=max_chars, size=len(text)):
                    self.assertGreater(len(plan["segments"]), 1)
                    self.assertEqual(plan["input_tokens"],
                                     math.ceil(len(self.textnorm.normalize(text)) / 2))
                    self.assert_valid(text, plan, max_chars)

    def test_prefers_sentence_then_space_boundary_when_available(self):
        text = "First sentence is here. Second sentence follows right now. Third one ends"
        plan, = self.plan([text], max_chars=40)
        ends = [segment["end_char"] for segment in plan["segments"][:-1]]
        self.assertTrue(all(text[end - 1] in ". " for end in ends), ends)
        self.assertEqual(text[:ends[0]], "First sentence is here.")
        self.assert_valid(text, plan, 40)

    def test_non_monotonic_tokenizer_segments_are_reverified(self):
        def counter(text):  # 길이 7의 배수만 비정상적으로 큼
            return 99 if text and len(text) % 7 == 0 else math.ceil(len(text) / 2)
        modules = fake_laya_modules(counter=counter)
        text = "abcdefghij" * 20
        plan, = self.plan([text], max_chars=30, modules=modules)
        self.assert_valid(text, plan, 30, modules)

    def test_zero_budget_hooks_and_untokenizable_return_errors_not_segments(self):
        zero = fake_agent(max_len=2)  # room = 0
        cases = {"zero": (zero, fake_laya_modules()),
                 "hooks": (fake_agent(), fake_laya_modules(hooks=["hook"])),
                 "tokenizer": (fake_agent(), fake_laya_modules(counter=Mock(side_effect=OSError("x"))))}
        for name, (agent, modules) in cases.items():
            with self.subTest(case=name):
                plan, = self.plan(["내용"], agent=agent, modules=modules)
                self.assertEqual(plan["segments"], [])
                self.assertTrue(plan["error"])

    def test_single_char_that_cannot_fit_is_an_error(self):
        modules = fake_laya_modules(counter=lambda text: 99 if "가" in text else len(text))
        plan, = self.plan(["가나다" * 30], max_chars=10, modules=modules)
        self.assertEqual(plan["segments"], [])
        self.assertIn("error", plan)

    def test_worker_plan_op_runs_no_model_and_predict_output_is_unchanged(self):
        agent = fake_agent()
        agent_module = types.ModuleType("laya.agent")
        agent_module.Agent = Mock(return_value=agent)
        conn = Mock()
        requests = [
            {"op": "plan", "texts": ["문구" * 60], "questions": QUESTIONS, "max_chars": 50},
            {"op": "predict", "texts": ["x"], "questions": QUESTIONS, "batch_size": 3},
            EOFError(),
        ]
        sent = []
        with patch.dict(sys.modules, {**fake_laya_modules(), "laya.agent": agent_module}), \
                patch.object(self.runtime, "_receive_message", side_effect=requests), \
                patch.object(self.runtime, "_send_message", side_effect=lambda c, m: sent.append(m)):
            with self.assertRaises(EOFError):
                self.runtime._model_worker(conn, "unused")
        self.assertEqual([m["status"] for m in sent], ["ready", "ok", "ok"])
        self.assertGreater(len(sent[1]["plans"][0]["segments"]), 1)
        agent.predict_batch.assert_called_once_with(["x"], QUESTIONS, batch_size=3)
        self.assertEqual(sent[2]["outputs"], [{"text": "x"}])

    def test_plan_batch_over_real_ipc_validates_arguments_and_response(self):
        text = "한글 문장입니다. " * 30
        with self.runtime.LayaRunner("unused", target=planning_worker_target,
                                     deadline=time.perf_counter() + 30) as runner:
            for bad in (0, -1, True, 1.5):
                with self.subTest(max_chars=bad), self.assertRaises(ValueError):
                    runner.plan_batch([text], QUESTIONS, max_chars=bad)
            plans = runner.plan_batch([text, "짧음"], QUESTIONS, max_chars=64)
            self.assertEqual(runner.predict_batch(["x"], QUESTIONS, batch_size=1), [{"text": "x"}])
        self.assert_valid(text, plans[0], 64)
        self.assertEqual(len(plans[1]["segments"]), 1)
        self.assertGreater(len(plans[0]["segments"]), 1)


if __name__ == "__main__":
    unittest.main()
