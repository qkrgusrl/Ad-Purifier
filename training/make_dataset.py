"""Laya 파인튜닝용 학습 데이터 생성 (템플릿 조합).

- 입력 텍스트는 탐지기가 정규화(닮은꼴 복원·자모 재조합)한 뒤의 형태를 가정한다.
- 테스트 사이트(testsite/server.py) 문구는 일부러 넣지 않았다.
출력: training/train.jsonl
"""
import json
import random
from pathlib import Path

QUESTION = {
    "is_illegal_ad": {
        "type": "noul",
        "instructions": "이 텍스트는 도박·성인·불법대출·마약 등을 홍보하는 불법 광고인가?",
    }
}

rng = random.Random(7)

GAMBLE = ["온라인 카지노", "바카라 사이트", "슬롯 게임", "스포츠 토토", "사설토토", "메이저 놀이터", "파워볼 픽",
          "홀덤 사이트", "라이브 카지노", "미니게임 베팅", "해외 배팅 사이트", "에볼루션 카지노", "토토 먹튀 검증"]
ADULT = ["성인 화상 채팅", "조건 만남", "출장 안마", "오피 정보", "유흥 업소 할인", "19금 영상 무료", "야동 사이트",
         "성인 용품 몰래 배송"]
LOAN = ["무직자 대출", "신용불량자 대출 가능", "작업 대출", "소액결제 현금화", "당일 대출", "주부 대출 무서류",
        "개인돈 급전"]
DRUG = ["아이스 판매", "떨 팝니다", "작대기 구매", "필로폰 직거래", "대마 구매 문의"]
BENEFIT = ["첫충 20%", "매충 10%", "꽁머니 3만원 지급", "가입 즉시 보너스", "무제한 환전", "롤링 100%",
           "신규 가입코드 777", "무료 스핀 50회", "당일 승인", "무서류 즉시 입금", "100% 비밀 보장", "익명 배송",
           "첫 이용 할인", "VIP 혜택", "입금 보너스 30%", ""]
CONTACT = ["텔레그램 @{id}", "텔레 {id}", "카톡 {id}", "오픈채팅 문의", "라인 {id}", "문의 010-{a}-{b}",
           "{id}.xyz 접속", "주소 {id}{n}.com", "상담 @{id}", "디엠 주세요", ""]
IDS = ["luckyday", "bigwin", "vipclub", "jackpot", "goldline", "sunny", "kingbet", "moon", "starz", "fastcash"]

NEG = [
    # 접근성·내비게이션
    "본문 바로가기", "주메뉴 바로가기", "새창 열림", "닫기", "이전 페이지", "다음 페이지", "검색어 입력", "검색",
    "사이트맵", "로그인", "회원가입", "전체메뉴 열기", "메뉴 닫기", "맨 위로", "목록", "첨부파일 다운로드",
    "게시판 목록 - 번호, 제목, 작성자, 등록일", "현재 페이지 1", "글자 크기 크게", "대표전화", "개인정보처리방침",
    "저작권 보호정책", "이메일무단수집거부", "웹접근성 품질인증 마크", "슬라이드 정지", "슬라이드 재생",
    # 공지·민원
    "민원 처리 결과를 안내해 드립니다.", "2026년 하반기 공무원 채용 공고", "구청 청사 주차장 공사 안내",
    "대설 대비 제설작업 협조 요청", "주민등록 사실조사 실시 안내", "재산세 납부 기간 안내",
    "청년 월세 지원 신청 바로가기", "어르신 무료 독감 예방접종 안내", "성인 문해교육 수강생 모집",
    "성인 대상 수영 강습 신청", "서민금융진흥원 햇살론 대출 상담 안내", "소상공인 정책자금 대출 신청 안내",
    "무료 법률 상담 신청 바로가기", "문화센터 무료 체험 강좌 추천", "지역화폐 10% 할인 판매",
    "도서관 신규 회원 가입 이벤트", "자원봉사 참여자 보너스 마일리지 지급", "시민 참여 예산 공모 접수",
    "불법 도박 신고는 1336", "청소년 도박 예방 교육 실시", "불법 사금융 피해 신고센터 안내",
    "불법 스팸 문자 주의 안내", "마약류 중독 예방 캠페인", "온라인 불법 도박 근절 캠페인 참여",
    "사이버 도박 피해 상담 전화", "보이스피싱 피해 예방 수칙", "스포츠 토토 판매점 위치 안내(합법 복권)",
    # 일상 댓글
    "좋은 정보 감사합니다", "저도 같은 문제를 겪었어요", "빠른 처리 부탁드립니다", "언제부터 신청 가능한가요?",
    "주말에도 운영하나요", "사진 잘 봤습니다", "덕분에 해결했습니다 ㅎㅎ", "ㅋㅋㅋ 공감합니다", "문의드립니다",
    "담당자 연락처 알려주세요", "카톡 상담 채널로 문의 가능한가요?", "주차 공간이 부족해요", "가로등이 고장났어요",
]
NEG_TEMPLATES = ["{x}", "[공지] {x}", "{x} (2026.10.01)", "{x} - 가상시청", "{x} 자세히 보기"]


def positive():
    cat = rng.choice([GAMBLE] * 3 + [ADULT, LOAN, DRUG])
    parts = [rng.choice(cat), rng.choice(BENEFIT), rng.choice(CONTACT)]
    rng.shuffle(parts[1:])
    text = " ".join(p for p in parts if p)
    text = text.format(id=rng.choice(IDS) + str(rng.randint(1, 999)), a=rng.randint(1000, 9999),
                       b=rng.randint(1000, 9999), n=rng.randint(10, 99))
    if rng.random() < 0.15:  # 띄어쓰기 우회 흔적 일부 유지
        text = " ".join(text.replace(" ", ""))
    return text


def negative():
    return rng.choice(NEG_TEMPLATES).format(x=rng.choice(NEG))


def main():
    rows, seen = [], set()
    while len(rows) < 900:
        label = len(rows) % 2 == 0
        text = positive() if label else negative()
        if (text, label) in seen and not label:
            # 부정 샘플은 조합 수가 적어 중복 허용 한도를 둔다
            if rng.random() < 0.7:
                continue
        seen.add((text, label))
        rows.append({"state": text, "questions": QUESTION, "expected": {"is_illegal_ad": label}})
    rng.shuffle(rows)
    out = Path(__file__).with_name("train.jsonl")
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"{len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
