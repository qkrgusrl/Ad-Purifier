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
    # 한글 자음 뒤 모음 자리에 닮은 영문을 쓴 표기: 'ㅋr지노'(카지노), 'ㅅl트'(시트). 'ㅋㅋㅋlol'처럼 뒤가 영문이면 제외
    sig.extend("hangul-latin:" + m.group(0) for m in HANGUL_LATIN.finditer(raw))
    # 숫자 자리에 닮은 글자('O'→0, 'l'→1)를 써서 전화번호·'19금'을 위장한 것도 닮은꼴 글자 위장이다 (요강: 숫자 또는 닮은 문자를 섞음)
    sig.extend("digit-disguise:" + d for d in dict.fromkeys(digit_disguise(raw)))
    return sig


HANGUL_LATIN = re.compile(r"[ㄱ-ㅎ][rlI|](?=[가-힣ㄱ-ㅣ]|\s|$)")
HANGUL_LATIN_VOWEL = {"r": "ㅏ", "l": "ㅣ", "I": "ㅣ", "|": "ㅣ"}
DIGIT_LOOKALIKE = {"o": "0", "ㅇ": "0", "〇": "0", "l": "1", "i": "1", "|": "1"}
_DIGIT_SEP = "-. "


def _digit_near(s, i, step):
    """s[i]에서 step 방향으로 구분자(- . 공백)를 건너뛴 첫 글자가 숫자인지."""
    j = i + step
    while 0 <= j < len(s) and s[j] in _DIGIT_SEP:
        j += step
    return 0 <= j < len(s) and s[j].isdigit()


def deobfuscate_digits(text):
    """숫자 사이에 끼운 닮은 글자를 숫자로 복원한다 ('l9금'→'19금', 'o6o-5oo'→'060-500', 'ㅇiㅇ-5309'→'010-5309').

    소문자로 정규화된 텍스트 기준. 이웃(구분자 건너뜀)이 숫자인 글자만 바꾸고, 바뀐 글자가 다시 이웃을 숫자로 만들 수 있어
    더 바뀌지 않을 때까지 반복한다. 연락처를 필터에 안 걸리게 적는 스팸 표기를 잡기 위한 것으로, 판정용 문자열에만 쓴다.
    """
    s = list(text)
    changed = True
    while changed:
        changed = False
        for i, ch in enumerate(s):
            if ch in DIGIT_LOOKALIKE and (_digit_near(s, i, -1) or _digit_near(s, i, 1)):
                s[i] = DIGIT_LOOKALIKE[ch]
                changed = True
    return "".join(s)


KO_DIGIT = {"공": "0", "영": "0", "일": "1", "이": "2", "삼": "3", "사": "4", "오": "5", "육": "6", "칠": "7", "팔": "8", "구": "9"}
_KO_PHONE_SPAN = re.compile(r"[0-9공영일이삼사오육칠팔구\-. ]{9,}")
_PHONE_LIKE = re.compile(r"0(?:1[016789]|60|50\d)[-._=~·/ㅡ\[\]() ]{0,3}\d{3,4}[-._=~·/ㅡ\[\]() ]{0,3}\d{4}")


def korean_digit_phone(text):
    """한글 숫자를 섞어 쓴 전화번호를 복원한다 ('공60- 오0이 -1오일5' → '060-502-1515' 꼴).

    '15일', '9월 이후'처럼 한글 숫자 글자는 일반 문장에도 많으므로, 숫자·구분자와 섞인 덩어리(실제 숫자 3개 이상)를
    바꿨을 때 전화번호 모양이 될 때만 그 덩어리를 바꾼다. 판정용 문자열에만 쓴다.
    """
    def fix(m):
        span = m.group(0)
        if sum(c.isdigit() for c in span) < 3 or not any(c in KO_DIGIT for c in span):
            return span
        conv = "".join(KO_DIGIT.get(c, c) for c in span)
        return conv if _PHONE_LIKE.search(re.sub(r"\s", "", conv)) else span
    return _KO_PHONE_SPAN.sub(fix, text)


