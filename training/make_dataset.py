"""Laya 파인튜닝용 학습 데이터 생성 (템플릿 조합).

- 입력 텍스트는 탐지기가 정규화(닮은꼴 복원·자모 재조합)한 뒤의 형태를 가정한다.
- 테스트 사이트(testsite/server.py)와 검증셋(eval_set.jsonl) 문구는 넣지 않는다. 생성 후 8글자 이상
  겹치는 문장이 있는지 검사해 출력한다.
- 지름길 학습 방지: 영문·숫자·전화번호·도메인·@아이디·[공지]·날짜·띄어 쓴 글자·장식 기호가
  광고/정상 양쪽에 고르게 나오도록 하고, 길이 분포도 맞춘다.
- 공공기관 정상 문구 중 광고 키워드가 들어간 것(놀이터·성인·대출·상품권 할인 등)을 대량으로 넣는다.
- 어휘 단위 분할: 각 어휘 목록의 20%는 dev.jsonl에만 쓴다. 외운 키워드가 아니라 일반화 성능을 보기 위해서다.
출력: training/train.jsonl, training/dev.jsonl
"""
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

try:
    from .long_cases import LONG_POOLS, long_negative, long_positive
except ImportError:  # python training/make_dataset.py 로 직접 실행할 때
    from long_cases import LONG_POOLS, long_negative, long_positive

QUESTION = {
    "is_illegal_ad": {
        "type": "noul",
        "instructions": "이 텍스트는 도박·성인·불법대출·마약 등을 홍보하는 불법 광고인가?",
    }
}

HERE = Path(__file__).resolve().parent
N_TRAIN, N_DEV = 2000, 300
P_LONG_POS, P_LONG_NEG = 0.30, 0.40  # v3 유형(긴 글·연락처·앵커·위장 숫자) 비율 — long_cases.py
DEV_FRAC = 0.2

# ------------------------------------------------------------------ 광고 어휘
GAMBLE = ["온라인 카지노", "바카라 사이트", "슬롯 게임", "스포츠 토토", "사설토토", "메이저 놀이터", "파워볼 픽",
          "홀덤 사이트", "라이브 카지노", "미니게임 베팅", "해외 배팅 사이트", "에볼루션 카지노", "토토 먹튀 검증",
          "안전놀이터 추천", "실시간 배팅", "그래프 게임", "룰렛 게임", "블랙잭 테이블", "경마 실시간 베팅",
          "고배당 승부 예측", "해외 축구 배당", "사다리 게임", "프라그마틱 슬롯", "카지노 총판 모집",
          "토토 픽스터 무료 픽", "키노 사다리", "식보 게임", "인플레이 배팅"]
ADULT = ["성인 화상 채팅", "조건 만남", "출장 안마", "오피 정보", "유흥 업소 할인", "19금 영상 무료", "야동 사이트",
         "성인 용품 몰래 배송", "애인 대행", "출장 마사지 후불", "키스방 할인", "노출 방송 BJ", "즉석 만남 어플",
         "립카페 정보", "무료 성인 웹툰", "원나잇 상대 매칭", "섹시 영상통화"]
LOAN = ["무직자 대출", "신용불량자 대출 가능", "작업 대출", "소액결제 현금화", "당일 대출", "주부 대출 무서류",
        "개인돈 급전", "정보이용료 현금화", "카드깡 최저 수수료", "대포통장 삽니다", "내구제 대출",
        "휴대폰깡 즉시 입금", "일수 대출 당일", "저신용 비상금 대출", "상품권 현금화 95%", "연체자 가능 대출"]
DRUG = ["아이스 판매", "떨 팝니다", "작대기 구매", "필로폰 직거래", "대마 구매 문의", "케타민 판매",
        "엑스터시 구해요", "허브 던지기 판매", "사티바 대마 직구", "졸피뎀 처방 없이", "펜타닐 패치 판매"]
ETC = ["운전면허 위조 제작", "졸업증명서 위조", "개인정보 DB 판매", "비아그라 직구 정품", "불법 IPTV 무료 시청",
       "해외 스포츠 무료 중계 링크", "주민등록증 위조"]
BENEFIT = ["첫충 20%", "매충 10%", "꽁머니 3만원 지급", "가입 즉시 보너스", "무제한 환전", "롤링 100%",
           "신규 가입코드 777", "무료 스핀 50회", "당일 승인", "무서류 즉시 입금", "100% 비밀 보장", "익명 배송",
           "첫 이용 할인", "VIP 혜택", "첫 입금 보너스 30%", "24시간 상담", "먹튀 시 전액 보상", "한도 500만원",
           "후불 가능", "신분 확인 없음", "출금 5분 완료", "첫 거래 50% 할인", "전국 퀵 배송", ""]
