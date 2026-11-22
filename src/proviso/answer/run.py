"""답변 생성(5장): 검색 결과(4장 R1 상위 k개 조문)를 자료로 넣고 로컬 LLM이 답한다.

문맥은 모든 설정에서 같다(한 번 검색해 파일로 저장). 바꾸는 것은 지시문과 사후 검증뿐이다.
- A0 plain: 자료와 질문만 준다(인용 강제 없음)
- A1 cite: 인용 형식 + "자료에 있는 조문만 인용" 지시
- A2 refuse: A1 + 근거가 없으면 정해진 문장으로 거절 지시
- A3 verify: A2 답을 코드로 검사해 인용이 없거나 유효한 인용이 하나도 없으면 거절로 바꾼다(LLM 호출 없음)
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from proviso.answer.cite import HALLUCINATED, REFUSAL, LawNames, classify, is_refusal, parse
from proviso.config import ROOT, load_sources
from proviso.eval.run import RUNS_DIR, file_sha256, git_state, latest_snapshot
from proviso.eval.schema import EvalItem

MAX_ARTICLE_CHARS = 3000  # 조문 하나당 본문 상한. 넘으면 앞부분만 넣고 (이하 생략)을 붙인다
DISCLAIMER = "※ 이 답변은 법률 자문이 아니며, 원문과 전문가의 확인이 필요합니다."

BASE = "{context}\n\n위 자료는 한국 금융 법령의 조문이다. 자료를 참고해 다음 질문에 한국어로 간결하게 답하라.\n"
CITE = ("답의 각 주장 뒤에 근거 조문을 [법령명 제N조] 형식으로 적어라. "
        "위 자료에 있는 조문만 인용하고, 자료에 없는 조문 번호는 쓰지 마라.\n")
REFUSE = ("자료에 질문의 답이 될 근거가 없거나, 질문이 개인의 특정 사안에 대한 판단이나 투자 조언을 요구하면 "
          f"다른 말 없이 다음 문장만 써라: {REFUSAL}\n")
PROMPTS = {"plain": BASE, "cite": BASE + CITE, "refuse": BASE + CITE + REFUSE}


def context_text(arts: list[dict]) -> str:
    return "\n\n".join(f"[{a['title_doc']} {a['article']}({a['title'] or ''}), 시행 {a['valid_from']}]\n{a['text']}"
                       for a in arts)


def load_agent_items(path: Path, split: str) -> list[EvalItem]:
    items = [EvalItem.model_validate(json.loads(x)) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return [i for i in items if split == "all" or i.split == split]


def fetch_arts(conn, hits, titles: dict[str, str]) -> list[dict]:
    """검색 결과 조문의 본문(버전 하나)을 가져온다. 긴 조문은 앞부분만 넣는다."""
    arts = []
    for h in hits:
        title, text = conn.execute("""SELECT t.article_title, coalesce(t.search_text, t.text) FROM article_spans s
            JOIN article_texts t USING (text_id) WHERE s.doc_id=%s AND s.article_no=%s AND s.valid_from=%s""",
                                   (h.doc_id, h.article_no, h.valid_from)).fetchone()
        cut = len(text) > MAX_ARTICLE_CHARS
        arts.append({"doc_id": h.doc_id, "title_doc": titles[h.doc_id], "article": h.article_no, "title": title,
                     "valid_from": str(h.valid_from), "cut": cut,
                     "text": text[:MAX_ARTICLE_CHARS] + (" (이하 생략)" if cut else "")})
    return arts


def build_contexts(conn, items: list[EvalItem], retriever, *, k: int, path: Path) -> dict[str, Any]:
    """문항마다 상위 k개 조문 전문. 이미 만든 문항은 건너뛴다(중간에 끊겨도 이어서)."""
    titles = dict(conn.execute("SELECT doc_id, title FROM documents").fetchall())
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for n, it in enumerate(items, 1):
        key = hashlib.sha256((it.question + str(it.as_of)).encode()).hexdigest()[:16]
        if old.get(it.id, {}).get("key") == key:
            continue
        t0 = time.perf_counter()
        arts = fetch_arts(conn, retriever.search(it.question, k=k, as_of=it.as_of)[:k], titles)
        old[it.id] = {"key": key, "arts": arts, "seconds": round(time.perf_counter() - t0, 2)}
        if n % 10 == 0:
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    return old


def law_names(conn) -> LawNames:
    pairs = [(t, d) for d, t in conn.execute("SELECT doc_id, title FROM documents").fetchall()]
    pairs += [(d, d) for d, in conn.execute("SELECT doc_id FROM documents").fetchall()]
    pairs += conn.execute("SELECT alias, doc_id FROM law_aliases").fetchall()
    parents = {s.id: s.parent for s in load_sources().laws if s.parent}
    return LawNames([p for p in pairs], parents)


def verify(answer: str, statuses: list[str]) -> str:
    """A3: 인용이 하나도 없거나, 유효한 인용(shown/mentioned)이 하나도 없으면 거절로 바꾼다."""
    if is_refusal(answer):
        return REFUSAL
    return answer if any(s not in HALLUCINATED for s in statuses) else REFUSAL


def judge(answer: str, shown, corpus, ctx: str, names: LawNames) -> dict:
    """답 하나의 인용 분류와 거절 여부. 저장된 답을 다시 채점할 때도 쓴다."""
    cs = parse(answer, names)
    return {"answer": answer, "refused": is_refusal(answer),
            "cites": [{"doc": c.doc_id, "outside": c.outside, "article": c.article,
                       "status": classify(c, shown, corpus, ctx, names)} for c in cs]}


def judge_arms(it: EvalItem, arts: list[dict], answers: dict[str, tuple[str, float]], corpus, names: LawNames) -> dict:
    """answers: arm → (모델 답, 초). verify는 refuse 답에 검증을 붙인 것이라 LLM 답을 따로 쓰지 않는다."""
    ctx = context_text(arts)
    shown = {(a["doc_id"], a["article"]) for a in arts}
    gold = {(g.doc_id, g.article) for g in it.gold}
    rec = {"id": it.id, "gold_in_context": bool(gold & shown), "all_gold_in_context": bool(gold) and gold <= shown,
           "ctx_chars": len(ctx), "arms": {}}
    flat = "".join(ctx.split())  # 긴 조문은 뒤가 잘려 정답 조문을 줬어도 답이 빠질 수 있다
    rec["answer_in_context"] = rec["gold_in_context"] and all("".join(m.split()) in flat for m in it.must_include)
    for arm, (ans, sec) in answers.items():
        r = judge(ans, shown, corpus, ctx, names)
        if arm == "verify":
            r = judge(verify(ans, [c["status"] for c in r["cites"]]), shown, corpus, ctx, names)
        rec["arms"][arm] = r | {"seconds": sec}
    return rec


def present(answer: str, cites: list[dict]) -> str:
    """사용자에게 보일 글: 답 + 자료에서 확인되지 않은 인용 표시 + 고지."""
    bad = list(dict.fromkeys(" ".join(x for x in (c["doc"] or c["outside"], c["article"]) if x)
                             for c in cites if c["status"] in HALLUCINATED))
    warn = [f"⚠ 자료에서 확인되지 않은 인용: {', '.join(bad)}"] if bad else []
    return "\n\n".join([answer.strip(), *warn, DISCLAIMER])


def ask(conn, question: str, *, retriever_cfg: dict, model: str, k: int) -> str:
    """질문 하나에 답한다. dev에서 고른 refuse 지시문을 쓴다."""
    from proviso.index.build import current_articles
    from proviso.retrieve import build_retriever
    from proviso.retrieve.query_llm import OllamaClient

    today = date(2026, 11, 20)
    titles = dict(conn.execute("SELECT doc_id, title FROM documents").fetchall())
    arts = fetch_arts(conn, build_retriever(retriever_cfg, conn).search(question, k=k, as_of=None)[:k], titles)
    ctx = context_text(arts)
    client = OllamaClient(model, num_ctx=16384, timeout=600, num_predict=500, presence_penalty=0.0)
    ans = client.generate(PROMPTS["refuse"].format(context=ctx) + f"질문: {question}")
    corpus = {(d, a["article_no"]) for d, xs in current_articles(conn, today).items() for a in xs}
    r = judge(ans, {(a["doc_id"], a["article"]) for a in arts}, corpus, ctx, law_names(conn))
    return present(r["answer"], r["cites"])


def run_answers(conn, items, contexts, *, arms: list[str], client, names: LawNames, corpus, progress=print) -> list[dict]:
    out = []
    for n, it in enumerate(items, 1):
        arts = contexts[it.id]["arts"]
        answers = {}
        for arm in arms:
            base = "refuse" if arm == "verify" else arm
            ans = client.generate(PROMPTS[base].format(context=context_text(arts)) + f"질문: {it.question}")
            answers[arm] = (ans, client.last_seconds)
        out.append(judge_arms(it, arts, answers, corpus, names))
        if n % 10 == 0:
            progress(f"{n}/{len(items)}", flush=True)
    return out


def rescore(conn, path: Path, items: dict[str, EvalItem]) -> dict:
    """저장된 답을 지금 채점기로 다시 채점해 같은 파일에 쓴다(LLM 호출 없음)."""
    from proviso.index.build import current_articles

    run = json.loads(path.read_text(encoding="utf-8"))
    name = run["run_id"].split("_answer-")[1].rsplit("-", 1)[0]
    contexts = json.loads((ROOT / "data" / "cache" / f"contexts_{name}_k{run['k']}.json").read_text(encoding="utf-8"))
    corpus = {(d, a["article_no"]) for d, arts in current_articles(conn, date(2026, 11, 20)).items() for a in arts}
    names = law_names(conn)
    for i, r in enumerate(run["per_item"]):
        raw = {arm: (r["arms"]["refuse" if arm == "verify" else arm]["answer"], a["seconds"]) for arm, a in r["arms"].items()}
        run["per_item"][i] = judge_arms(items[r["id"]], contexts[r["id"]]["arts"], raw, corpus, names)
    path.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    return run


def run(conn, *, evalset: Path, split: str, arms: list[str], model: str, k: int, retriever_cfg: dict, name: str,
        num_ctx: int = 16384, num_predict: int = 500, limit: int | None = None) -> Path:
    from proviso.index.build import current_articles
    from proviso.retrieve import build_retriever
    from proviso.retrieve.query_llm import OllamaClient

    items = load_agent_items(evalset, split)[:limit]
    ctx_path = ROOT / "data" / "cache" / f"contexts_{name}_k{k}.json"
    today = date(2026, 11, 20)
    done = json.loads(ctx_path.read_text(encoding="utf-8")) if ctx_path.exists() else {}
    retr = None
    if any(i.id not in done for i in items):
        retr = build_retriever(retriever_cfg, conn)
    contexts = build_contexts(conn, items, retr, k=k, path=ctx_path) if retr else done
    corpus = {(d, a["article_no"]) for d, arts in current_articles(conn, today).items() for a in arts}
    client = OllamaClient(model, num_ctx=num_ctx, timeout=600, num_predict=num_predict, presence_penalty=0.0)
    recs = run_answers(conn, items, contexts, arms=arms, client=client, names=law_names(conn), corpus=corpus)
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_answer-{name}-{split}"
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps({"run_id": run_id, "kind": "answer", "model": model, "options": client.options,
                                "split": split, "k": k, "evalset": {"path": str(evalset), "hash": file_sha256(evalset)},
                                "retriever": retriever_cfg, "snapshot_id": latest_snapshot(conn), "git": git_state(),
                                "prompts": PROMPTS, "per_item": recs}, ensure_ascii=False), encoding="utf-8")
    return path
