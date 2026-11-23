"""시점 질의(7장) 공통: 본문 기간(text period)에서 찾은 조문을 기준일에 시행 중이던 span으로 바꾼다.

색인은 본문이 같은 기간마다 하나지만, 평가 정답·재정렬·인용은 span(버전마다 생기는 기간) 단위다.
기준일을 덮는 span 하나를 골라 그 시작일을 결과의 valid_from으로 쓴다.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date


class SpanMap:
    def __init__(self, conn):
        self._by: dict[tuple[str, str], tuple[list[date], list[date | None]]] = {}
        for doc, art, vf, vt in conn.execute(
                "SELECT doc_id, article_no, valid_from, valid_to FROM article_spans ORDER BY doc_id, article_no, valid_from"):
            starts, ends = self._by.setdefault((doc, art), ([], []))
            starts.append(vf)
            ends.append(vt)

    def at(self, doc_id: str, article_no: str, d: date) -> tuple[date, date | None] | None:
        """그날 시행 중이던 span의 (시작, 끝). 없으면 None."""
        got = self._by.get((doc_id, article_no))
        if not got:
            return None
        starts, ends = got
        i = bisect_right(starts, d) - 1
        if i < 0 or (ends[i] is not None and d >= ends[i]):
            return None
        return starts[i], ends[i]


def covers(valid_from, valid_to, d: date) -> bool:
    vf = date.fromisoformat(valid_from) if isinstance(valid_from, str) else valid_from
    vt = date.fromisoformat(valid_to) if isinstance(valid_to, str) else valid_to
    return vf <= d and (vt is None or d < vt)


def period_starts(conn) -> dict[tuple[str, str, date], date]:
    """(doc, 조, span 시작일) → 그 span이 속한 본문 기간의 시작일. 이어진 span의 본문이 같으면 한 기간이다."""
    out: dict[tuple[str, str, date], date] = {}
    prev = None
    for doc, art, vf, vt, tid in conn.execute(
            "SELECT doc_id, article_no, valid_from, valid_to, text_id FROM article_spans ORDER BY doc_id, article_no, valid_from"):
        if prev and prev[0] == doc and prev[1] == art and prev[2] == tid and prev[3] == vf:
            start = prev[4]
        else:
            start = vf
        out[(doc, art, vf)] = start
        prev = (doc, art, tid, vt, start)
    return out
