"""적격평가 닮은꼴·자모 변형 (eval/hidden_variants의 텍스트 쪽). 정상 표기는 신호가 없어야 한다."""
import unicodedata
import unittest

from detector.textnorm import homoglyph_signals, jamo_signal, normalize


class TextnormVariantTests(unittest.TestCase):
    def test_hangul_consonant_with_latin_vowel_is_homoglyph(self):
        for raw, restored in (("ㅋr지노 신규쿠폰", "카지노"), ("ㅋr톡/텔: boqi88", "카톡"), ("예약ㄹr인 BV52", "라인")):
            with self.subTest(raw=raw):
                self.assertTrue(any(s.startswith("hangul-latin:") for s in homoglyph_signals(raw)))
                self.assertIn(restored, normalize(raw))

    def test_spaced_and_conjoining_jamo(self):
        for raw in ("ㅌ ㅗ ㅌ ㅗ 사이트", "ㅋ.ㅏ.ㅈ.ㅣ.ㄴ.ㅗ", unicodedata.normalize("NFD", "바카라사이트")):
            with self.subTest(raw=raw):
                self.assertTrue(jamo_signal(raw))
        self.assertIn("토 토", normalize("ㅌ ㅗ ㅌ ㅗ 사이트"))

    def test_ordinary_text_has_no_signal(self):
        for raw in ("ㅋㅋㅋlol", "ㅠ ㅠ 슬퍼", "ㄱ. 내용 ㄴ. 내용", "ㅋㅋ ㅎㅎ 좋아요", "ㅇ ㅇ 맞아", "바카라사이트", "Wi-Fi 6 지원"):
            with self.subTest(raw=raw):
                self.assertFalse(jamo_signal(raw))
                self.assertFalse(any(s.startswith("hangul-latin:") for s in homoglyph_signals(raw)))


if __name__ == "__main__":
    unittest.main()
