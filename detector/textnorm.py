"""텍스트 정규화: 닮은꼴 문자 복원(HOMOGLYPH), 자모 재조합(JAMO).

프로토타입이라 confusables는 자주 쓰이는 일부만 담았다. 정식 버전에서는 유니코드 UTS #39
confusables.txt 전체를 로드해 대체한다.
"""
import re
import unicodedata

# ---------------------------------------------------------------- HOMOGLYPH
CONFUSABLES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    # Greek
    "α": "a", "ο": "o", "ν": "v", "ρ": "p", "τ": "t", "ι": "i", "κ": "k", "ε": "e", "υ": "u",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
    "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}
LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a"})
ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")


def _script(ch):
    if "a" <= ch.lower() <= "z":
        return "LATIN"
    o = ord(ch)
    if 0x0400 <= o <= 0x04FF:
        return "CYRILLIC"
    if 0x0370 <= o <= 0x03FF:
        return "GREEK"
    return None


def homoglyph_signals(raw):
    """원문에서 닮은꼴 위장 흔적을 찾는다. 흔적 목록을 반환(없으면 빈 리스트)."""
    sig = []
    if re.search("[Ａ-Ｚａ-ｚ]", raw):  # 전각 영문자
        sig.append("fullwidth-latin")
    if any(0x1D400 <= ord(c) <= 0x1D7FF for c in raw):
        sig.append("math-alnum")
    if re.search("[Ⓐ-ⓩ\U0001F130-\U0001F189]", raw):
        sig.append("enclosed-alnum")
    for tok in re.findall(r"[^\s]+", raw):
        scripts = {_script(c) for c in tok} - {None}
        if len(scripts) > 1:
            sig.append("mixed-script:" + tok)
        elif scripts & {"CYRILLIC", "GREEK"} and any(c in CONFUSABLES for c in tok):
            sig.append("lookalike-script:" + tok)
    if ZERO_WIDTH.search(raw):
        sig.append("zero-width")
    return sig


def leet_words(text):
    """'s1ot' 처럼 영문 사이에 숫자를 끼운 토큰만 복원한 결과."""
    out = []
    for tok in re.findall(r"[a-z0-9$@]+", text.lower()):
        if re.search(r"[a-z][0-9$@]+[a-z]", tok):
            out.append(tok.translate(LEET))
    return out


# ---------------------------------------------------------------- JAMO
CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ",
        "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
JUNG_PAIR = {"ㅗㅏ": "ㅘ", "ㅗㅐ": "ㅙ", "ㅗㅣ": "ㅚ", "ㅜㅓ": "ㅝ", "ㅜㅔ": "ㅞ", "ㅜㅣ": "ㅟ", "ㅡㅣ": "ㅢ"}
JONG_PAIR = {"ㄱㅅ": "ㄳ", "ㄴㅈ": "ㄵ", "ㄴㅎ": "ㄶ", "ㄹㄱ": "ㄺ", "ㄹㅁ": "ㄻ", "ㄹㅂ": "ㄼ", "ㄹㅅ": "ㄽ",
             "ㄹㅌ": "ㄾ", "ㄹㅍ": "ㄿ", "ㄹㅎ": "ㅀ", "ㅂㅅ": "ㅄ"}


def _is_cho(c):
    return c in CHO


def _is_jung(c):
    return c in JUNG


def jamo_signal(raw):
    """분해된 음절(자음+모음 연속)이 2개 이상이면 자모 분해로 본다. ㅋㅋㅋ/ㅠㅠ 는 해당 없음."""
    s = unicodedata.normalize("NFC", raw)
    pairs = sum(1 for a, b in zip(s, s[1:]) if _is_cho(a) and _is_jung(b))
    return pairs >= 2


def compose_jamo(text):
    """호환 자모 시퀀스를 완성형 음절로 재조합한다."""
    s = unicodedata.normalize("NFC", text)
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if _is_cho(c) and i + 1 < n and _is_jung(s[i + 1]):
            cho, jung = c, s[i + 1]
            i += 2
            if i < n and _is_jung(s[i]) and (jung + s[i]) in JUNG_PAIR:
                jung = JUNG_PAIR[jung + s[i]]
                i += 1
            jong = ""
            # 받침: 다음 자음 뒤에 모음이 오지 않을 때만
            if i < n and s[i] in JONG and not (i + 1 < n and _is_jung(s[i + 1])):
                jong = s[i]
                i += 1
                if i < n and (jong + s[i]) in JONG_PAIR and not (i + 1 < n and _is_jung(s[i + 1])):
                    jong = JONG_PAIR[jong + s[i]]
                    i += 1
            code = 0xAC00 + (CHO.index(cho) * 21 + JUNG.index(jung)) * 28 + JONG.index(jong)
            out.append(chr(code))
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------- 공통
def normalize(raw):
    """판정용 정규화 텍스트: NFKC(전각/수학문자) → confusables → 자모 재조합 → 소문자."""
    t = compose_jamo(ZERO_WIDTH.sub("", raw))
    # NFKC는 호환 자모(ㄱ~ㅣ)를 조합용 자모로 바꿔 버리므로 그 범위는 제외
    t = "".join(c if 0x3131 <= ord(c) <= 0x318E else unicodedata.normalize("NFKC", c) for c in t)
    t = "".join(CONFUSABLES.get(c, c) for c in t)
    return re.sub(r"\s+", " ", t).strip().lower()


def compact(text):
    """띄어쓰기·기호로 쪼갠 우회('카 지 노', '바.카.라')를 무력화한 비교용 문자열."""
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())
