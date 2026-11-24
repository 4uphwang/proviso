"""질문 글자 정리(조사 보고서 2순위). 보이지 않는 문자, 전각 영숫자, 모양이 같은 다른 문자로 규칙을 피하는 공격을 막는다.

- 서식 문자(유니코드 범주 Cf: 제로폭 공백, 방향 제어, 태그 문자 U+E0000~E007F 등)와 변형 선택자를 지운다
- 전각 영숫자·기호(U+FF01~FF5E)와 전각 공백을 반각으로 바꾼다
- 라틴 글자와 모양이 같은 키릴·그리스 글자를 라틴 글자로 바꾼다(정상 질문에는 키릴·그리스 글자가 없다)
- 바뀌는 것이 없을 때까지 되풀이한다(한 번만 지우면 남는 조합이 있다, AWS 권고)

전체 NFKC 정규화는 쓰지 않는다. 법령 용어의 'ㆍ'(U+318D)를 다른 글자로 바꿔 정상 질문 9개가 달라졌다.
"""

from __future__ import annotations

import unicodedata

_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o", "ν": "v",
})


def _once(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if unicodedata.category(ch) == "Cf" or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
            continue
        if 0xFF01 <= cp <= 0xFF5E:
            ch = chr(cp - 0xFEE0)
        elif cp == 0x3000:
            ch = " "
        out.append(ch)
    return "".join(out).translate(_CONFUSABLE)


def normalize(text: str, limit: int = 5) -> str:
    for _ in range(limit):
        new = _once(text)
        if new == text:
            return new
        text = new
    return text