def digit_disguise(raw):
    """숫자 자리에 닮은 글자를 써서 전화번호·'19금'을 위장한 표기 목록 (닮은꼴 글자 위장, HOMOGLYPH 근거).

    'l9금', 'O6O-5OO-266O', '0lO-1234-5678'처럼 복원했을 때 전화번호나 '19금'이 되고 원문과 다른 것만 센다.
    deobfuscate_digits는 판정 점수용이라 'i7 CPU'·'Wi-Fi 6'도 바꾸므로, 기법 분류에는 이 결과가 전화번호 꼴일 때만 쓴다.
    """
    low = raw.lower()
    restored = deobfuscate_digits(low)
    if restored == low:
        return []
    found = []
    for pat in (_PHONE_LIKE, re.compile(r"(?<!\d)19금")):
        for m in pat.finditer(restored):
            if low[m.start():m.end()] != m.group(0):
                found.append(low[m.start():m.end()].strip())
    return found


_SEP = r"[\s.,·]{0,3}"  # 숫자 한 자리마다 끼우는 구분자
_DASH = _SEP + r"[-~_ㅡ]" + _SEP  # 번호 덩어리 사이 구분 기호 — 이것이 있어야 날짜·버전 번호('0.1.0 2026.10.15')와 구별된다
_SPACED_PHONE = re.compile(r"(?<!\d)0" + _SEP + r"(?:1" + _SEP + r"[016789]|6" + _SEP + r"0)" + _DASH
                           + r"\d(?:" + _SEP + r"\d){2,3}" + _DASH + r"\d(?:" + _SEP + r"\d){3}(?!\d)")


def spaced_phone(text):
    """숫자 사이마다 점·쉼표·공백을 끼워 쓴 휴대폰·유료 번호를 붙인다 ('0.1 0, - 8.2.5.6 - 9.5.5.7' → '010-8256-9557').

    01X·060으로 시작하고 세 덩어리가 '-' 같은 기호로 나뉜 것만 바꾼다(날짜·버전 번호와 헷갈리지 않게). 판정용 문자열에만 쓴다.
    전북 인재개발원 자유게시판의 자격증 광고 11건이 이렇게 번호를 숨겼다.
    """
    def fix(m):
        d = re.sub(r"\D", "", m.group(0))
        return f"{d[:3]}-{d[3:-4]}-{d[-4:]}" if len(d) in (10, 11) else m.group(0)
    return _SPACED_PHONE.sub(fix, text)


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


JAMO_SEP = " .·-_"  # 'ㅌ ㅗ ㅌ ㅗ', 'ㅋ.ㅏ.ㅈ.ㅣ'처럼 자모 사이에 한 글자씩 끼운 우회
JAMO_PAIR = re.compile(f"[{CHO}][{re.escape(JAMO_SEP)}]?[{JUNG}]")
# 조합용 자모(U+1100~): 화면에는 완성형처럼 보이지만 음절이 자모로 쪼개진 문자열 (NFD 분해)
CONJOINING_PAIR = re.compile("[\u1100-\u1112][\u1161-\u1175]")


def jamo_signal(raw):
    """분해된 음절(자음+모음, 사이에 구분자 한 글자 허용)이 2개 이상이면 자모 분해로 본다. ㅋㅋㅋ/ㅠㅠ 는 해당 없음."""
    if len(CONJOINING_PAIR.findall(raw)) >= 2:
        return True
    return len(JAMO_PAIR.findall(unicodedata.normalize("NFC", raw))) >= 2


def compose_jamo(text):
    """호환 자모 시퀀스를 완성형 음절로 재조합한다."""
    s = unicodedata.normalize("NFC", text)
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        sep = 1 if _is_cho(c) and i + 2 < n and s[i + 1] in JAMO_SEP and _is_jung(s[i + 2]) else 0
        if _is_cho(c) and i + 1 + sep < n and _is_jung(s[i + 1 + sep]):
            cho, jung = c, s[i + 1 + sep]
            i += 2 + sep
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
    t = HANGUL_LATIN.sub(lambda m: m.group(0)[0] + HANGUL_LATIN_VOWEL[m.group(0)[1]], ZERO_WIDTH.sub("", raw))
    t = compose_jamo(t)
    # NFKC는 호환 자모(ㄱ~ㅣ)를 조합용 자모로 바꿔 버리므로 그 범위는 제외
    t = "".join(c if 0x3131 <= ord(c) <= 0x318E else unicodedata.normalize("NFKC", c) for c in t)
    t = "".join(CONFUSABLES.get(c, c) for c in t)
    t = t.replace("。", ".")  # 전각 마침표('｡'은 NFKC에서 '。'가 됨) — 도메인 위장 '찌라시｡𝐂𝐎𝐌'
    return re.sub(r"\s+", " ", t).strip().lower()


def compact(text):
    """띄어쓰기·기호로 쪼갠 우회('카 지 노', '바.카.라')를 무력화한 비교용 문자열."""
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())
