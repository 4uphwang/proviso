"""색인 만들기: config/indexes/<이름>.yaml → index_versions + index_chunks.

1장 기준선은 기준일(기본: 오늘)에 유효한 조문만 넣는다(현행만). 과거 버전은 7장에서 다룬다.
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
from proviso.index.chunking import fixed_chunks

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
        SELECT s.doc_id, s.article_no, s.valid_from, s.valid_to, coalesce(t.search_text, t.text)
        FROM article_spans s JOIN article_texts t USING (text_id)
        WHERE s.valid_from <= %(d)s AND (s.valid_to IS NULL OR %(d)s < s.valid_to) AND NOT t.deleted
        ORDER BY s.doc_id, s.sort_key""", {"d": as_of}).fetchall()
    out: dict[str, list[dict]] = {}
    for doc, art, vf, vt, text in rows:
        out.setdefault(doc, []).append({"article_no": art, "valid_from": vf, "valid_to": vt, "text": text})
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

    as_of = as_of or date.today()
    ch, em = cfg["chunking"], cfg["embedding"]
    if ch.get("type") != "fixed":
        raise ValueError(f"모르는 청킹 방식: {ch.get('type')!r}")
    embedder = embedder or Embedder(em["model"], max_length=em.get("max_length", 512),
                                    batch_size=em.get("batch_size", 16))
    snapshot = latest_snapshot(conn)
    method = {"chunking": ch, "embedding": embedder.describe()}
    digest = hashlib.sha256(json.dumps([method, snapshot, str(as_of)], sort_keys=True).encode()).hexdigest()[:8]
    index_id = f"{cfg['name']}-{digest}"
    if conn.execute("SELECT 1 FROM index_versions WHERE index_id = %s", (index_id,)).fetchone():
        log(f"이미 있음: {index_id}")
        return index_id

    t0 = time.perf_counter()
    chunks = []
    for doc, arts in current_articles(conn, as_of).items():
        for c in fixed_chunks(arts, embedder.tokenize_offsets, size=ch["size"], overlap=ch["overlap"]):
            chunks.append({"doc_id": doc, **c})
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
