"""에이전트 세트(evalset/agent.jsonl) 출제 대상 뽑기와 조립.

대상은 검색 세트와 다른 시드로 뽑고, 검색 세트 정답과 같은 조문 본문(text_id)은 되도록 피한다.
약관(terms)은 수집한 문서가 없어 출제하지 않는다(계획 300 → 270).
무응답(unanswerable) 문항은 조문 없이 손으로 쓰므로 여기서 뽑지 않는다.
"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from proviso.eval.build_retrieval import lexical_overlap
from proviso.eval.schema import EvalItem
from proviso.eval.targets import (CUR, _current_articles, _past_articles, _rows, _sqrt_alloc, good_target,
                                  substantive_diff)

QUOTA = {"lookup": 54, "definition": 42, "cross_ref": 42, "multi": 36, "temporal": 48}
DEV_SHARE = 1 / 3
MAX_CHARS = 3500  # 이보다 긴 조문은 질문을 쓰는 쪽도 다 읽기 어렵고 근거 문장을 특정하기 어렵다


def used_golds(path: Path) -> set[tuple[str, str]]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        for g in json.loads(line)["gold"]:
            out.add((g["doc_id"], g["article"]))
    return out


def _slim(t: dict) -> dict:
    return {k: (str(v) if isinstance(v, date) else v) for k, v in t.items()
            if k not in ("paragraphs", "sort_key", "chunk_id", "kind")}


def sample(conn, retrieval_path: Path, today: date, seed: int = 20261122) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    avoid = used_golds(retrieval_path)
    cur = [a for a in _current_articles(conn, today)
           if a["kind"] == "article" and len(a["text"]) <= MAX_CHARS
           and good_target({**a, "category": "lookup"})]
    fresh = lambda a: (a["doc_id"], a["article_no"]) not in avoid  # noqa: E731
    out: list[dict] = []
    used: set[str] = set()

    def take(t, category, **extra):
        used.add(t["text_id"])
        out.append({"category": category, **_slim(t), **extra})

    # 정의: 호 단위(정의 조문은 검색 세트와 겹쳐도 된다. 호가 다르다)
    ret_items = {(g["doc_id"], g["article"], g.get("item")) for line in retrieval_path.read_text().splitlines()
                 for g in json.loads(line)["gold"]}
    defs = []
    for a in _current_articles(conn, today):
        if a["kind"] != "article" or not re.search(r"(^|\s)정의$", a["title"] or ""):
            continue
        for p in a["paragraphs"]:
            for it in p.get("items", []):
                if "삭제" in it["text"][:12] or not 25 <= len(it["text"]) <= MAX_CHARS:
                    continue
                if (a["doc_id"], a["article_no"], str(it["no"])) in ret_items:
                    continue
                defs.append({**a, "text": it["text"], "item": str(it["no"]), "paragraph": p["no"]})
    rng.shuffle(defs)
    for d in defs[:QUOTA["definition"]]:
        out.append({"category": "definition", **_slim(d)})
        used.add(d["text_id"])

    # 위임: 검색 세트에 안 쓴 (시행령) 조문 우선
    dels = _rows(conn, f"""
        SELECT b.chunk_id, b.doc_id, b.article_no, b.sort_key, b.valid_from, b.text_id, tb.kind,
               tb.article_title AS title, tb.text, tb.paragraphs,
               a.doc_id AS law_doc, a.article_no AS law_article, ta.text AS law_text
        FROM delegations d
        JOIN article_texts ta ON ta.text_id = d.from_text_id JOIN article_texts tb ON tb.text_id = d.to_text_id
        JOIN article_spans a ON a.text_id = ta.text_id JOIN article_spans b ON b.text_id = tb.text_id
        WHERE {CUR.replace('s.', 'a.')} AND {CUR.replace('s.', 'b.')} AND NOT tb.deleted AND NOT ta.deleted
        ORDER BY b.chunk_id, a.chunk_id""", today=today)
    dels = [d for d in dels if d["kind"] == "article" and len(d["text"]) <= MAX_CHARS
            and good_target({**d, "category": "lookup"})]
    rng.shuffle(dels)
    dels.sort(key=lambda d: not fresh(d))  # 안 쓴 것 먼저(안정 정렬이라 섞인 순서는 유지)
    n = 0
    for d in dels:
        if n >= QUOTA["cross_ref"] or d["text_id"] in used:
            continue
        take(d, "cross_ref")
        n += 1

    # 두 조문 함께: 같은 문서 안에서 본문이 참조하는 조문 쌍
    by_key = {(a["doc_id"], a["article_no"]): a for a in cur}
    refs = _rows(conn, f"""
        SELECT s.doc_id, s.article_no, r->>'doc_id' AS ref_doc, r->>'article' AS ref_article
        FROM article_spans s JOIN article_texts t USING (text_id), jsonb_array_elements(t.refs) r
        WHERE {CUR} AND NOT t.deleted AND r->>'doc_id' IS NOT NULL""", today=today)
    pairs = []
    for r in refs:
        a, b = by_key.get((r["doc_id"], r["article_no"])), by_key.get((r["ref_doc"], r["ref_article"]))
        if a and b and a["text_id"] != b["text_id"]:
            pairs.append((a, b))
    rng.shuffle(pairs)
    pairs.sort(key=lambda p: not (fresh(p[0]) and fresh(p[1])))
    n = 0
    for a, b in pairs:
        if n >= QUOTA["multi"] or a["text_id"] in used or b["text_id"] in used:
            continue
        used.add(b["text_id"])
        take(a, "multi", doc2=b["doc_id"], article2=b["article_no"], text2=b["text"], title2=b["title"])
        n += 1

    # 시점: 검색 세트에 안 쓴 옛 조문
    past = [p for p in _past_articles(conn, today) if len(p["text"]) <= MAX_CHARS
            and substantive_diff(p["text"], p["current_text"], strict=True)]
    past = [p for p in past if good_target({**p, "category": "temporal"})]
    ret_temporal = {(g["doc_id"], g["article"], g["valid_from"]) for line in retrieval_path.read_text().splitlines()
                    for g in json.loads(line)["gold"] if g.get("valid_from")}
    rng.shuffle(past)
    past.sort(key=lambda p: (p["doc_id"], p["article_no"], str(p["valid_from"])) in ret_temporal)
    seen: set[tuple] = set()
    for p in past:
        if len(seen) >= QUOTA["temporal"]:
            break
        key = (p["doc_id"], p["article_no"])
        if key in seen or p["text_id"] in used:
            continue
        seen.add(key)
        mid = p["valid_from"] + timedelta(days=(p["valid_to"] - p["valid_from"]).days // 2)
        take(p, "temporal", as_of=str(mid))

    # 조문 찾기
    pool: dict[str, list[dict]] = defaultdict(list)
    for a in cur:
        if a["text_id"] not in used and not re.search(r"(^|\s)정의$", a["title"] or ""):
            pool[a["doc_id"]].append(a)
    alloc = _sqrt_alloc({d: len(v) for d, v in pool.items()}, QUOTA["lookup"])
    for d, k in sorted(alloc.items()):
        cand = sorted(rng.sample(pool[d], len(pool[d])), key=lambda a: not fresh(a))
        for a in cand[:k]:
            take(a, "lookup")

    rng.shuffle(out)
    by: dict[str, list[dict]] = defaultdict(list)
    for o in out:
        by[o["category"]].append(o)
    for items in by.values():
        k = round(len(items) * DEV_SHARE)
        for i, o in enumerate(items):
            o["split"] = "dev" if i < k else "test"
    for i, o in enumerate(out, start=1):
        o["id"] = f"a{i:03d}"
        o["fresh_gold"] = (o["doc_id"], o["article_no"]) not in avoid
    return out


def to_item(t: dict, w: dict) -> EvalItem:
    gold = [{"doc_id": t["doc_id"], "article": t["article_no"]}]
    tags, as_of, cat = [], None, t["category"]
    if cat == "definition":
        gold[0] |= {"item": t["item"]} | ({"paragraph": t["paragraph"]} if t.get("paragraph") else {})
    elif cat == "cross_ref":
        gold.append({"doc_id": t["law_doc"], "article": t["law_article"]})
        tags.append("위임")
    elif cat == "multi":
        gold.append({"doc_id": t["doc2"], "article": t["article2"]})
    elif cat == "temporal":
        gold[0]["valid_from"], as_of = t["valid_from"], t["as_of"]
    return EvalItem.model_validate({
        "id": t["id"], "set": "agent", "split": t["split"], "question": w["question"].strip(), "category": cat,
        "answerable": True, "gold": gold, "as_of": as_of, "tags": tags, "must_include": w["must_include"],
        "difficulty": w.get("difficulty") or "medium", "style": w["style"], "origin": "auto",
        "lexical_overlap": lexical_overlap(w["question"], t["text"] + t.get("text2", "")),
        "notes": ("Claude 작성. " + (w.get("notes") or "")).strip()})