CONTACT = ["텔레그램 @{id}", "텔레 {id}", "카톡 {id}", "오픈채팅 문의", "라인 {id}", "문의 010-{a}-{b}",
           "{id}.xyz 접속", "주소 {id}{n}.com", "상담 @{id}", "디엠 주세요", "{id}.vip 검색", "링크는 프로필에", "문의 메일 {id}@gmail.com", "{id}@naver.com 으로 문의",
           "주소창에 {id}{n}.club", "상담원 연결 1:1", ""]
IDS = ["luckyday", "bigwin", "vipclub", "jackpot", "goldline", "sunny", "kingbet", "moon", "starz", "fastcash",
       "ace", "royal", "dragon", "money", "happy", "nightowl", "blue", "tiger"]
# 영어·인도네시아어·중국어·일본어 스팸 (해킹된 공공 사이트에 흔함). 검증셋 문구는 일부러 피했다.
FOREIGN = ["best online casino {pct} welcome bonus visit {id}{n}.com", "situs judi bola terpercaya deposit pulsa",
           "agen togel resmi bonus new member {pct}", "bandar judi online terbesar", "sports betting tips sure win",
           "free porn videos hd {id}.xyz", "cheap viagra no prescription fast delivery",
           "payday loan instant approval no credit check", "线上娱乐城 注册送彩金", "真人百家乐 在线投注",
           "无抵押贷款 当天放款", "オンラインカジノ 入金不要ボーナス", "poker online uang asli",
           "crypto casino {pct} rakeback"]
POS_TEMPLATES = ["{core} {benefit} {contact}", "{core} {contact} {benefit}", "{core} 찾으세요? {benefit} {contact}",
                 "{contact} 연락주시면 {core} 바로 안내", "{core} ▶ {benefit} ◀ {contact}",
                 "지금 {core} 가입하면 {benefit}", "안전 {core}, {benefit}, 문의 {contact}", "{core} {contact}",
                 "{core}", "{benefit} {core} 여기서만"]

# ------------------------------------------------------------------ 정상 어휘
# 화면 구성 요소 — 영문·숫자·도메인·전화번호·@아이디가 정상 쪽에도 나오게 한다.
NAV = ["본문 바로가기", "주메뉴 바로가기", "새창 열림", "닫기", "이전 페이지", "다음 페이지", "검색어 입력", "검색",
       "사이트맵", "로그인", "회원가입", "전체메뉴 열기", "메뉴 닫기", "맨 위로", "목록", "첨부파일 다운로드",
       "게시물 표: 번호, 제목, 첨부, 조회수", "현재 페이지 {d}", "글자 크기 크게", "개인정보처리방침",
       "저작권 보호정책", "이메일무단수집거부", "웹접근성 품질인증 마크", "슬라이드 정지", "슬라이드 재생",
       "COPYRIGHT © 2026 {org}. 무단 전재 및 재배포 금지", "ⓒ {org}", "TEL 02-{a}-{b} | FAX 02-{a}-{b}",
       "대표전화 1588-{b}", "민원 문의 {dom}@korea.kr", "www.{dom}.go.kr", "ENGLISH", "中文", "日本語", "FAQ",
       "Q&A", "SNS 공유하기 facebook twitter", "QR코드로 모바일 홈페이지 접속", "PDF 다운로드 ({d}.{d}MB)",
       "hwp 뷰어 설치", "TOP", "ver {d}.{d}.{d}", "Last updated 2026-09-{d}{d}", "{d}/{d} 페이지",
       "조회수 1,{d}{d}{d}", "(우) {a}{d} {org} 청사로 {d}", "사업자등록번호 {d}{d}{d}-{d}{d}-{a}{d}",
       "공식 인스타그램 @{dom}_official", "공식 유튜브 채널 바로가기", "카카오톡 채널 @{org}", "RSS 구독",
       "Family Site", "관련 사이트 바로가기", "이 페이지에서 제공하는 정보에 만족하십니까?", "담당부서 {org} 총무과",
       "개인정보 보호책임자 연락처 02-{a}-{b}", "IE 11 이하 브라우저는 지원하지 않습니다", "Chrome 최신 버전 권장",
       "자료 출처: 통계청 KOSIS", "Instagram @{dom}.city", "Youtube {org} TV", "Twitter @{dom}_gov",
       "Tel. 031-{a}-{b}", "E-mail: {dom}{n}@korea.kr", "http://www.{dom}.go.kr", "Skip to content",
       "Language: KOR | ENG", "No. {n}{d}", "Page {d} of {n}", "Login", "Home > 알림마당 > 공지사항"]
