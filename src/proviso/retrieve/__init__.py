"""검색기 인터페이스와 등록부.

검색기는 질문 하나에 검색 결과(Hit) 목록을 순위대로 돌려준다. 평가 실행기(proviso.eval.run)는 이 인터페이스만 쓴다.
장마다 새 검색기를 더하면 config/experiments/*.yaml의 retriever.type으로 고른다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from proviso.eval.metrics import Hit


class Retriever(Protocol):
    name: str

    def describe(self) -> dict[str, Any]:
        """결과 파일에 남길 설정(모델, 청킹, 파라미터 등)."""

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        """상위 k개 결과. as_of가 있으면 그날 유효한 조문만, 없으면 오늘 유효한 조문만 찾는다."""


def build_retriever(spec: dict[str, Any], conn) -> Retriever:
    """spec = {type, params, expand_aliases?}. type: random | dense | bm25 | rrf(params.parts = {이름: spec}) | rerank(params.inner = spec)."""
    kind = spec.get("type")
    params = dict(spec.get("params") or {})
    if kind == "random":
        from proviso.retrieve.random_baseline import RandomRetriever

        r = RandomRetriever(conn, **params)
    elif kind == "dense":
        from proviso.retrieve.dense import DenseRetriever

        r = DenseRetriever(conn, **params)
    elif kind == "bm25":
        from proviso.retrieve.bm25 import BM25Retriever

        r = BM25Retriever(conn, **params)
    elif kind == "rrf":
        from proviso.retrieve.hybrid import RRFRetriever

        parts = [(name, build_retriever(s, conn)) for name, s in params.pop("parts").items()]
        r = RRFRetriever(parts, **params)
    elif kind == "rerank":
        from proviso.retrieve.rerank import RerankRetriever

        r = RerankRetriever(conn, build_retriever(params.pop("inner"), conn), **params)
    else:
        raise ValueError(f"모르는 검색기 종류: {kind!r}")
    if spec.get("expand_aliases"):
        from proviso.retrieve.hybrid import AliasExpander

        r = AliasExpander(conn, r)
    return r
