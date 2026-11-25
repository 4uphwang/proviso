"""사람 검수 시트. 평가셋(Claude 작성)에서 사람이 볼 문항을 고르고, 검수 결과를 평가셋에 되돌린다.

전부 보기에는 많으므로(1,258문항) 두 묶음을 본다.
- 무작위 표본: 평가셋 라벨이 얼마나 맞는지 추정한다(의심 문항만 보면 오류율이 부풀려진다)
- 의심 문항: 가장 좋은 설정이 틀린 문항. 시스템이 틀린 것인지 라벨이 틀린 것인지 사람이 가린다
  - 검색: 지금 기본 검색(t1-asof)의 정답 순위가 10위 밖
  - 에이전트: 답할 수 있는 문항인데 거절했거나, 핵심어가 빠졌거나, 정답 조문을 인용하지 않음. 답할 수 없는 문항인데 답함

판정은 셋이다: ok(질문·정답·핵심어 모두 맞음), wrong(라벨이 틀림, 메모에 고칠 내용), unsure(애매).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from proviso.eval.run import load_items
from proviso.eval.schema import EvalItem

TODAY = "2026-11-20"


def gold_text(conn, doc_id: str, article: str, valid_from, as_of) -> dict | None:
    day = str(as_of) if as_of else TODAY
    if valid_from:
        sql, args = "s.valid_from=%s", (valid_from,)
    else:
        sql, args = "s.valid_from <= %s AND (s.valid_to IS NULL OR %s < s.valid_to)", (day, day)
    row = conn.execute(f"""SELECT t.text, t.article_title, s.valid_from FROM article_spans s
        JOIN article_texts t USING (text_id) WHERE s.doc_id=%s AND s.article_no=%s AND {sql}
        ORDER BY s.valid_from DESC LIMIT 1""", (doc_id, article, *args)).fetchone()
    if not row:
        return None
    return {"doc_id": doc_id, "article": article, "title": row[1] or "", "valid_from": str(row[2]), "text": row[0]}


def _cited(cites: list[dict], gold) -> bool:
    return any(c.get("article") == gold.article and (c.get("doc") in (None, gold.doc_id)) and c.get("status") == "shown"
               for c in cites)


def agent_flags(item: EvalItem, arm: dict) -> list[str]:
    """에이전트 답에서 라벨을 의심할 만한 점."""
    out = []
    if not item.answerable:
        if not arm["refused"]:
            out.append("답할 수 없는 문항인데 답함")
        return out
    if arm["refused"]:
        return ["답할 수 있는 문항인데 거절"]
    miss = [m for m in item.must_include if m not in arm["answer"]]
    if miss:
        out.append("핵심어 빠짐: " + ", ".join(miss))
    if item.gold and not _cited(arm.get("cites", []), item.gold[0]):
        out.append("주 정답 조문을 인용하지 않음")
    return out


def select(retrieval: list[EvalItem], agent: list[EvalItem], ranks: dict[str, int | None], answers: dict[str, dict],
           n_random: int = 30, seed: int = 0) -> list[dict]:
    """[{id, set, pick: 'random'|'suspect', flags, answer?}]. 이미 사람이 본(origin=human) 문항은 뺀다."""
    rng = random.Random(seed)
    rows = []
    # 검색
    sus = [i for i in retrieval if i.origin != "human" and i.id in ranks and (ranks[i.id] is None or ranks[i.id] > 10)]
    rest = [i for i in retrieval if i.origin != "human" and i not in sus]
    for i in rng.sample(rest, min(n_random, len(rest))):
        rows.append({"id": i.id, "pick": "random", "flags": []})
    for i in sus:
        r = ranks[i.id]
        rows.append({"id": i.id, "pick": "suspect", "flags": [f"기본 검색 정답 순위 {r if r else '20위 밖'}"]})
    # 에이전트
    sus_a, rest_a = [], []
    for i in agent:
        f = agent_flags(i, answers[i.id]) if i.id in answers else []
        (sus_a if f else rest_a).append((i, f))
    for i, _ in rng.sample(rest_a, min(n_random, len(rest_a))):
        rows.append({"id": i.id, "pick": "random", "flags": [], "answer": answers.get(i.id, {}).get("answer", "")})
    for i, f in sus_a:
        rows.append({"id": i.id, "pick": "suspect", "flags": f, "answer": answers[i.id]["answer"]})
    return rows


def build(conn, retrieval_path: Path, agent_path: Path, retrieval_runs: list[Path], agent_runs: list[Path],
          n_random: int = 30) -> dict:
    retrieval = load_items(retrieval_path, None)
    agent = load_items(agent_path, None)
    ranks: dict[str, int | None] = {}
    for p in retrieval_runs:
        for x in json.loads(p.read_text(encoding="utf-8"))["per_item"]:
            ranks[x["id"]] = x["rank"][0]
    answers = {}
    for p in agent_runs:
        d = json.loads(p.read_text(encoding="utf-8"))
        arm = d["arm"] if isinstance(d.get("arm"), str) else "agent_asof"
        for x in d["per_item"]:
            a = x["arms"].get(arm) or next(iter(x["arms"].values()))
            answers[x["id"]] = a
    rows = select(retrieval, agent, ranks, answers, n_random)
    items = {i.id: i for i in retrieval + agent}
    out = []
    for r in rows:
        it = items[r["id"]]
        golds = [g for g in (gold_text(conn, g.doc_id, g.article, g.valid_from, it.as_of) for g in it.gold) if g]
        out.append({**r, "set": it.set, "split": it.split, "question": it.question, "category": it.category,
                    "answerable": it.answerable, "as_of": str(it.as_of) if it.as_of else None,
                    "refusal_reason": it.refusal_reason, "must_include": it.must_include, "notes": it.notes,
                    "gold": [{"doc_id": g.doc_id, "article": g.article, "paragraph": g.paragraph, "item": g.item}
                             for g in it.gold], "gold_texts": golds})
    return {"made": TODAY, "n_random_per_set": n_random, "items": out}


def apply(evalset: Path, verdicts: dict[str, dict]) -> dict:
    """검수 결과를 평가셋에 반영한다. ok → origin=human. wrong/unsure는 notes에 메모를 남기고 origin은 그대로 둔다
    (라벨 수정은 메모를 보고 따로 한다). 바뀐 문항 수를 돌려준다."""
    lines = evalset.read_text(encoding="utf-8").splitlines()
    count = {"ok": 0, "wrong": 0, "unsure": 0}
    out = []
    for line in lines:
        d = json.loads(line)
        v = verdicts.get(d["id"])
        if v and v.get("verdict") in count:
            count[v["verdict"]] += 1
            if v["verdict"] == "ok":
                d["origin"] = "human"
            else:
                tag = f"사람 검수 {v['verdict']}: {v.get('memo', '').strip()}"
                d["notes"] = (d.get("notes", "") + " / " + tag).strip(" /")
        out.append(json.dumps(d, ensure_ascii=False))
    evalset.write_text("\n".join(out) + "\n", encoding="utf-8")
    return count
