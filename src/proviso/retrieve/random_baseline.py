"""무작위 검색기: 기준일에 유효한 조문 중에서 무작위로 k개를 고른다.

검색을 전혀 안 했을 때의 점수(우연 수준)를 보여 주고, 평가 실행기가 끝까지 도는지 확인하는 용도다.
질문 문자열로 시드를 정해 같은 질문이면 같은 결과가 나온다(결정적).
"""

from __future__ import annotations

import hashlib
import random
from datetime import date
from typing import Any

from proviso.eval.metrics import Hit


class RandomRetriever:
    name = "random"

    def __init__(self, conn, seed: int = 0):
        self._seed = seed
        rows = conn.execute("""
            SELECT s.chunk_id, s.doc_id, s.article_no, s.valid_from, s.valid_to
            FROM article_spans s JOIN article_texts t USING (text_id)
            WHERE NOT t.deleted ORDER BY s.chunk_id""").fetchall()
        self._spans = [Hit(chunk_id=r[0], doc_id=r[1], article_no=r[2], valid_from=r[3], valid_to=r[4]) for r in rows]
        self._by_day: dict[date, list[Hit]] = {}

    def describe(self) -> dict[str, Any]:
        return {"type": "random", "seed": self._seed}

    def _valid_on(self, d: date) -> list[Hit]:
        if d not in self._by_day:
            self._by_day[d] = [h for h in self._spans if h.valid_from <= d and (h.valid_to is None or d < h.valid_to)]
        return self._by_day[d]

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        pool = self._valid_on(as_of or date.today())
        h = int(hashlib.sha256(f"{self._seed}:{question}".encode()).hexdigest()[:16], 16)
        return random.Random(h).sample(pool, min(k, len(pool)))
