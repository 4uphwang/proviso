"""결합(F1 RRF)과 질의 가공(Q3 약칭 확장).

RRF: 검색기마다 순위만 보고 점수 = Σ 1 / (k_rrf + 순위). 점수 크기가 다른 BM25와 코사인을 정규화 없이 합칠 수 있다.
     같은 조문 버전(doc, 조, 시행일)을 한 결과로 묶고, 검색기마다 depth개(서로 다른 조문)를 받아 합친다.
약칭 확장: 질문에 "금소법" 같은 약칭이 있으면 정식 법령명("금융소비자 보호에 관한 법률")을 질문 뒤에 붙인다.
     조각 머리말과 본문에는 정식 명칭만 있어서, 약칭만으로는 어느 법인지 단어가 맞지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from proviso.eval.metrics import Hit


class RRFRetriever:
    name = "rrf"

    def __init__(self, parts: list[tuple[str, Any]], k_rrf: int = 60, depth: int = 100):
        self._parts = parts
        self._k_rrf = k_rrf
        self._depth = depth

    def describe(self) -> dict[str, Any]:
        return {"type": "rrf", "k_rrf": self._k_rrf, "depth": self._depth,
                "parts": {name: r.describe() for name, r in self._parts}}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        fused: dict[tuple, float] = {}
        first: dict[tuple, Hit] = {}
        for _, r in self._parts:
            rank = 0
            seen: set[tuple] = set()
            for h in r.search(question, k=max(k, self._depth), as_of=as_of):
                key = (h.doc_id, h.article_no, h.valid_from)
                if key in seen:  # 한 검색기 안에서 같은 조문의 다른 조각은 첫 순위만
                    continue
                seen.add(key)
                rank += 1
                fused[key] = fused.get(key, 0.0) + 1.0 / (self._k_rrf + rank)
                first.setdefault(key, h)
        order = sorted(fused, key=lambda x: -fused[x])[:k]
        return [Hit(chunk_id=first[x].chunk_id, doc_id=x[0], article_no=x[1], valid_from=x[2],
                    valid_to=first[x].valid_to, score=fused[x]) for x in order]


def load_aliases(conn) -> list[tuple[str, str]]:
    """(약칭, 정식 법령명). 정식 명칭과 같은 별칭, 띄어쓰기만 다른 별칭은 뺀다. 긴 것부터(겹치면 긴 것이 이긴다)."""
    rows = conn.execute("SELECT a.alias, d.title FROM law_aliases a JOIN documents d USING (doc_id)").fetchall()
    titles = {t for _, t in rows}
    out = {(a, t) for a, t in rows if a.replace(" ", "") != t.replace(" ", "")}
    for a, t in list(out):  # "금소법 시행령"처럼 법률 약칭 + 시행령 → 시행령 정식 명칭
        if f"{t} 시행령" in titles:
            out |= {(f"{a} 시행령", f"{t} 시행령"), (f"{a}시행령", f"{t} 시행령")}
    return sorted(out, key=lambda x: (-len(x[0]), x[0]))


def expand_aliases(question: str, aliases: list[tuple[str, str]]) -> str:
    found: list[str] = []
    q = question
    for alias, title in aliases:
        if alias in q:
            q = q.replace(alias, " ")  # 긴 약칭이 이미 잡은 자리는 짧은 약칭이 다시 잡지 않게
            if title not in found and title not in question:
                found.append(title)
    return question if not found else question + " " + " ".join(found)


class AliasExpander:
    """다른 검색기 앞에서 질문만 바꾼다."""

    name = "aliases"

    def __init__(self, conn, inner):
        self._inner = inner
        self._aliases = load_aliases(conn)

    def describe(self) -> dict[str, Any]:
        return {**self._inner.describe(), "query": {"expand_aliases": True, "aliases": len(self._aliases)}}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        return self._inner.search(expand_aliases(question, self._aliases), k=k, as_of=as_of)
