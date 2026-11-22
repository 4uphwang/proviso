"""에이전트 세트 검증: 정답 조문과 후보 조문을 섞어 보여 주고, 로컬 LLM이 정답 조문을 고르는지 본다.

계획(§7.1.1)은 서로 다른 회사의 모델 둘이지만 API 키가 없어 로컬 모델로 한다. 답변에 쓰는 qwen3.5:4b는 자기 답을
채점하는 셈이라 빼고, 다른 회사 모델인 gpt-oss(OpenAI 공개 가중치)로 확인한다. 어긋난 문항은 사람이 본다.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import httpx

from proviso.eval.schema import EvalItem

# 후보를 숫자로 부르면 조 번호나 금액과 헷갈려 답한다(처음 100개 중 38개). 그래서 알파벳으로 부른다
LABELS = "ABCDEFGHIJ"
PROMPT = ("다음은 한국 금융 법령 조문 후보와 질문이다. 질문에 답하려면 읽어야 하는 조문의 후보 기호(A, B, C …)를 모두 골라라.\n"
          "질문에 답하지 말고, 설명 없이 기호만 쉼표로 써라(예: B 또는 A, D). 답이 되는 조문이 없으면 없음.\n\n"
          "{cands}\n\n질문: {q}\n\n후보 기호:")


def read_pick(answer: str, n: int) -> set[int] | None:
    """모델 답 → 고른 후보 번호(1부터). 형식이 틀리면 None."""
    a = re.sub(r"\s", "", answer).strip("[]().").replace("후보", "")  # \u202f 같은 특수 공백도 지운다
    if a in ("없음", "0"):
        return set()
    if not re.fullmatch(r"[A-J](?:,[A-J])*", a):
        return None
    pick = {LABELS.index(x) + 1 for x in a.split(",")}
    return pick if max(pick) <= n else None


def ask(model: str, prompt: str, timeout: float = 600) -> str:
    r = httpx.post("http://localhost:11434/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "think": "low",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 16384, "num_predict": 1500},
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["message"]["content"]


def excerpt(text: str, keys: list[str], limit: int = 3000) -> str:
    """긴 조문에서 정답 근거가 있는 쪽을 보여 준다. 앞부분에 다 있거나 찾지 못하면 앞에서부터 자른다."""
    pos = [text.find(k) for k in keys]
    if len(text) <= limit or not pos or min(pos) < 0 or max(p + len(k) for p, k in zip(pos, keys)) <= limit:
        return text[:limit]
    start = max(0, min(pos) - limit // 2)
    return ("(앞 생략) " if start else "") + text[start:start + limit]


def candidates(it: EvalItem, ctx: list[dict], gold_texts: list[dict], seed: int = 0) -> tuple[list[dict], set[int]]:
    """검색 상위 조문 + 정답(빠졌으면 더함)을 섞는다. 시점 문항은 정답 버전 본문을 넣고 같은 조의 현행 버전은 뺀다."""
    gold_keys = {(g["doc_id"], g["article"]) for g in gold_texts}
    pool = [c for c in ctx if (c["doc_id"], c["article"]) not in gold_keys][:5]
    cand = gold_texts + pool
    random.Random(f"{seed}{it.id}").shuffle(cand)
    return cand, {i for i, c in enumerate(cand, 1) if (c["doc_id"], c["article"]) in gold_keys}


def verify(conn, items: list[EvalItem], contexts: dict, model: str, out: Path) -> list[dict]:
    done = {}
    if out.exists():
        done = {d["id"]: d for d in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
    titles = dict(conn.execute("SELECT doc_id, title FROM documents").fetchall())
    with open(out, "a", encoding="utf-8") as f:
        for it in items:
            if it.id in done or not it.answerable:
                continue
            gt = []
            for g in it.gold:
                row = conn.execute("""SELECT coalesce(t.search_text, t.text), t.article_title FROM article_spans s
                    JOIN article_texts t USING (text_id) WHERE s.doc_id=%s AND s.article_no=%s AND
                    (%s::date IS NOT NULL AND s.valid_from=%s OR %s::date IS NULL AND s.valid_from <= '2026-11-20'
                     AND (s.valid_to IS NULL OR '2026-11-20' < s.valid_to))""",
                                   (g.doc_id, g.article, g.valid_from, g.valid_from, g.valid_from)).fetchone()
                if row:
                    gt.append({"doc_id": g.doc_id, "article": g.article, "title": row[1], "text": excerpt(row[0], it.must_include)})
            cand, gold_idx = candidates(it, contexts[it.id]["arts"], gt)
            txt = "\n\n".join(f"[후보 {LABELS[i - 1]}] {titles[c['doc_id']]} {c['article']}({c['title'] or ''})\n{c['text']}" for i, c in enumerate(cand, 1))
            q = it.question + (f" (기준일 {it.as_of})" if it.as_of else "")
            t0 = time.time()
            ans = ask(model, PROMPT.format(cands=txt, q=q))
            pick = read_pick(ans, len(cand))
            rec = {"id": it.id, "answer": ans.strip()[-60:], "pick": None if pick is None else sorted(pick),
                   "gold_idx": sorted(gold_idx), "n": len(cand), "main_ok": bool(pick and gold_idx & pick),
                   "all_ok": bool(pick) and gold_idx <= pick, "seconds": round(time.time() - t0, 1)}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            done[it.id] = rec
    return list(done.values())
