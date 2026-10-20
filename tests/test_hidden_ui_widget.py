"""display:none으로 접힌 UI 부품(메뉴·탭·모바일 전용)과 숨김 광고의 구분, 불법 중계 사전 보강. 모델은 실행하지 않는다."""
import unittest

from fixtures.ai_contract import main, pages, source

from detector import adscore
from detector.textnorm import normalize


def judged(selector, text, signals):
    row = source(text=text, signals=signals)
    row["selector"] = selector
    return main.judge(main.candidates_from(pages([row]), detect_posted=False), "unused", False)[0]


DISPLAY_NONE = [["OFFSCREEN", "display:none"]]


class HiddenUiWidgetTests(unittest.TestCase):
    def test_folded_menu_with_borderline_score_is_not_sent_to_ai(self):
        for selector in ("div.m-menu > div.gnb.gnb7 > div.gnb-submenu.menu-type1",
                         "header > div.wrap > ul.util-menu.mobile-only",
                         "section.wrap > div.desc.TAB:nth-of-type(4)",
                         "div.sub-top > ul.breadcrumb"):
            with self.subTest(selector=selector):
                c = judged(selector, "카지노 교육기관", DISPLAY_NONE)
                self.assertEqual(c["verdict"], "clean", "접힌 메뉴의 경계 문구가 AI 판정으로 넘어감")
                self.assertIn("-ui-widget-hidden", c["hits"])

    def test_widget_word_inside_class_name_is_still_hidden_ad_candidate(self):
        # 모의 사이트 정답: 클래스 이름 중간에 nav·sitemap이 섞인 진짜 숨김 광고
        for selector in ("main.wrap > div.article-body > div.data-nav-hidden",
                         "main.wrap > div.article-body > div.sitemap-hidden",
                         "main.wrap > div.article-body > div.ad-slot"):
            with self.subTest(selector=selector):
                self.assertEqual(judged(selector, "카지노 교육기관", DISPLAY_NONE)["verdict"], "borderline")

    def test_rule_confirmed_ad_in_menu_stays_ad(self):
        c = judged("nav.gnb > ul > li > ul.sub", "토토사이트 가입코드 꽁머니 지급", DISPLAY_NONE)
        self.assertEqual(c["verdict"], "ad")

    def test_other_hiding_signal_keeps_borderline(self):
        c = judged("div.gnb-submenu", "카지노 교육기관",
                   [["OFFSCREEN", "display:none"], ["OFFSCREEN", "clip/1px(sr-only)"]])
        self.assertEqual(c["verdict"], "borderline")

    def test_illegal_streaming_names_are_rule_confirmed(self):
        for text in ("벳모아티비", "구구티비"):
            with self.subTest(text=text):
                self.assertGreaterEqual(adscore.score(normalize(text))[0], adscore.HIGH)
        self.assertIn("gugutv", adscore.score(normalize("gugutvon1.com"))[1])


if __name__ == "__main__":
    unittest.main()
