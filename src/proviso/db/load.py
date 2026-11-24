"""파싱한 법령을 DB에 적재한다. 같은 입력을 다시 넣어도 결과가 같다(멱등)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from proviso.collect.laws import raw_body_path
from proviso.config import Sources
from proviso.parse.articles import Article, Version, parse_law


def name_index(sources: Sources) -> dict[str, str]:
    """법령명·약칭·id → doc_id. 본문의 「법령명」을 doc_id로 풀 때 쓴다."""
    idx: dict[str, str] = {}
    for s in sources.laws:
        idx[s.id] = s.id
        idx[s.query] = s.id
        for a in s.aliases:
            idx[a] = s.id
    return idx


def upsert_document(conn: psycopg.Connection, sources: Sources, version: Version) -> None:
    src = next(s for s in sources.laws if s.id == version.doc_id)
    conn.execute(
        """INSERT INTO documents (doc_id, doc_type, parent_doc_id, title, law_id)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (doc_id) DO UPDATE SET title = EXCLUDED.title, law_id = EXCLUDED.law_id,
             parent_doc_id = EXCLUDED.parent_doc_id, doc_type = EXCLUDED.doc_type""",
        (src.id, src.type, src.parent, version.title, version.law_id),
    )
    aliases = [(a, "manual") for a in src.aliases]
    if version.alias:
        aliases.append((version.alias, "api"))
    if version.title != src.id:
        aliases.append((version.title, "api"))
    for alias, origin in aliases:
        row = conn.execute(
            """INSERT INTO law_aliases (alias, doc_id, origin) VALUES (%s, %s, %s)
               ON CONFLICT (alias) DO UPDATE SET origin = law_aliases.origin
               RETURNING doc_id""",
            (alias, src.id, origin),
        ).fetchone()
        if row[0] != src.id:
            raise ValueError(f"약칭 {alias!r}가 이미 {row[0]!r}에 연결돼 있음")


def load_version(conn: psycopg.Connection, sources: Sources, version: Version, articles: list[Article],
                 status: str | None = None) -> int:
    """한 버전(MST, 시행일)의 조문을 넣는다. 반환값: 이 버전의 조문 수."""
    upsert_document(conn, sources, version)
    eff = version.effective_on
    conn.execute(
        """INSERT INTO law_versions (doc_id, mst, effective_on, promulgated_on, revision_kind, status, title)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (doc_id, mst, effective_on) DO UPDATE SET promulgated_on = EXCLUDED.promulgated_on,
             revision_kind = EXCLUDED.revision_kind, status = EXCLUDED.status, title = EXCLUDED.title""",
        (version.doc_id, version.mst, eff, version.promulgated_on, version.revision_kind, status, version.title),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO article_texts (text_id, doc_id, article_no, article_title, text, paragraphs, refs,
                                          delegation_phrases, delegated_from, deleted, kind, search_text)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (text_id) DO UPDATE SET refs = EXCLUDED.refs, delegated_from = EXCLUDED.delegated_from,
                 delegation_phrases = EXCLUDED.delegation_phrases, paragraphs = EXCLUDED.paragraphs""",
            [(a.text_id, a.doc_id, a.article_no, a.title, a.text, Jsonb(a.paragraphs), Jsonb(a.refs),
              Jsonb(a.delegations), Jsonb(a.delegated_from), a.deleted, a.kind, a.search_text) for a in articles],
        )
        # 같은 시행일 버전이 둘이면(같은 날 시행되는 개정 두 건) 늦게 공포된 쪽을 나중에 넣어 덮어쓴다
        cur.executemany(
            """INSERT INTO article_spans (chunk_id, doc_id, article_no, sort_key, text_id, mst, version_effective_on,
                                          valid_from, heading, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (chunk_id) DO UPDATE SET text_id = EXCLUDED.text_id, mst = EXCLUDED.mst,
                 version_effective_on = EXCLUDED.version_effective_on,
                 heading = EXCLUDED.heading, note = EXCLUDED.note, sort_key = EXCLUDED.sort_key""",
            [(f"{a.doc_id}:{a.article_no}@{eff.isoformat()}", a.doc_id, a.article_no, a.sort_key,
              a.text_id, version.mst, eff, eff, a.heading, a.note) for a in articles],
        )
        # 이 시행일에 더는 없는 조문(늦게 공포된 버전에서 빠진 조문)의 기간은 지운다
        cur.execute(
            """DELETE FROM article_spans WHERE doc_id = %s AND valid_from = %s AND valid_from_basis = 'version'
                 AND NOT (article_no = ANY(%s))""",
            (version.doc_id, eff, [a.article_no for a in articles]),
        )
    return len(articles)


def recompute_validity(conn: psycopg.Connection, doc_id: str) -> None:
    """valid_to = 이 문서에서 다음으로 시행되는 버전의 시행일(없으면 NULL).

    공포 순서가 아니라 시행일 순서로 계산한다(늦게 공포된 개정이 먼저 시행되는 경우가 있음).
    다음 버전에서 조문이 빠졌으면 그 조문의 기간도 다음 시행일에 닫힌다.
    """
    conn.execute(
        """UPDATE article_spans s SET valid_to = (
               SELECT min(v.effective_on) FROM law_versions v
               WHERE v.doc_id = s.doc_id AND v.effective_on > s.valid_from)
           WHERE s.doc_id = %s AND s.valid_from_basis = 'version'""",
        (doc_id,),
    )