# 광고 키워드가 들어 있지만 공공 문맥인 문장
HARD_NEG = [
    # 놀이터
    "어린이공원 놀이터 바닥 탄성포장 교체 공사", "놀이터 미끄럼틀 파손 신고합니다", "단지 내 놀이터 모래 소독 실시",
    "물놀이터 운영 시간 변경 안내", "무장애 통합놀이터 개장식", "놀이터 시설물 배상책임보험 가입 현황",
    "숲속 모험 놀이터 이용 수칙", "놀이터 옆 가로등 수리 요청",
    # 성인
    "성인 대상 컴퓨터 기초 교실", "만 19세 이상 성인 인플루엔자 접종", "성인병 예방 건강 강좌",
    "성인 문해 교실 졸업식 개최", "성인 발달장애인 주간활동 서비스", "성인 학습자 장학금 지급",
    # 대출 (도서관·정책금융)
    "전세자금 대출 이자 지원 사업", "청년 버팀목 전세대출 안내", "학자금 대출 상환 유예 신청",
    "중소기업 육성자금 대출 이차보전", "주택도시기금 대출 금리 인하", "도서 대출 기간 연장 방법",
    "상호대차 도서 대출 서비스", "전자책 대출 1인 5권까지", "희망도서 바로대출 신청",
    "소상공인 긴급 경영안정 대출 접수",
    # 신용·무직·급여
    "실직자 구직급여 신청 안내", "신용회복위원회 채무조정 상담", "저신용 소상공인 특례보증",
    "서민금융 통합지원센터 이전 안내", "무직 청년 구직활동 지원금", "개인회생 무료 법률 상담",
    # 당일·즉시·승인
    "여권 당일 발급 불가 안내", "건축 허가 승인 처리 기간 단축", "긴급복지 생계지원 즉시 지급",
    "인감증명 당일 발급 가능 창구", "카드 승인 오류 시 환불 절차",
    # 무료·보너스·할인·%
    "청소년 무료 수영 강습", "지역사랑상품권 구매 시 10% 추가 적립", "착한가격업소 5% 할인",
    "탄소중립 포인트 최대 5만원 지급", "신규 회원 대상 경품 이벤트", "출석 이벤트 참여하고 기념품 받아가세요",
    "공공자전거 첫 가입 30일 무료", "주민 설문조사 참여 시 모바일 커피 쿠폰 증정", "헌혈 참여자 영화 관람권 지급",
    "재난지원금 1인당 20만원 지급", "전기차 구매 보조금 최대 700만원", "관광지 입장료 50% 감면 대상",
    "공영주차장 경차 50% 할인", "평생학습 수강료 30% 감면",
    # 메신저·연락처
    "카카오톡 상담 채널로 민원 접수 가능", "민원 상담은 카카오톡 플러스친구로", "텔레비전 수신료 분리 납부 안내",
    "텔레마케팅 사기 주의", "주민 소통 오픈채팅방 개설 안내", "라인댄스 강좌 수강생 모집", "가이드라인 개정 안내",
    "담당자 휴대전화 010-{a}-{b}", "행사 문의 카톡 아이디는 주민센터에 문의", "텔레그램 사칭 메시지 주의",
    # 합법 사행·도박 관련 행정
    "체육진흥투표권 발행 대상 경기 안내", "스포츠토토 판매점 위탁 사업자 모집", "카지노 폐광지역 개발기금 배분",
    "도박 중독 치유 프로그램 참가자 모집", "경륜 경정 장외발매소 이전 공청회", "온라인 베팅 광고 차단 프로그램 보급",
    "사행산업 매출 총량 관리 결과", "복권 기금 지원 사업 공모",
    # 비슷한 말
    "포커스 그룹 인터뷰 참여자 모집", "롤링 배너 이미지 교체", "외화 환전 우대 쿠폰 배포 (관광안내소)",
    "주차 슬롯 증설 공사", "롤링페이퍼 만들기 체험", "사설 학원 교습비 공개", "사설 구급차 이용 요금 안내",
    "오피스텔 건축 허가 공람", "유흥주점 영업시간 제한 해제 안내", "청소년 유해업소 출입 단속",
    "19세 미만 관람 불가 공연 안내", "아이스링크 개장 안내", "아이스팩 재사용 캠페인", "작대기 모양 조형물 설치",
    "허브 정원 가꾸기 체험", "블랙잭 이라는 이름의 반려견을 찾습니다",
    # 마약 관련 행정
    "마약류 의약품 오남용 예방 교육", "펜타닐 패치 처방 관리 강화", "대마 성분 의약품 수입 허가 기준",
    "필로폰 투약 사범 검거 보도자료", "마약 퇴치 홍보 대사 위촉",
    # 기타 키워드
    "도로명주소 안내 시스템 바로가기", "추천 도서 목록 공개", "우수 공무원 추천 접수", "VIP 의전 매뉴얼 개정",
    "익명 신고 게시판 운영", "비밀번호 변경 안내",
]
NOTICE = [
    "민원 처리 결과를 안내해 드립니다.", "2026년 하반기 공무원 채용 공고", "구청 청사 주차장 공사 안내",
    "대설 대비 제설작업 협조 요청", "주민등록 사실조사 실시 안내", "재산세 납부 기간 안내",
    "청년 월세 지원 신청 바로가기", "어르신 무료 독감 예방접종 안내", "무료 법률 상담 신청 바로가기",
    "문화센터 체험 강좌 추천", "도서관 신규 회원 가입 이벤트", "자원봉사 참여자 보너스 마일리지 지급",
    "불법 도박 신고는 1336", "청소년 도박 예방 교육 실시", "불법 사금융 피해 신고센터 안내",
    "불법 스팸 문자 주의 안내", "마약류 중독 예방 캠페인", "보이스피싱 피해 예방 수칙",
    "2026년 상반기 지방세 체납액 일제 정리 기간 운영", "제{d}회 시민 걷기대회 참가 신청 (선착순 300명)",
    "10월 정례 조례 공포 알림", "인구 12만 명 돌파 기념식", "쓰레기 배출 요일 변경: 월·수·금",
    "공영주차장 요금 30분 500원으로 조정", "국민신문고 민원 접수 건수 전년 대비 12% 증가",
    "하수관로 정비 공사로 인한 교통 통제", "가을철 산불 예방 입산 통제 구역 지정", "주민자치회 위원 공개 모집",
    "노인 일자리 사업 참여자 추가 모집", "공동주택 관리비 절감 우수 단지 선정", "{org} 도시계획위원회 심의 결과",
    "어린이집 입소 대기 신청 방법", "반려동물 등록 자진신고 기간", "폭염 대비 무더위 쉼터 운영",
    "임신부 교통비 70만원 지원", "{org} 시민 참여단 회의록 공개", "청사 내 전기차 충전기 {d}기 추가 설치",
    "CCTV {d}{d}대 추가 설치 완료", "공공 Wi-Fi 구축 사업 2단계 착수", "KTX 역 연계 버스 노선 신설",
    "ESG 경영 실천 협약 체결", "AI 민원 챗봇 시범 운영", "LED 가로등 교체 1,{d}{d}0개", "K-POP 페스티벌 개최",
    "QR 체크인 시스템 종료", "SRT 정차역 유치 서명 운동", "COVID-19 백신 {d}차 접종 안내", "GIS 기반 지도 서비스 개편",
    "2026 {org} Smart City Forum 참가 신청", "e-Book 신착 도서 {d}{d}0권 입고", "OPEN API 데이터 개방 목록",
    "IoT 스마트 쓰레기통 시범 설치", "{org} 공식 블로그 blog.naver.com/{dom}", "행사 안내 www.{dom}.go.kr/event",
]
COMMENT = [
    "좋은 정보 감사합니다", "저도 같은 문제를 겪었어요", "빠른 처리 부탁드립니다", "언제부터 신청 가능한가요?",
    "주말에도 운영하나요", "사진 잘 봤습니다", "덕분에 해결했습니다 ㅎㅎ", "ㅋㅋㅋ 공감합니다", "문의드립니다",
    "담당자 연락처 알려주세요", "카톡 상담 채널로 문의 가능한가요?", "주차 공간이 부족해요", "가로등이 고장났어요",
    "확인 부탁드려요!!", "010-{a}-{b} 로 연락 주시면 감사하겠습니다", "카톡으로 사진 보내드려도 되나요?",
    "링크 눌러도 404 오류 나요", "ㅠㅠ 마감됐네요", "작년보다 지원금이 줄었네요", "답변 감사합니다^^",
    "신청 페이지가 모바일에서 안 열려요", "우리 동네도 해 주세요", "추천합니다 아이들이 좋아해요",
    "놀이터 공사는 언제 끝나나요?", "대출 도서 반납함 위치가 어디인가요", "무료 주차 되나요?",
]
NEG_POOLS = {"nav": NAV, "hard": HARD_NEG, "notice": NOTICE, "comment": COMMENT}
NEG_WEIGHTS = {"nav": 3, "hard": 5, "notice": 3, "comment": 2}

