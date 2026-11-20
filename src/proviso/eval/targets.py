"""검색 세트 출제 대상 조문 뽑기.

DB에 적재한 조문에서 유형별로 출제할 조문(정답)을 정한다. 질문은 이 대상을 보고 따로 쓴다.
시드를 고정해 같은 DB면 같은 대상이 나온다.
"""

from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from datetime import date, timedelta

import psycopg

# 유형별 문항 수(합계 1,000)
QUOTA = {"lookup": 670, "cross_ref": 100, "temporal": 100, "definition": 80, "appendix": 50}
MIN_LEN = 60  # 너무 짧은 조문(삭제·준용만 있는 조문 등)은 뺀다
CUR = """s.valid_from <= %(today)s AND (s.valid_to IS NULL OR %(today)s < s.valid_to)"""


ANNOT_RE = re.compile(r"<[^<>]{0,80}>|\[[^\[\]]{0,80}\]|「[^」]*」|\s+")


def substantive_diff(old: str, new: str, min_chars: int = 5) -> bool:
    """개정 표기(<개정 …>), 다른 법 이름(「…」), 공백을 빼고도 min_chars자 이상 다르면 True."""
    import difflib
    a, b = ANNOT_RE.sub("", old or ""), ANNOT_RE.sub("", new or "")
    if a == b:
        return False
    matched = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    changed = max(len(a), len(b)) - matched
    return changed >= min_chars


def _rows(conn, sql, **params):
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _current_articles(conn, today):
    return _rows(conn, f"""
        SELECT s.chunk_id, s.doc_id, s.article_no, s.sort_key, s.valid_from, s.text_id, t.kind,
               t.article_title AS title, t.text, t.paragraphs
        FROM article_spans s JOIN article_texts t USING (text_id)
        WHERE {CUR} AND NOT t.deleted AND length(t.text) >= {MIN_LEN}
        ORDER BY s.doc_id, s.sort_key""", today=today)


def _sqrt_alloc(counts: dict[str, int], total: int) -> dict[str, int]:
    """문서별 몫을 조문 수의 제곱근에 비례해 나눈다(큰 법이 세트를 다 차지하지 않게)."""
    w = {d: math.sqrt(n) for d, n in counts.items()}
    s = sum(w.values())
    alloc = {d: min(counts[d], int(total * w[d] / s)) for d in counts}
    rest = total - sum(alloc.values())
    for d in sorted(counts, key=lambda d: -counts[d]):  # 남는 몫은 큰 문서부터 채운다
        add = min(rest, counts[d] - alloc[d])
        alloc[d] += add
        rest -= add
    return alloc


