"""Gemma 넘김 선별: 짧은 문구·판단 가능한 문구는 제외하고 판단 곤란 점수가 높은 것만 Gemma 후보로 둔다."""
import unittest

from detector import adscore
from detector.ai import GEMMA_MIN_AMBIGUITY, ambiguity

LONG = "베트남 필리핀 테더 환전 상담 문의는 텔레그램으로 연락 주세요"
MID = (adscore.LOW + adscore.HIGH) / 2


class GemmaSelectionTests(unittest.TestCase):
    def test_short_text_never_goes_to_gemma(self):
        self.assertEqual(ambiguity("강남 달토", MID, .5), (None, "short"))

    def test_rule_and_laya_agree_is_decidable(self):
        self.assertEqual(ambiguity(LONG, adscore.HIGH, .95)[1], "clear")
        self.assertEqual(ambiguity(LONG, adscore.LOW, .05)[1], "clear")

    def test_borderline_and_disagreement_are_candidates(self):
        a, why = ambiguity(LONG, MID, .5)
        self.assertIsNone(why)
        self.assertGreaterEqual(a, GEMMA_MIN_AMBIGUITY)
        self.assertIsNone(ambiguity(LONG, MID, .62)[1])
        self.assertEqual(ambiguity(LONG, MID, .8)[1], "clear")
        # 규칙이 경계 끝이면 Laya가 조금만 기울어도 판단 가능
        self.assertEqual(ambiguity(LONG, adscore.HIGH, .62)[1], "clear")

    def test_more_ambiguous_ranks_higher(self):
        self.assertGreater(ambiguity(LONG, MID, .5)[0], ambiguity(LONG, MID, .4)[0])

    def test_context_conflict_splits_by_text_own_score(self):
        # Laya 광고 확신 + 교육·보도 페이지: 문구 자체 단서가 충분하면 끼어든 스팸 → Laya를 따른다
        self.assertEqual(ambiguity(LONG, MID, .95, conflict=True, text_score=6), (None, "clear"))
        # 짧은 문구('카지노' 메뉴·시설명)는 맥락대로 정상
        self.assertEqual(ambiguity("카지노 실습실", MID, .95, conflict=True, text_score=3), (None, "context"))
        # 긴 문구인데 단서가 약하면 Gemma 후보 (약관 문장 등 Laya 오탐)
        a, why = ambiguity(LONG, -3.5, .91, conflict=True, text_score=-3.5)
        self.assertIsNone(why)
        self.assertGreaterEqual(a, GEMMA_MIN_AMBIGUITY)


if __name__ == "__main__":
    unittest.main()


class BareUrlTests(unittest.TestCase):
    """URL만 있는 문구는 Laya로 보내지 않는다 (djbokji 10-22: 부고·청첩장 링크를 0.78~0.94로 광고 판정)."""
    def test_bare_url(self):
        from detector.ai import bare_url
        for text in ("https://www.wooribugo4.com/page/funeral/view.php?uri=P189", "http://pf.kakao.com/_lGzxkn",
                     "https://www.xn--299av1t05d.com/v/jr9qetn", "m.site.naver.com/1Xp75", "[창티켓.kr]"):
            with self.subTest(text=text):
                self.assertTrue(bare_url(text))
        for text in ("2022.08.16", "1.2.3", "0505-310-9093", "카지노 https://a.com", "● 매.충전 ●", ""):
            with self.subTest(text=text):
                self.assertFalse(bare_url(text))
