"""불법광고 성격 점수 (규칙 기반). 정규화된 텍스트를 입력으로 받는다.

점수 >= HIGH  : 불법광고로 확정
점수 <= LOW   : 정상으로 확정
그 사이       : 경계 → 로컬 분류기(Laya)로 판정
"""
import re

from .textnorm import compact, leet_words

HIGH, LOW = 4.0, 1.5

# (키워드, 가중치) — compact 문자열에서 부분일치
KO_KEYWORDS = [
    # 도박
    ("카지노", 3), ("바카라", 3), ("슬롯", 3), ("토토", 3), ("먹튀", 3), ("놀이터", 2.5), ("홀덤", 3),
    ("파워볼", 3), ("첫충", 3), ("매충", 3), ("꽁머니", 3), ("환전", 2), ("롤링", 2), ("가입코드", 3),
    ("배팅", 2.5), ("베팅", 2.5), ("입금보너스", 3), ("무료스핀", 3), ("포커", 2), ("사설", 1.5), ("도박", 1),
    # 성인
    ("야동", 3), ("조건만남", 3), ("출장안마", 3), ("오피", 2), ("유흥", 2), ("성인", 1), ("19금", 2),
    # 불법 금융
    ("작업대출", 3), ("당일대출", 3), ("무직자", 2), ("신용불량", 2), ("대출", 1), ("소액결제현금화", 3),
    # 마약
    ("필로폰", 3), ("대마초", 3), ("떨액", 3),
    # 불법 스포츠/영상 중계 (피라시) — 게시형 스팸에서 급증
    ("무료중계", 3), ("스포츠중계", 2.5), ("실시간중계", 2.5), ("고화질중계", 2), ("무료보기", 1.5),
    ("중계", 0.5),  # '생중계·국회중계' 등 합법 맥락이 있어 단독 가중치는 낮게
    # 검색 유인 (직접 링크 대신 "구글검색 OO"로 필터를 우회) — 게시형 불법광고의 전형적 신호
    ("구글검색", 2.5), ("네이버검색", 2.5), ("검색하면", 1.5), ("빠른주소", 2.5), ("우회주소", 2.5),
    ("접속주소", 1.5), ("주소검색", 2),
    # 불법 대부 (게시형)
    ("비상금대출", 3), ("비상금", 1.5), ("무담보", 1.5), ("무서류", 1.5),
    # 연락처 유도
    ("텔레그램", 2), ("텔레", 1.5), ("카톡", 1.5), ("오픈채팅", 2), ("라인아이디", 2),
    # 혜택·행동 유도 (단독으로는 약함)
    ("보너스", 1), ("무료", 0.5), ("지급", 0.5), ("추천", 0.5), ("승인", 0.5), ("바로가기", 0.3), ("주소", 0.5),
    ("티비", 1),  # OO티비 등 불법 중계/도박 사이트 브랜드 접미사
]
EN_KEYWORDS = [
    (r"casino", 3), (r"baccarat", 3), (r"slots?", 3), (r"toto", 3), (r"bet\b|\bbet", 3), (r"poker", 2),
    (r"holdem", 3), (r"jackpot", 2), (r"porn", 3), (r"xxx", 2),
]
# 공익·안내 맥락 (불법 도박 '신고' 안내 등) — 감점
NEGATIVE = [("신고", 3), ("근절", 3), ("예방", 3), ("캠페인", 2), ("중독", 2), ("처벌", 2), ("주의", 1.5),
            ("피해", 1.5), ("1336", 2), ("발견", 1.5), ("제보", 2), ("단속", 1.5), ("척결", 2),
            ("신고센터", 2), ("예방교육", 2), ("상담전화", 1.5)]
PATTERNS = [
    (re.compile(r"@[a-z0-9_]{3,}"), 2, "messenger-id"),
    (re.compile(r"01[016789][-. ]?\d{3,4}[-. ]?\d{4}"), 1.5, "phone"),
    (re.compile(r"\b[a-z0-9-]+\.(xyz|top|club|vip|bet|win|casino|io|me|cc|com|net)\b"), 1, "domain"),
    (re.compile(r"\d+\s*%"), 1, "percent"),
    (re.compile(r"\b[a-z]+\d{2,}\b"), 0.5, "code-like"),
]
# 장식 이모지로 문구를 강조하는 전형적 스팸 흔적 (✅구글검색✅ 처럼 감싸는 형태)
EMPHASIS = re.compile(r"[✅\U0001F525\U0001F4B0\U0001F4CC⭐★\U0001F3AF\U0001F4B5\U0001F4B8"
                      r"\U0001F449\U0001F447➡\U0001F6A8\U0001F531]")


def score(norm_text):
    """(점수, 근거 목록)"""
    c = compact(norm_text)
    hits, total = [], 0.0
    for kw, w in KO_KEYWORDS:
        if kw in c:
            total += w
            hits.append(kw)
    words = norm_text + " " + " ".join(leet_words(norm_text))
    for pat, w in EN_KEYWORDS:
        if re.search(pat, words):
            total += w
            hits.append(pat.split("\\")[0].rstrip("?s|("))
    for pat, w, name in PATTERNS:
        if pat.search(norm_text):
            total += w
            hits.append(name)
    if len(EMPHASIS.findall(norm_text)) >= 2:  # 장식 이모지 2개 이상 → 강조형 스팸
        total += 1.5
        hits.append("emoji-emphasis")
    for kw, w in NEGATIVE:
        if kw in c:
            total -= w
            hits.append("-" + kw)
    return round(total, 2), hits


def verdict(s):
    if s >= HIGH:
        return "ad"
    if s <= LOW:
        return "clean"
    return "borderline"
