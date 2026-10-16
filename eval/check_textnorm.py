"""textnorm 자체 검사 — 숫자 위장(digit_disguise)이 닮은꼴 글자 위장(HOMOGLYPH)으로만 잡히는지.

사용: python eval/check_textnorm.py   (실패하면 AssertionError, 통과하면 'OK')
양성: 숫자 자리에 닮은 글자를 써서 전화번호·'19금'을 위장한 표기
음성: 정상 문장에 흔한 'i7 CPU'·'Wi-Fi 6'·'COVID-19'·일반 전화번호 (deobfuscate_digits는 이런 것도 바꾸므로 분류에는 쓰지 않는다)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detector.textnorm import digit_disguise, homoglyph_signals  # noqa: E402

POSITIVE = [
    "l9금 《O6O-5OO-266O》 폰팅 전화방",
    "문의 O1O-1234-5678",
    "0lO-1234-5678 상담",
    "I9금 영상",
    "19금 채팅 O6O-700-1234",
]
NEGATIVE = [
    "i7 CPU 노트북", "Wi-Fi 6 지원", "010-1234-5678", "02-123-4567 대표전화", "COVID-19 예방접종",
    "iPhone 15 판매", "2026-10-16 10:00", "l9 자료실", "TEL 051-242-3939", "ㅇ1 공지", "I.T 교육 1:1 상담", "학교 O1 반",
]

for t in POSITIVE:
    assert digit_disguise(t), f"양성인데 못 잡음: {t}"
    assert any(s.startswith("digit-disguise:") for s in homoglyph_signals(t)), f"HOMOGLYPH 신호 없음: {t}"
for t in NEGATIVE:
    assert not digit_disguise(t), f"정상인데 잡힘: {t} → {digit_disguise(t)}"
    assert not any(s.startswith("digit-disguise:") for s in homoglyph_signals(t)), f"정상인데 HOMOGLYPH: {t}"
print(f"OK (양성 {len(POSITIVE)} / 음성 {len(NEGATIVE)})")
