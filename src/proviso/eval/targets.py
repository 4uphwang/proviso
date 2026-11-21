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
# 조문을 가리키는 말(법 제3조제2항제1호가목, 같은 조, 각 호 등). 번호만 바뀐 개정을 차이로 보지 않으려고 지운다
REF_TEXT_RE = re.compile(r"(법\s*)?제\d+조(의\d+)?(제\d+항)?(제\d+호(의\d+)?)?([가-하]목)?|제\d+(항|호)(의\d+)?|[가-하]목|같은\s*조|각\s*[호목]")
# 내용은 같고 용어만 바뀐 개정(전자금융 법령에서 흔함)
TERM_SYNONYMS = [("금융기관", "금융회사"), ("모사전송번호", "팩스번호"), ("모사전송", "팩스")]


def _normalize_for_diff(text: str) -> str:
    t = text or ""
    for old, new in TERM_SYNONYMS:
        t = t.replace(old, new)
    return ANNOT_RE.sub("", REF_TEXT_RE.sub("", t))


def substantive_diff(old: str, new: str, min_chars: int = 5, strict: bool = False) -> bool:
    """옛 본문과 지금 본문이 min_chars자 이상 다르면 True. 개정 표기(<개정 …>), 다른 법 이름(「…」), 공백은 무시한다.

    strict=True(교체 대상 뽑을 때, 검토에서 나온 기준): 조문 번호, 용어만 바뀐 것(금융기관→금융회사)도 무시하고,
    옛 본문 쪽 차이만 센다. 지금 본문에 새로 생긴 내용만 있으면 옛 본문으로 물을 거리가 없기 때문이다.
    """
    import difflib
    if strict:
        a, b = _normalize_for_diff(old), _normalize_for_diff(new)
    else:
        a, b = ANNOT_RE.sub("", old or ""), ANNOT_RE.sub("", new or "")
    if a == b:
        return False
    matched = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return (len(a) if strict else max(len(a), len(b))) - matched >= min_chars


POINTER_WORDS_RE = re.compile(
    r"대통령령으로\s*정하는|총리령으로\s*정하는|금융위원회가\s*정하여\s*고시하는|금융위원회가\s*고시하는|그\s*밖에|"
    r"에\s*해당하는|에\s*따른|의\s*어느\s*하나|준용한다|자|것|기관|정보|경우|를\s*말한다"
)


def pointer_only(text: str, min_chars: int = 20) -> bool:
    """다른 조문·법령을 가리키거나 '그 밖에 대통령령/고시로 정하는 ~'뿐이라 이 본문만 읽어서는 답이 안 나오면 True."""
    t = REF_TEXT_RE.sub("", ANNOT_RE.sub("", re.sub(r"^\s*(제\d+조(의\d+)?\([^)]*\)|\d+(의\d+)?\.)", "", text or "")))
    t = POINTER_WORDS_RE.sub("", t)
    return len(re.findall(r"[가-힣0-9]", t)) < min_chars


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


def _past_articles(conn, today):
    """지금은 바뀐 옛 조문(180일 이상 유효했던 것)과 그 조문의 지금 본문."""
    return _rows(conn, """
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
    past = _past_articles(conn, today)
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

    _split_and_number(out, rng)
    return out


def _split_and_number(out: list[dict], rng: random.Random) -> None:
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


def good_target(t: dict) -> bool:
    """교체 대상 조건(1차 검토에서 나온 기준): 다른 조문을 가리키기만 하는 본문, 목적 조항, 옛 본문 쪽 차이가 없는 시점 대상은 뺀다."""
    if re.search(r"목적$|준용", t.get("title") or ""):  # 목적 조항, 다른 조문을 그대로 쓰라는 준용 조항
        return False
    if pointer_only(t.get("item_text") or t["text"]):
        return False
    if t["category"] == "temporal" and not substantive_diff(t["text"], t["current_text"], strict=True):
        return False
    if t["category"] == "appendix" and len(re.findall(r"\d", t["text"])) < 10:  # 숫자·기준이 없는 참조표
        return False
    return True


def replacement_candidates(conn: psycopg.Connection, targets: list[dict], drop_ids: list[str], per_drop: int = 3,
                           seed: int = 20261121, today: date | None = None) -> dict[str, list[dict]]:
    """빠진 문항마다 같은 유형의 새 대상 후보를 per_drop개씩 고른다(이미 쓴 조문 제외, good_target 통과).

    시드 고정이라 같은 DB·같은 drop 목록이면 같은 후보가 나온다. 후보 중 질문을 쓸 수 있는 첫 번째를 쓴다.
    """
    today = today or date.today()
    pool = sample_targets(conn, seed=seed, today=today)  # 다른 시드로 뽑은 전체 대상에서 후보를 고른다
    # 시점 후보는 몫(100개)에 묶이지 않게 옛 조문 전체에서 따로 고른다(조문당 하나, 기준일은 유효 기간 가운데)
    rng = random.Random(seed)
    past = _past_articles(conn, today)
    rng.shuffle(past)
    for p in past:
        mid = p["valid_from"] + timedelta(days=(p["valid_to"] - p["valid_from"]).days // 2)
        pool.append({"category": "temporal", **p, "as_of": mid})
    taken_temporal = {(t["doc_id"], t["article_no"]) for t in targets if t["category"] == "temporal"}
    # 같은 조문이 버전마다 text_id가 다를 수 있어 (문서, 조, 항, 호)로도 막는다
    used = {t["text_id"] for t in targets} | {(t["doc_id"], t["article_no"], str(t.get("paragraph")), str(t.get("item"))) for t in targets}
    by_id = {t["id"]: t for t in targets}
    out: dict[str, list[dict]] = {}
    for did in sorted(drop_ids):
        cat = by_id[did]["category"]
        picked = []
        for c in pool:
            akey = (c["doc_id"], c["article_no"], str(c.get("paragraph")), str(c.get("item")))
            if c["category"] != cat or akey in used or (cat != "definition" and c["text_id"] in used) or not good_target(c):
                continue
            if cat == "temporal":
                if (c["doc_id"], c["article_no"]) in taken_temporal:
                    continue
                taken_temporal.add((c["doc_id"], c["article_no"]))
            used |= {akey, c["text_id"]} if cat != "definition" else {akey}
            picked.append({**c, "id": did, "split": by_id[did]["split"]})
            if len(picked) >= per_drop:
                break
        out[did] = picked
    return out
