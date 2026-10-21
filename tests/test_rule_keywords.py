"""Laya가 정상으로 확신해 놓치던 유형(찌라시 SEO·문서 위조·게임 핵·도박 Q&A·업소 은어)의 규칙 보강. 모델은 실행하지 않는다."""
import unittest

from detector import adscore
from detector.textnorm import normalize


def score(text):
    return adscore.score(normalize(text))[0]


class RuleKeywordTests(unittest.TestCase):
    def test_missed_spam_types_are_confirmed_by_rules(self):
        # 라벨 데이터에서 규칙+advanced가 놓친 문구 (advanced p≈0.05~0.5)
        for text in ("구글찌라시프로그램 구글찌라시광고대행",
                     "찌라시홍보샘플 웹문서찌라시홍보",
                     "구글 웹문서 상위 노출 전문 홍보",
                     "#기본증명서위조 【카아톡: hoy29 텔레상담】 수료증명서위조 통장내역위조 위조맛집",
                     "발로란트 무료 스킨 핵 【핵블리.com】 서든핵 주소",
                     "[0l0-4798-5876] 스타머니 한게임머니상 피망머니상 365일24시",
                     "롤링금 적립:",
                     "홍대입구역하퍼룸 010 5815 2262 아현동셔츠빠예약방법",
                     # hanyanglaw 게시판 도배: 사전을 피한 도박 표기 (단서 0이면 잔여 선별에서 빠지므로 규칙이 잡아야 함)
                     "1, 첫입금플러스",
                     "파워삿횰덤 (비타민게임) 웹바둑이",
                     "펀치바둑이 타워game 츄쳔인 ; 복단지 010,7364,8213"):
            with self.subTest(text=text):
                self.assertGreaterEqual(score(text), adscore.HIGH)

    def test_normal_texts_with_similar_words_stay_below_confirm(self):
        for text in ("해피머니상품권 1만원 지급",
                     "졸업증명서 위조 혐의로 검거",
                     "구글찌라시 피해 신고 및 예방 안내",
                     "롤링페이퍼 만들기 체험",
                     "카지노 실습실",  # '카지'(잘린 표기) 패턴이 '카지노'에 겹쳐 붙지 않아야 함
                     "바둑이와 철수"):
            with self.subTest(text=text):
                self.assertLess(score(text), adscore.HIGH)


if __name__ == "__main__":
    unittest.main()
