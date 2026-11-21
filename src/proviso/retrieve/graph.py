"""참조 그래프 확장(G1): 앞 단계 상위 결과와 위임·참조로 이어진 조문의 순위를 올린다.

이음: 위임(법 "대통령령으로 정하는" ↔ 시행령 조문, delegations 표)과 조문 본문의 참조("제N조", refs 열).
양방향으로 본다. 점수 = 앞 단계 RRF식 점수 1/(60 + 순위) + weight × Σ(이어진 상위 seeds개 조문의 1/(60 + 순위)).
앞 단계에서 이미 찾았고 상위 조문과도 이어진 조문이 위로 올라온다. 앞 단계에 없던 이웃도 후보에 들어가지만,
weight가 1보다 작으면 상위 몇 위 안에 들기는 어렵다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from proviso.eval.metrics import Hit


def current_links(conn, as_of: date) -> dict[tuple, set[tuple]]:
    """(doc, 조) → 이어진 (doc, 조) 집합. as_of에 유효한 버전끼리만."""
    spans = conn.execute(
        "SELECT s.text_id, s.doc_id, s.article_no, s.valid_from, s.valid_to, t.refs FROM article_spans s"
        " JOIN article_texts t USING (text_id)"
        " WHERE s.valid_from <= %(d)s AND (s.valid_to IS NULL OR %(d)s < s.valid_to) AND NOT t.deleted",
        {"d": as_of}).fetchall()
    by_text = {tid: (doc, art) for tid, doc, art, *_ in spans}
    exists = set(by_text.values())
    links: dict[tuple, set[tuple]] = {}

    def add(a, b):
        if a != b and a in exists and b in exists:
            links.setdefault(a, set()).add(b)
            links.setdefault(b, set()).add(a)

    for f, t in conn.execute("SELECT from_text_id, to_text_id FROM delegations").fetchall():
        if f in by_text and t in by_text:
            add(by_text[f], by_text[t])
    for tid, doc, art, _, _, refs in spans:
        for r in refs or []:
            if r.get("doc_id") and r.get("article"):
                add((doc, art), (r["doc_id"], r["article"]))
    return links


class GraphExpandRetriever:
    name = "graph"

    def __init__(self, conn, inner, *, as_of: str | date = "2026-11-20", seeds: int = 5, weight: float = 0.5,
                 k_rrf: int = 60, depth: int = 100):
        self._inner = inner
        as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        self._links = current_links(conn, as_of)
        self._spans = {(d, a): (vf, vt) for d, a, vf, vt in conn.execute(
            "SELECT doc_id, article_no, valid_from, valid_to FROM article_spans"
            " WHERE valid_from <= %(d)s AND (valid_to IS NULL OR %(d)s < valid_to)", {"d": as_of}).fetchall()}
        self._p = {"as_of": str(as_of), "seeds": seeds, "weight": weight, "k_rrf": k_rrf, "depth": depth,
                   "linked_articles": len(self._links)}

    def describe(self) -> dict[str, Any]:
        return {"type": "graph", **self._p, "inner": self._inner.describe()}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        p = self._p
        hits = self._inner.search(question, k=max(k, p["depth"]), as_of=as_of)
        score: dict[tuple, float] = {}
        meta: dict[tuple, Hit] = {}
        for rank, h in enumerate(hits, 1):
            key = (h.doc_id, h.article_no)
            if key not in score:
                score[key] = 1.0 / (p["k_rrf"] + rank)
                meta[key] = h
        for rank, h in enumerate(hits[:p["seeds"]], 1):
            for nb in self._links.get((h.doc_id, h.article_no), ()):
                score[nb] = score.get(nb, 0.0) + p["weight"] / (p["k_rrf"] + rank)
        order = sorted(score, key=lambda x: -score[x])[:k]
        out = []
        for key in order:
            if key in meta:
                h = meta[key]
                out.append(Hit(h.chunk_id, h.doc_id, h.article_no, h.valid_from, h.valid_to, score[key]))
            else:
                vf, vt = self._spans[key]
                out.append(Hit(f"graph:{key[0]}:{key[1]}", key[0], key[1], vf, vt, score[key]))
        return out