ORGS = ["가상군청", "새빛시청", "한울구청", "푸른도청", "가람동 주민센터", "해솔시 보건소", "미래구 도서관"]
DOMS = ["gasang", "saebit", "hanul", "pureun", "garam", "haesol", "mirae"]
# 장식·말머리는 광고/정상 양쪽에 같은 확률로 씌운다.
WRAPPERS = ["{x}"] * 8 + ["[공지] {x}", "{x} (2026.10.0{d})", "{x} - {org}", "{x} 자세히 보기", "★ {x} ★", "▶ {x}",
                          "{x} [{d}]", "Re: {x}", "【{x}】", "{x} ~!!"]
SPACED_P = 0.08  # 글자마다 띄어 쓰는 우회 흔적 (양쪽 동일)


def split_pools(rng):
    """어휘 목록마다 DEV_FRAC를 떼어 dev 전용으로 둔다."""
    pools = {"gamble": GAMBLE, "adult": ADULT, "loan": LOAN, "drug": DRUG, "etc": ETC, "foreign": FOREIGN,
             **NEG_POOLS, **LONG_POOLS}
    train, dev = {}, {}
    for name, items in pools.items():
        items = items[:]
        rng.shuffle(items)
        k = max(1, round(len(items) * DEV_FRAC))
        dev[name], train[name] = items[:k], items[k:]
    return train, dev


