"""질문에서 기준일 읽기(7장). 규칙만 쓴다(모델 호출 없음).

- "2014년 3월 5일", "2014.3.5." → 그날
- "2014년 3월" → 그달 15일
- "2014년", 문장 속 "2014" → 그해 7월 1일. 연도만 말한 질문은 그해 중 어느 날인지 모르므로 한가운데로 잡는다
- 올해(today의 연도) 이후는 현행으로 본다(None)
금액·수량("2000만원", "1000개")은 연도로 읽지 않는다.
"""

from __future__ import annotations

import re
from datetime import date

FULL = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(?:년\s*|[.\-/]\s*)(\d{1,2})\s*(?:월\s*|[.\-/]\s*)(\d{1,2})\s*일?")
MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월")
YEAR = re.compile(r"(?<![\d.])((?:19|20)\d{2})(?!\s*(?:\d|[.,]\d|만|억|천|원|개|명|건|%|조|항|호))")
MID_YEAR = (7, 1)


def question_date(question: str, today: date | None = None) -> date | None:
    today = today or date.today()
    found: list[date] = []
    for y, m, d in FULL.findall(question):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if not found:
        for y, m in MONTH.findall(question):
            if 1 <= int(m) <= 12:
                found.append(date(int(y), int(m), 15))
    if not found:
        found = [date(int(y), *MID_YEAR) for y in YEAR.findall(question) if 1948 <= int(y)]
    found = [d for d in found if d.year < today.year]
    return min(found) if found else None
