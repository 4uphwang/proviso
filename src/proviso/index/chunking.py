"""청킹. C0 = 고정 길이 청킹(1장 기준선), C3 = 조 단위 청킹 + C1 문맥 머리말(2장).

C0: 문서(법령 하나)의 조문을 순서대로 이어 붙인 뒤, 조 경계를 무시하고 토큰 size개씩 자른다(겹침 overlap).
조각마다 어느 조문의 몇 글자를 담았는지 기록해, 검색 결과를 조문 단위로 채점할 수 있게 한다.
문서 경계는 넘지 않는다(법률과 시행령이 한 조각에 섞이지 않게).
"""

from __future__ import annotations

import re
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


# --- C3: 조 단위 청킹 ---------------------------------------------------------------

PARA_START = re.compile(r"^\s*[①-⑳㉑-㉟]")


def _units(text: str) -> list[str]:
    """조문을 항 단위 덩어리로 나눈다(줄 맨 앞의 ①②… 앞에서 끊는다). 첫 덩어리는 조 머리(제N조(제목) …)."""
    units: list[list[str]] = [[]]
    for line in text.split("\n"):
        if PARA_START.match(line) and units[-1]:
            units.append([])
        units[-1].append(line)
    return ["\n".join(u) for u in units if any(x.strip() for x in u)]


def _split_long(piece: str, tokenize: Tokenize, size: int) -> list[str]:
    """size 토큰을 넘는 덩어리는 줄 단위로, 그래도 넘는 줄은 토큰 단위로 자른다."""
    if len(tokenize(piece)) <= size:
        return [piece]
    out: list[str] = []
    for line in piece.split("\n"):
        toks = tokenize(line)
        if len(toks) <= size:
            out.append(line)
            continue
        for i in range(0, len(toks), size):
            w = toks[i:i + size]
            out.append(line[w[0][0]:w[-1][1]])
    return out


def _pack(parts: list[str], tokenize: Tokenize, size: int) -> list[str]:
    """덩어리를 순서대로 size 토큰 안에서 최대한 묶는다."""
    out: list[str] = []
    cur: list[str] = []
    for p in parts:
        cand = "\n".join(cur + [p])
        if cur and len(tokenize(cand)) > size:
            out.append("\n".join(cur))
            cur = [p]
        else:
            cur.append(p)
    if cur:
        out.append("\n".join(cur))
    return out


def header_for(a: dict) -> str:
    """C1 문맥 머리말: '[법령명] 장 제목 > 제N조(제목)'. 긴 조문을 나눈 뒤 조각마다 붙여 어느 조문인지 알게 한다."""
    title = f"{a['article_no']}({a['title']})" if a.get("title") else a["article_no"]
    path = " > ".join(x for x in (a.get("heading"), title) if x)
    return f"[{a['doc_title']}] {path}\n"


def article_chunks(articles: list[dict], tokenize: Tokenize, size: int = 480, header: bool = False) -> list[dict]:
    """조문 하나 = 조각 하나. size 토큰을 넘는 조문은 항 경계에서 나눠 여러 조각으로 만든다(모두 같은 조문을 가리킨다).

    header=True면 조각마다 문맥 머리말(C1)을 앞에 붙인다. 머리말은 size에 넣지 않는다(짧아서 512 안에 들어간다).
    """
    out = []
    for a in articles:
        parts = [s for u in _units(a["text"]) for s in _split_long(u, tokenize, size)]
        pieces = _pack(parts, tokenize, size)
        for i, p in enumerate(pieces):
            out.append({
                "text": (header_for(a) if header else "") + p,
                "articles": [{"article_no": a["article_no"], "valid_from": str(a["valid_from"]),
                              "valid_to": str(a["valid_to"]) if a.get("valid_to") else None,
                              "chars": len(p), "part": i, "parts": len(pieces)}],
            })
    return out