def fill(rng, text):
    return text.format(id=rng.choice(IDS) + (str(rng.randint(1, 999)) if rng.random() < 0.5 else rng.choice(["_kr", "vip", ""])), a=rng.randint(1000, 9999),
                       b=rng.randint(1000, 9999), n=rng.randint(10, 99), d=rng.randint(1, 9),
                       pct=f"{rng.choice([50, 100, 150, 200, 300])}%", org=rng.choice(ORGS), dom=rng.choice(DOMS))


def decorate(rng, text):
    text = fill(rng, rng.choice(WRAPPERS).replace("{x}", "\0")).replace("\0", text)
    if rng.random() < SPACED_P and len(text) <= 80:  # 긴 글을 글자마다 띄우지는 않는다
        text = " ".join(text.replace(" ", ""))
    return text


def positive(rng, pools):
    if rng.random() < P_LONG_POS:
        return long_positive(rng, pools, fill, short_positive)
    return short_positive(rng, pools)


def short_positive(rng, pools):
    if rng.random() < 0.08:
        return fill(rng, rng.choice(pools["foreign"]))
    cat = rng.choice(["gamble"] * 3 + ["adult", "loan", "drug"] + ["etc"] * (rng.random() < 0.5))
    tpl = rng.choice(POS_TEMPLATES)
    text = tpl.format(core=rng.choice(pools[cat]), benefit=rng.choice(BENEFIT), contact=rng.choice(CONTACT))
    return fill(rng, re.sub(r"\s+", " ", text).strip(" ,▶◀"))


def negative(rng, pools):
    if rng.random() < P_LONG_NEG:
        return long_negative(rng, pools, fill)
    kind = rng.choice([k for k, w in NEG_WEIGHTS.items() for _ in range(w)])
    text = fill(rng, rng.choice(pools[kind]))
    # 광고 문장(평균 약 25자)과 길이 분포를 맞추려고 일부는 두 문장을 잇는다.
    if rng.random() < 0.35:
        text += rng.choice([" ", ". ", " | ", " / "]) + fill(rng, rng.choice(pools[rng.choice(["hard", "notice"])]))
    return text


def generate(rng, pools, n):
    rows, seen = [], set()
    while len(rows) < n:
        label = len(rows) % 2 == 0
        text = decorate(rng, positive(rng, pools) if label else negative(rng, pools))
        if text in seen:
            continue
        seen.add(text)
        rows.append({"state": text, "questions": QUESTION, "expected": {"is_illegal_ad": label}})
    rng.shuffle(rows)
    return rows


