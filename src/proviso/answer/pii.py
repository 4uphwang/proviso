"""질문 속 개인정보를 모델과 기록에 넘기기 전에 가린다(11장, 계획서 §5.6).

주민등록번호(외국인등록번호), 카드번호, 전화번호, 계좌번호, 이메일을 정규식으로 찾아 [주민등록번호] 같은 자리표시로 바꾼다.
원문은 어디에도 저장하지 않는다. 법령 질문에는 이 값들이 필요 없어서 가려도 답은 같아야 한다.
날짜(2024-01-01), 금액(500만원), 조문 번호는 가리지 않는다.
"""

from __future__ import annotations

import re

_D = r"(?<![\d-])"   # 앞뒤에 숫자가 더 붙은 긴 수는 다른 값이다
_E = r"(?![\d-])"
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("주민등록번호", re.compile(_D + r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-\s?[1-8]\d{6}" + _E)),
    ("카드번호", re.compile(_D + r"\d{4}(?:[-\s]\d{4}){3}" + _E + r"|" + _D + r"\d{16}" + _E)),
    ("전화번호", re.compile(_D + r"(?:01[016789]|0[2-6]\d?|070)[-\s.]?\d{3,4}[-\s.]?\d{4}" + _E)),
    # 계좌번호: 숫자 묶음 3~4개를 '-'로 이은 10~14자리(날짜 2024-01-01은 8자리라 빠진다)
    ("계좌번호", re.compile(_D + r"\d{2,6}(?:-\d{2,7}){1,3}" + _E)),
    ("이메일", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]


def _account(m: re.Match) -> bool:
    return 10 <= sum(c.isdigit() for c in m.group()) <= 14


def mask(text: str) -> tuple[str, dict[str, int]]:
    """(가린 글, {종류: 개수})."""
    found: dict[str, int] = {}
    for name, pat in PATTERNS:
        def sub(m: re.Match, name=name) -> str:
            if name == "계좌번호" and not _account(m):
                return m.group()
            found[name] = found.get(name, 0) + 1
            return f"[{name}]"
        text = pat.sub(sub, text)
    return text, found
