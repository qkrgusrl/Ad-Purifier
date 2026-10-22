"""잔여 원문 선별: 규칙 단서가 하나도 없는 문구는 실제 실행 기본값에서 Laya·Gemma로 보내지 않는다.

jnbburi(251페이지)에서 '패밀리 사이트'·기사 제목·주소 같은 단서 0 문구를 Laya가 광고로 확신해 190건이 모두 오탐이었고,
hanyanglaw(670페이지)에서는 애매한 잔여 문구 2,414건이 Gemma 추출로 넘어갔다. 모델·네트워크는 쓰지 않는다.
"""
import tempfile
import unittest
from pathlib import Path

from fixtures.ai_contract import OfflineRun, prediction, source


class ResidualRuleClueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ad-purifier-tests-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def run_default(self, rows):
        # Laya가 무엇이든 광고로 확신하는 최악의 경우에도 단서 0 문구는 광고가 되면 안 된다.
        h = OfflineRun(rows, candidate_ids=[], predict=lambda ts: [prediction(.99) for _ in ts])
        with h:
            report = h.run(self.out, residual_needs_rule_clue=True)
        return h, report

    def test_zero_clue_residual_skips_laya_and_gemma(self):
        h, report = self.run_default([source("s1", "패밀리 사이트"), source("s2", "전남 여수시 중흥3로 15-8")])
        self.assertEqual(h.laya_inputs + h.verify_inputs + h.extract_inputs, [])
        self.assertEqual([c for c in report["candidates"] if c["verdict"] == "ad"], [])
        self.assertEqual(report["ai"]["residual_no_rule_clue"], 2)

    def test_residual_with_rule_clue_still_reaches_laya(self):
        # '환전'(2점)은 경계 미만이라 규칙 후보는 아니지만 단서가 있어 잔여 선별 대상이다.
        h, report = self.run_default([source("s1", "환전 안내"), source("s2", "패밀리 사이트")])
        self.assertEqual(len(h.laya_inputs), 1)
        self.assertEqual(report["ai"]["residual_no_rule_clue"], 1)


if __name__ == "__main__":
    unittest.main()