def sample_targets(conn: psycopg.Connection, seed: int = 20261120, today: date | None = None) -> list[dict]:
    today = today or date.today()
    rng = random.Random(seed)
    used: set[str] = set()  # text_id 단위로 한 번만 출제
    out: list[dict] = []

    def take(t: dict, category: str, **extra):
        used.add(t["text_id"])
        out.append({"category": category, **t, **extra})

    current = _current_articles(conn, today)

    # 1) 별표
    apps = [a for a in current if a["kind"] == "appendix"]
    rng.shuffle(apps)
    for a in apps[: QUOTA["appendix"]]:
        take(a, "appendix")

    # 2) 정의: 제목에 '정의'가 있는 조문의 호 단위
    items = []
    for a in current:
        if a["kind"] != "article" or not re.search(r"(^|\s)정의$", a["title"] or "") or a["text_id"] in used:
            continue
        for p in a["paragraphs"]:
            for it in p.get("items", []):
                if "삭제" in it["text"][:12] or len(it["text"]) < 25:
                    continue
                items.append({**a, "item": it["no"], "paragraph": p["no"], "item_text": it["text"]})
    rng.shuffle(items)
    per_article: dict[str, int] = defaultdict(int)
    for it in items:
        if sum(1 for o in out if o["category"] == "definition") >= QUOTA["definition"]:
            break
        if per_article[it["chunk_id"]] >= 12:  # 한 정의 조문에 몰리지 않게
            continue
        per_article[it["chunk_id"]] += 1
        out.append({"category": "definition", **it})
    # 정의 조문은 호마다 따로 출제하므로 used에는 넣지 않는다(다른 유형에서만 제외)
    def_ids = {o["text_id"] for o in out if o["category"] == "definition"}
    used |= def_ids

    # 3) 위임 따라가기: 현행 법 조문 → 현행 시행령 조문
    dels = _rows(conn, f"""
        SELECT b.chunk_id, b.doc_id, b.article_no, b.sort_key, b.valid_from, b.text_id, tb.kind,
               tb.article_title AS title, tb.text, tb.paragraphs,
               a.doc_id AS law_doc, a.article_no AS law_article, ta.text AS law_text, d.phrase
        FROM delegations d
        JOIN article_texts ta ON ta.text_id = d.from_text_id
        JOIN article_texts tb ON tb.text_id = d.to_text_id
        JOIN article_spans a ON a.text_id = ta.text_id
        JOIN article_spans b ON b.text_id = tb.text_id
        WHERE {CUR.replace('s.', 'a.')} AND {CUR.replace('s.', 'b.')} AND NOT tb.deleted
        ORDER BY b.chunk_id, a.chunk_id""", today=today)
    rng.shuffle(dels)
    for d in dels:
        if sum(1 for o in out if o["category"] == "cross_ref") >= QUOTA["cross_ref"]:
            break
        if d["text_id"] in used:
            continue
        take(d, "cross_ref")

    # 4) 시점: 지금은 바뀐 옛 조문. 기준일은 그 조문이 유효했던 기간의 가운데
    past = _rows(conn, """
        SELECT s.chunk_id, s.doc_id, s.article_no, s.sort_key, s.valid_from, s.valid_to, s.text_id, t.kind,
               t.article_title AS title, t.text, t.paragraphs, c.text AS current_text, c.text_id AS current_text_id
        FROM article_spans s JOIN article_texts t USING (text_id)
        JOIN article_spans cs ON cs.doc_id = s.doc_id AND cs.article_no = s.article_no
             AND cs.valid_from <= %(today)s AND (cs.valid_to IS NULL OR %(today)s < cs.valid_to)
        JOIN article_texts c ON c.text_id = cs.text_id
        WHERE s.valid_to IS NOT NULL AND s.valid_to <= %(today)s AND s.valid_to - s.valid_from >= 180
          AND NOT t.deleted AND NOT c.deleted AND t.text_id <> c.text_id AND t.kind = 'article'
          AND length(t.text) >= %(min_len)s
        ORDER BY s.chunk_id""", today=today, min_len=MIN_LEN)
    rng.shuffle(past)
    seen_articles: set[tuple] = set()
    for p in past:
        if sum(1 for o in out if o["category"] == "temporal") >= QUOTA["temporal"]:
            break
        key = (p["doc_id"], p["article_no"])
        if key in seen_articles or p["text_id"] in used or not substantive_diff(p["text"], p["current_text"]):
            continue
        seen_articles.add(key)
        mid = p["valid_from"] + timedelta(days=(p["valid_to"] - p["valid_from"]).days // 2)
        take(p, "temporal", as_of=mid)

    # 5) 조문 찾기: 나머지 현행 조문에서 문서별 제곱근 비례
    pool: dict[str, list[dict]] = defaultdict(list)
    for a in current:
        if a["kind"] == "article" and a["text_id"] not in used:
            pool[a["doc_id"]].append(a)
    need = sum(QUOTA.values()) - len(out)  # lookup 몫. 다른 유형이 모자라면 lookup으로 채운다
    alloc = _sqrt_alloc({d: len(v) for d, v in pool.items()}, need)
    for d, n in sorted(alloc.items()):
        for a in rng.sample(pool[d], n):
            take(a, "lookup")

    # dev/test 나누기(유형별 30%)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for o in out:
        by_cat[o["category"]].append(o)
    for items_ in by_cat.values():
        rng.shuffle(items_)
        k = round(len(items_) * 0.3)
        for i, o in enumerate(items_):
            o["split"] = "dev" if i < k else "test"
    rng.shuffle(out)
    for i, o in enumerate(out, start=1):
        o["id"] = f"r{i:04d}"
    return out