# ------------------------------------------------------------------ 점검
def compact(s):
    return re.sub(r"\s+", "", s).lower()


def leak_check(rows, refs, k=8):
    """참조 문구(검증셋·모의 사이트·real_cases)와 공백 제거 기준 k글자 이상 겹치는 학습 문장. 한글이 3자 미만인 조각(URL·영단어 등)은 제외."""
    grams = {r[i:i + k] for r in map(compact, refs) for i in range(len(r) - k + 1)}
    grams = {g for g in grams if len(re.findall("[가-힣]", g)) >= 3}  # 'https://'·'@incruit' 같은 기호·영문 조각은 제외
    out = []
    for row in rows:
        c = compact(row["state"])
        hit = next((c[i:i + k] for i in range(len(c) - k + 1) if c[i:i + k] in grams), None)
        if hit:
            out.append((hit, row["state"]))
    return out


def stats(name, rows):
    sys.path.insert(0, str(HERE.parent))
    from detector import adscore
    from detector.textnorm import normalize

    pos = [r["state"] for r in rows if r["expected"]["is_illegal_ad"]]
    neg = [r["state"] for r in rows if not r["expected"]["is_illegal_ad"]]
    print(f"\n[{name}] {len(rows)}건 (광고 {len(pos)} / 정상 {len(neg)}), 평균 길이 광고 "
          f"{sum(map(len, pos)) / len(pos):.1f}자 / 정상 {sum(map(len, neg)) / len(neg):.1f}자, "
          f"100자 이상 광고 {sum(len(t) >= 100 for t in pos) / len(pos):.2f} / 정상 {sum(len(t) >= 100 for t in neg) / len(neg):.2f}")
    feats = {"영문": r"[a-zA-Z]", "숫자": r"\d", "@아이디": r"@\w{3,}", "전화번호": r"01\d-\d{3,4}-\d{4}",
             "띄어 쓴 글자": r"(\S ){5,}", "[공지]": r"\[공지\]", "날짜": r"2026"}
    print("  특징            광고    정상   (양쪽이 비슷해야 지름길이 안 생김)")
    for f, pat in feats.items():
        rp = sum(bool(re.search(pat, t)) for t in pos) / len(pos)
        rn = sum(bool(re.search(pat, t)) for t in neg) / len(neg)
        print(f"  {f:10}  {rp:6.2f}  {rn:6.2f}")
    print("  규칙 점수 구간   광고    정상   (경계 구간이 Laya가 실제로 판정하는 문장)")
    for label, texts in (("광고", pos), ("정상", neg)):
        band = Counter(adscore.verdict(adscore.score(normalize(t))[0]) for t in texts)
        print(f"    {label}: " + ", ".join(f"{b}={band[b] / len(texts):.2f}" for b in ("clean", "borderline", "ad")))


def main():
    rng = random.Random(7)
    train_pools, dev_pools = split_pools(rng)
    train = generate(rng, train_pools, N_TRAIN)
    dev = generate(rng, dev_pools, N_DEV)
    for fname, rows in (("train.jsonl", train), ("dev.jsonl", dev)):
        out = HERE / fname
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        print(f"{len(rows)} rows -> {out}")
    for name, rows in (("train", train), ("dev", dev)):
        stats(name, rows)

    eval_texts = [json.loads(l)["text"] for l in (HERE / "eval_set.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
    site = (HERE.parent / "testsite" / "server.py").read_text(encoding="utf-8")
    site_texts = re.findall(r">([^<>{}]{8,})<", site)
    dup = {r["state"] for r in train} & {r["state"] for r in dev}
    print(f"\n[중복 점검] train∩dev 동일 문장 {len(dup)}건")
    real = HERE / "real_cases.jsonl"  # 28개 URL에서 뽑은 실제 문구 — 평가 전용이라 학습 데이터와 겹치면 안 된다
    real_texts = [json.loads(l)["text"] for l in real.read_text(encoding="utf-8").splitlines() if l.strip()] if real.exists() else []
    for name, refs in (("eval_set", eval_texts), ("testsite", site_texts), ("real_cases", real_texts)):
        hits = leak_check(train + dev, refs)
        print(f"[누수 점검] {name}와 8글자 이상 겹침: {len(hits)}건")
        for gram, text in hits[:10]:
            print(f"    '{gram}' <- {text}")


if __name__ == "__main__":
    main()
