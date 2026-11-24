"""벡터 검색(D1): 질문 임베딩과 색인 조각 임베딩의 코사인 거리로 상위 k 조각을 찾는다.

조각 하나가 조문 여러 개에 걸치면(고정 길이 청킹) 조각 안 순서대로 조문을 모두 결과에 넣는다.
채점은 같은 조문 버전을 묶어 처음 나온 순위만 쓰므로, 순위는 '조문 기준'이 된다.
index는 현행 조문만 담는다. 7장: versions_index(모든 본문 기간 색인)를 주면, 질문에 기준일이 있을 때
그 색인에서 그날 시행 중이던 본문의 조각만 찾는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from proviso.eval.metrics import Hit


class DenseRetriever:
    name = "dense"

    def __init__(self, conn, index: str, embedder=None, versions_index: str | None = None):
        from proviso.index.build import latest_index
        from proviso.index.embed import Embedder

        self._conn = conn
        self._index_id = latest_index(conn, index)
        if not self._index_id:
            raise ValueError(f"색인 {index!r}이 없음. 먼저 `proviso index build {index}`")
        method, n = conn.execute("SELECT method, chunk_count FROM index_versions WHERE index_id = %s",
                                 (self._index_id,)).fetchone()
        self._method = method
        self._chunks = n
        em = method["embedding"]
        self._embedder = embedder or Embedder(em["model"], max_length=em.get("max_length", 512),
                                                     query_prompt=em.get("query_prompt"))
        self._vindex_id = None
        if versions_index:
            from proviso.retrieve.versions import SpanMap

            self._vindex_id = latest_index(conn, versions_index)
            if not self._vindex_id:
                raise ValueError(f"색인 {versions_index!r}이 없음. 먼저 `proviso index build {versions_index}`")
            self._spans = SpanMap(conn)

    def describe(self) -> dict[str, Any]:
        return {"type": "dense", "index_id": self._index_id, "chunks": self._chunks, **self._method,
                "versions_index_id": self._vindex_id, "uses_as_of": self._vindex_id is not None}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        """서로 다른 조문이 k개 모일 때까지 가까운 조각부터 담는다.

        청킹 방식마다 조각 하나에 든 조문 수가 달라서(고정 길이는 여럿, 조 단위는 하나, 긴 조문은 여러 조각),
        '조각 k개'가 아니라 '조문 k개'로 맞춰야 방식 간 비교가 공정하다.
        """
        enc = getattr(self._embedder, "encode_query", self._embedder.encode)
        q = enc([question])[0]
        qv = "[" + ",".join(f"{x:.6f}" for x in q) + "]"
        if self._vindex_id and as_of is not None:
            return self._search_at(qv, k, as_of)
        rows = self._conn.execute(
            "SELECT chunk_no, doc_id, articles, 1 - (embedding <=> %s::vector) FROM index_chunks"
            " WHERE index_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, self._index_id, qv, k * 5)).fetchall()
        hits: list[Hit] = []
        seen: set[tuple] = set()
        for chunk_no, doc_id, arts, score in rows:
            if len(seen) >= k:
                break
            for a in arts:
                if len(seen) >= k and (doc_id, a["article_no"], a["valid_from"]) not in seen:
                    break
                seen.add((doc_id, a["article_no"], a["valid_from"]))
                hits.append(Hit(chunk_id=f"{self._index_id}#{chunk_no}", doc_id=doc_id, article_no=a["article_no"],
                                valid_from=date.fromisoformat(a["valid_from"]),
                                valid_to=date.fromisoformat(a["valid_to"]) if a["valid_to"] else None,
                                score=float(score)))
        return hits

    def _search_at(self, qv: str, k: int, as_of: date) -> list[Hit]:
        rows = self._conn.execute(
            "SELECT chunk_no, doc_id, articles, 1 - (embedding <=> %(q)s::vector) FROM index_chunks c"
            " WHERE index_id = %(i)s AND EXISTS (SELECT 1 FROM jsonb_array_elements(c.articles) x"
            "   WHERE (x->>'valid_from')::date <= %(d)s AND (x->>'valid_to' IS NULL OR %(d)s < (x->>'valid_to')::date))"
            " ORDER BY embedding <=> %(q)s::vector LIMIT %(n)s",
            {"q": qv, "i": self._vindex_id, "d": as_of, "n": k * 5}).fetchall()
        hits: list[Hit] = []
        seen: set[tuple] = set()
        for chunk_no, doc_id, arts, score in rows:
            if len(seen) >= k:
                break
            for a in arts:
                span = self._spans.at(doc_id, a["article_no"], as_of)
                if span is None or (doc_id, a["article_no"]) in seen:
                    continue
                seen.add((doc_id, a["article_no"]))
                hits.append(Hit(chunk_id=f"{self._vindex_id}#{chunk_no}", doc_id=doc_id, article_no=a["article_no"],
                                valid_from=span[0], valid_to=span[1], score=float(score)))
        return hits
