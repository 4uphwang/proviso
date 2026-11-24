"""색인 만들기: config/indexes/<이름>.yaml → index_versions + index_chunks.

1장 기준선은 기준일(기본: 오늘)에 유효한 조문만 넣는다(현행만).
7장: 설정에 `versions: all`이 있으면 본문이 같은 기간(text period)마다 하나씩, 제정 이후 모든 본문을 넣는다.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from proviso.config import ROOT
from proviso.index.chunking import article_chunks, fixed_chunks

INDEX_DIR = ROOT / "config" / "indexes"


def load_index_config(name_or_path: str) -> dict[str, Any]:
    p = Path(name_or_path)
    if not p.suffix:
        p = INDEX_DIR / f"{name_or_path}.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    for key in ("name", "chunking", "embedding"):
        if key not in cfg:
            raise ValueError(f"{p}: '{key}'가 없음")
    return cfg


def current_articles(conn, as_of: date) -> dict[str, list[dict]]:
    """문서별로 기준일에 유효한 조문(조 순서, 별표는 뒤). 별표는 표 테두리를 뺀 search_text를 쓴다."""
    rows = conn.execute("""
        SELECT s.doc_id, s.article_no, s.valid_from, s.valid_to, coalesce(t.search_text, t.text),
               s.heading, t.article_title, d.title
        FROM article_spans s JOIN article_texts t USING (text_id) JOIN documents d ON d.doc_id = s.doc_id
        WHERE s.valid_from <= %(d)s AND (s.valid_to IS NULL OR %(d)s < s.valid_to) AND NOT t.deleted
        ORDER BY s.doc_id, s.sort_key""", {"d": as_of}).fetchall()
    out: dict[str, list[dict]] = {}
    for doc, art, vf, vt, text, heading, title, doc_title in rows:
        out.setdefault(doc, []).append({"article_no": art, "valid_from": vf, "valid_to": vt, "text": text,
                                        "heading": heading, "title": title, "doc_title": doc_title})
    return out


def text_periods(conn) -> dict[str, list[dict]]:
    """문서별로 본문이 바뀌지 않은 기간마다 조문 하나. 버전마다 생기는 기간(span)을 본문이 같으면 이어 붙인다.

    valid_from/valid_to는 기간의 처음과 끝, spans는 그 안의 span 시작일 목록(평가 정답과 재정렬이 span 단위로 찾는다).
    장 제목은 기간의 마지막 span 것을 쓴다. 삭제된 조문은 넣지 않는다.
    """
    rows = conn.execute("""
        SELECT s.doc_id, s.article_no, s.valid_from, s.valid_to, s.text_id, coalesce(t.search_text, t.text),
               s.heading, t.article_title, d.title, t.deleted
        FROM article_spans s JOIN article_texts t USING (text_id) JOIN documents d ON d.doc_id = s.doc_id
        ORDER BY s.doc_id, s.sort_key, s.article_no, s.valid_from""").fetchall()
    out: dict[str, list[dict]] = {}
    last: dict | None = None
    for doc, art, vf, vt, tid, text, heading, title, doc_title, deleted in rows:
        if last and last["doc_id"] == doc and last["article_no"] == art and last["text_id"] == tid \
                and last["valid_to"] == vf:
            last["valid_to"] = vt
            last["spans"].append(str(vf))
            last["heading"] = heading
            continue
        last = {"doc_id": doc, "article_no": art, "valid_from": vf, "valid_to": vt, "text_id": tid, "text": text,
                "heading": heading, "title": title, "doc_title": doc_title, "spans": [str(vf)], "deleted": deleted}
        if not deleted:
            out.setdefault(doc, []).append(last)
    return out


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def latest_index(conn, name: str) -> str | None:
    row = conn.execute("SELECT index_id FROM index_versions WHERE name = %s ORDER BY created_at DESC LIMIT 1",
                       (name,)).fetchone()
    return row[0] if row else None


def build_index(conn, cfg: dict[str, Any], *, as_of: date | None = None, embedder=None, log=print) -> str:
    from proviso.eval.run import latest_snapshot
    from proviso.index.embed import Embedder

    versions = cfg.get("versions", "current")
    if versions not in ("current", "all"):
        raise ValueError(f"versions는 current나 all: {versions!r}")
    as_of = as_of or date.today()
    ch, em = cfg["chunking"], cfg["embedding"]
    if ch.get("type") not in ("fixed", "article"):
        raise ValueError(f"모르는 청킹 방식: {ch.get('type')!r}")
    embedder = embedder or Embedder(em["model"], max_length=em.get("max_length", 512),
                                    batch_size=em.get("batch_size", 16), query_prompt=em.get("query_prompt"))
    tokenize = embedder.tokenize_offsets
    if ch.get("tokenizer"):  # 12장: 조각 길이를 다른 모델 토크나이저로 세서 임베딩 모델끼리 조각을 같게 한다
        from proviso.index.embed import tokenizer_offsets

        tokenize = tokenizer_offsets(ch["tokenizer"])
    snapshot = latest_snapshot(conn)
    method = {"chunking": ch, "embedding": embedder.describe()}
    if versions == "all":
        method["versions"] = "all"
    digest = hashlib.sha256(json.dumps([method, snapshot, str(as_of)], sort_keys=True).encode()).hexdigest()[:8]
    index_id = f"{cfg['name']}-{digest}"
    if conn.execute("SELECT 1 FROM index_versions WHERE index_id = %s", (index_id,)).fetchone():
        log(f"이미 있음: {index_id}")
        return index_id

    t0 = time.perf_counter()
    chunks = []
    source = text_periods(conn) if versions == "all" else current_articles(conn, as_of)
    for doc, arts in source.items():
        if ch["type"] == "fixed":
            cs = fixed_chunks(arts, tokenize, size=ch["size"], overlap=ch["overlap"])
        else:
            cs = article_chunks(arts, tokenize, size=ch["size"], header=ch.get("header", False))
        chunks += [{"doc_id": doc, **c} for c in cs]
    log(f"조각 {len(chunks)}개, 임베딩 중({embedder.model_name})")
    vecs = embedder.encode([c["text"] for c in chunks])
    with conn.transaction():
        conn.execute(
            "INSERT INTO index_versions (index_id, name, method, snapshot_id, as_of, chunk_count, build_seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (index_id, cfg["name"], json.dumps(method), snapshot, as_of, len(chunks), round(time.perf_counter() - t0, 1)))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO index_chunks (index_id, chunk_no, doc_id, text, articles, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector)",
                [(index_id, i, c["doc_id"], c["text"], json.dumps(c["articles"], ensure_ascii=False), _vec(v))
                 for i, (c, v) in enumerate(zip(chunks, vecs))])
    log(f"완료: {index_id} ({round(time.perf_counter() - t0, 1)}초)")
    return index_id
