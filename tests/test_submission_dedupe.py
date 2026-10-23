"""제출 중복 기준: 은닉 4종은 요강 집계 단위 (url + location + technique)로만 합친다."""
import unittest

from detector import main

URL = "https://example.go.kr/board/view?id=1024"


def ad(location, technique, text="카지노 바카라 가입코드", url=URL):
    return {"url": url, "location": location, "technique": technique, "evidence_text": text, "verdict": "ad"}


class SubmissionDedupeTests(unittest.TestCase):
    def test_same_hidden_text_at_different_locations_is_kept_separately(self):
        cands = [ad("div.post > span.hide", "TRANSPARENT"), ad("ul.list > li:nth-of-type(3) > span", "TRANSPARENT")]
        kept, dups = main.dedupe(cands)
        self.assertEqual(len(kept), 2, "위치가 다르면 같은 문구라도 각각 제출")
        self.assertEqual(dups, [])

    def test_same_element_same_technique_is_one_finding_even_with_different_text(self):
        cands = [ad("div.post > span.hide", "OFFSCREEN", "카지노"),
                 ad("div.post > span.hide", "OFFSCREEN", "바카라 가입코드")]
        kept, dups = main.dedupe(cands)
        self.assertEqual(len(kept), 1, "요강: 동일 요소 내 같은 기법 문자열은 1건")
        self.assertEqual(len(dups), 1)

    def test_same_element_different_hidden_techniques_are_both_kept(self):
        cands = [ad("div.post > span.hide", "TRANSPARENT"), ad("div.post > span.hide", "OFFSCREEN")]
        self.assertEqual(len(main.dedupe(cands)[0]), 2)

    def test_paging_variant_of_same_post_is_merged(self):
        cands = [ad("div.post > span.hide", "JAMO", url=URL),
                 ad("div.post > span.hide", "JAMO", url=URL + "&startPage=2")]
        self.assertEqual(len(main.dedupe(cands)[0]), 1, "목록 위치 인자만 다른 같은 글 주소는 한 번만 제출")

    def test_posted_copy_of_hidden_ad_is_dropped_and_posted_text_merge_is_unchanged(self):
        hidden = ad("div.post > span.hide", "TRANSPARENT")
        posted_title = ad("h1.subject", "POSTED")
        posted_list = ad("ul.list > li > a", "POSTED")
        kept, dups = main.dedupe([posted_list, hidden, posted_title])
        self.assertEqual(kept, [hidden], "같은 페이지 같은 문구의 게시형은 은닉 1건으로 본다(기존 규칙 유지)")
        self.assertEqual(len(dups), 2)


if __name__ == "__main__":
    unittest.main()