def build_delegations(conn: psycopg.Connection) -> int:
    """법 조문의 위임 문구 ↔ '법 제N조…에서 "대통령령으로 정하는 …"'로 그 조를 밝힌 시행령 조문을 잇는다.

    시행령이 법 조문을 단순히 언급하는 경우(…에 따라)는 넣지 않아 정밀도를 우선한다.
    두 조문이 같은 기간에 유효해야 하고, 시행령 쪽에 항이 있으면 법 쪽 그 항에 위임 문구가 있어야 한다.
    """
    conn.execute("DELETE FROM delegations WHERE source = 'parsed'")
    cur = conn.execute(
        """INSERT INTO delegations (from_text_id, to_text_id, phrase, source)
           SELECT DISTINCT ON (t1.text_id, t2.text_id) t1.text_id, t2.text_id,
                  t1.delegation_phrases->0->>'phrase', 'parsed'
           FROM article_texts t2
           JOIN documents d2 ON d2.doc_id = t2.doc_id AND d2.parent_doc_id IS NOT NULL
           CROSS JOIN LATERAL jsonb_array_elements(t2.delegated_from) r
           JOIN article_texts t1 ON t1.doc_id = d2.parent_doc_id AND t1.doc_id = r->>'doc_id'
                                AND t1.article_no = r->>'article'
                                AND jsonb_array_length(t1.delegation_phrases) > 0
           WHERE (r->>'paragraph' IS NULL OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements(t1.delegation_phrases) p
                    WHERE p->>'paragraph' = r->>'paragraph'))
             AND EXISTS (
                    SELECT 1 FROM article_spans s1 JOIN article_spans s2 ON s2.text_id = t2.text_id
                    WHERE s1.text_id = t1.text_id
                      AND s1.valid_from < COALESCE(s2.valid_to, 'infinity'::date)
                      AND s2.valid_from < COALESCE(s1.valid_to, 'infinity'::date))
           ON CONFLICT DO NOTHING"""
    )
    return cur.rowcount


def create_snapshot(conn: psycopg.Connection, versions_by_doc: dict[str, list[dict]]) -> str:
    # 17장: 어떤 기간(span)도 가리키지 않는 본문(같은 시행일 버전을 덮어쓸 때 남는 것)은 내용에 넣지 않는다
    rows = conn.execute("""SELECT text_id FROM article_texts t
                           WHERE EXISTS (SELECT 1 FROM article_spans s WHERE s.text_id = t.text_id)
                           ORDER BY text_id""").fetchall()
    digest = hashlib.sha256("\n".join(r[0] for r in rows).encode()).hexdigest()
    snap_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + digest[:8]
    count = conn.execute("SELECT count(*) FROM article_spans").fetchone()[0]
    latest = {
        d: max(((v["effective"], v["mst"]) for v in vs if v.get("status") in ("현행", "시행예정")), default=None)
        for d, vs in versions_by_doc.items()
    }
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, latest_mst, article_count, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, clock_timestamp())"  # 17장: 한 트랜잭션 안에서도 나중 것이 최신이 되게
        " ON CONFLICT (snapshot_id) DO NOTHING",  # 같은 초에 같은 내용이면 같은 스냅샷
        (snap_id, Jsonb(latest), count, digest),
    )
    return snap_id


def _ymd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def load_versions(conn: psycopg.Connection, sources: Sources, raw_dir: Path,
                  versions_by_doc: dict[str, list[dict]]) -> dict:
    """수집해 둔 버전 원본(data/raw/eflaw/<MST>_<시행일>.json)을 파싱해 한 트랜잭션으로 적재한다.

    원본이 있는 버전만 넣는다(현행만 받았으면 현행·시행예정만). 반환값: 문서별 적재 버전 수·조문 수 등.
    """
    names = name_index(sources)
    loaded: dict[str, dict] = {}
    with conn.transaction():
        # 시행령이 모법을 참조하므로 법률을 먼저 넣는다
        for src in sorted(sources.laws, key=lambda s: s.type != "act"):
            n_versions = n_rows = 0
            for v in sorted(versions_by_doc.get(src.id, []), key=lambda v: (v["effective"], v["promulgated"], v["mst"])):
                path = raw_body_path(raw_dir, v["mst"], v["effective"])
                if not path.exists():
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
                version, articles = parse_law(raw, src.id, mst=v["mst"], parent_doc=src.parent, name_to_doc=names)
                version.effective_on = _ymd(v["effective"])  # 목록의 시행일을 기준으로 삼는다
                n_rows += load_version(conn, sources, version, articles, status=v.get("status"))
                n_versions += 1
            if n_versions:
                recompute_validity(conn, src.id)
                loaded[src.id] = {"versions": n_versions, "article_rows": n_rows}
        delegations = build_delegations(conn)
        snapshot_id = create_snapshot(conn, versions_by_doc)
        unique_texts = conn.execute("SELECT count(*) FROM article_texts").fetchone()[0]
    return {"docs": loaded, "delegations": delegations, "snapshot_id": snapshot_id, "unique_texts": unique_texts}
