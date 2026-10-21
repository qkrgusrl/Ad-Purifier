"""Laya가 정상으로 확신해 놓치던 유형(찌라시 SEO·문서 위조·게임 핵·도박 Q&A·업소 은어)의 규칙 보강과 페이지 맥락 감점. 모델은 실행하지 않는다."""
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
                     "펀치바둑이 타워game 츄쳔인 ; 복단지 010,7364,8213",
                     # counselpsy·hanyanglaw 게시판 도배 (10-22): 업소 정보 사이트·테더 환전·현금화·가품·홀덤 변형
                     "보령 op 나라 ◆ 오피나비.com ◆ 평점",
                     "마닐라테더환전 | 상품권코인교환",
                     "컨텐츠이용료현금화 소액결제티켓",
                     "슈가통신 선불유심내구제 급전문의",
                     "@SV공장워치 @레플리카지존급 @이미테이션신발",
                     "#현찰횰덤 badugi횰덤 솔로몬흘덤제작"):
            with self.subTest(text=text):
                self.assertGreaterEqual(score(text), adscore.HIGH)

    def test_normal_texts_with_similar_words_stay_below_confirm(self):
        for text in ("해피머니상품권 1만원 지급",
                     "졸업증명서 위조 혐의로 검거",
                     "구글찌라시 피해 신고 및 예방 안내",
                     "롤링페이퍼 만들기 체험",
                     "카지노 실습실",  # '카지'(잘린 표기) 패턴이 '카지노'에 겹쳐 붙지 않아야 함
                     "바둑이와 철수",
                     "보름달토끼 만들기",
                     "제주대학교 주대 앞 버스",
                     "모빌리언스 휴대폰 결제 안내",
                     "이미테이션 게임 상영",
                     "강남 노래방"):
            with self.subTest(text=text):
                self.assertLess(score(text), adscore.HIGH)


if __name__ == "__main__":
    unittest.main()


class PageContextTests(unittest.TestCase):
    """경고·보도 글 제목은 페이지 맥락 감점, 감점 단어를 섞은 스팸 글 제목은 감점하지 않는다 (counselpsy·hanyanglaw 10-22)."""
    def ctx(self, title):
        return adscore.page_context(normalize(title))

    def test_spam_titles_are_not_warning_context(self):
        for title in ("산타먹튀신고 【HONGBO8288】 이태원역보도방",
                      "신용카드 현금화 처벌 010_5772-5293 휴대폰 소액 결제 현금화 루트 디시",
                      "광주 거제op → 오피나비.com ← 장점 룸살롱 처음"):
            with self.subTest(title=title):
                self.assertEqual(self.ctx(title), [])

    def test_warning_and_news_titles_keep_context(self):
        for title in ("카지노 바카라 불법 사이트 적발", "불법 도박 예방교육 안내", "가짜 비아그라 적발 주의보",
                      "먹튀 피해 신고 접수", "음식점 먹튀 신고 방법 안내"):
            with self.subTest(title=title):
                self.assertTrue(self.ctx(title))
