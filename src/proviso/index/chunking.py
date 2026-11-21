"""청킹. C0 = 고정 길이 청킹(1장 기준선).

C0: 문서(법령 하나)의 조문을 순서대로 이어 붙인 뒤, 조 경계를 무시하고 토큰 size개씩 자른다(겹침 overlap).
조각마다 어느 조문의 몇 글자를 담았는지 기록해, 검색 결과를 조문 단위로 채점할 수 있게 한다.
문서 경계는 넘지 않는다(법률과 시행령이 한 조각에 섞이지 않게).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SEP = "\n\n"

# 텍스트 → 토큰별 (시작, 끝) 글자 위치. 임베딩 모델의 토크나이저로 센다.
Tokenize = Callable[[str], list[tuple[int, int]]]


def join_document(articles: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    """조문을 이어 붙인 문서 텍스트와 조문별 글자 범위."""
    parts, spans, pos = [], [], 0
    for a in articles:
        if parts:
            parts.append(SEP)
            pos += len(SEP)
        parts.append(a["text"])
        spans.append((pos, pos + len(a["text"]), a))
        pos += len(a["text"])
    return "".join(parts), spans


def fixed_chunks(articles: list[dict], tokenize: Tokenize, size: int = 500, overlap: int = 50) -> list[dict[str, Any]]:
    """한 문서의 조문 목록(순서대로, 각 {article_no, valid_from, valid_to, text})을 고정 길이 조각으로 자른다."""
    if overlap >= size:
        raise ValueError("overlap은 size보다 작아야 함")
    text, spans = join_document(articles)
    toks = tokenize(text)
    if not toks:
        return []
    out = []
    step = size - overlap
    start = 0
    while True:
        window = toks[start:start + size]
        c0, c1 = window[0][0], window[-1][1]
        arts = []
        for a0, a1, a in spans:
            chars = min(c1, a1) - max(c0, a0)
            if chars > 0:
                arts.append({"article_no": a["article_no"], "valid_from": str(a["valid_from"]),
                             "valid_to": str(a["valid_to"]) if a.get("valid_to") else None, "chars": chars})
        out.append({"text": text[c0:c1], "articles": arts})
        if start + size >= len(toks):
            break
        start += step
    return out
