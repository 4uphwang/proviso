"""6장 평가: 고정 파이프라인(자료를 코드가 고름)과 에이전트(자료를 모델이 고름)를 같은 문항으로 잰다.

- fixed: 5장 거절 설정 그대로(R1 상위 5개 조문, 앞 3,000자)
- focus: 긴 조문을 앞부분 대신 질문과 겹치는 항·호로 줄인다
- follow: 상위 3개 조문과 이어진(위임·참조) 조문 중 재정렬 점수 상위 3개를 더한다
- focus_follow: 둘 다
- short: focus를 에이전트의 검색 도구와 같은 1,000자로(점수 차이가 도구 덕인지 짧은 자료 덕인지 가르는 대조)
- short_follow: short + follow
- agent: 모델이 도구 4개를 골라 쓴다
- agent_seed: 질문 그대로 한 번 검색한 결과를 먼저 주고 시작한다
- agent_seed_follow: agent_seed의 첫 검색 결과에 follow처럼 이어진 조문 3개를 코드가 붙인다(6장 test 뒤 추가)
- agent_links: agent에서 검색 결과마다 이어진 조문 이름을 함께 보여 준다(12문항 시험에서 검색만 하고 끝내 추가)
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

from proviso.agent.loop import FINISH, SYSTEM, OllamaChat, run_agent
from proviso.agent.tools import SPECS, Key, Law, Toolbox, focus, load_law
from proviso.answer.run import MAX_ARTICLE_CHARS, PROMPTS, build_contexts, context_text, judge, law_names, load_agent_items
from proviso.config import ROOT
from proviso.eval.run import RUNS_DIR, file_sha256, git_state, latest_snapshot
from proviso.eval.schema import EvalItem

TODAY = date(2026, 11, 20)
FIXED = ("fixed", "focus", "follow", "focus_follow", "short", "short_follow")
AGENT = ("agent", "agent_seed", "agent_links", "agent_seed_follow")
EXTRA_CHARS = 1500  # 더한 조문 하나의 상한
SHORT_CHARS = 1000  # 검색 도구가 조문 하나에 보여 주는 길이와 같다


class Retriever:
    """R1을 필요할 때만 올리고, 검색 결과를 파일에 저장한다(같은 질의는 다시 검색하지 않는다)."""

    def __init__(self, conn, cfg: dict, path: Path):
        self._conn, self._cfg, self._path, self._r = conn, cfg, path, None
        self._cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    @property
    def r(self):
        if self._r is None:
            from proviso.retrieve import build_retriever
            self._r = build_retriever(self._cfg, self._conn)
        return self._r

    def free(self) -> None:
        """임베딩·재정렬 모델을 내려 답변 모델이 쓸 메모리를 비운다."""
        if self._r is None:
            return
        import gc

        import torch

        from proviso.retrieve.rerank import load_cross_encoder
        self._r = None
        load_cross_encoder.cache_clear()
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()

    def _save(self, key: str, value):
        self._cache[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        return value

    def search(self, query: str) -> list[Key]:
        """(문서, 조) 목록. 저장된 결과를 쓰면 걸린 시간은 0에 가깝다(지연 비교에는 seconds를 더한다)."""
        key = f"s:{query}"
        if key not in self._cache:
            t0 = time.perf_counter()
            hits = self.r.search(query, k=5, as_of=None)[:5]
            self._save(key, {"keys": [[h.doc_id, h.article_no] for h in hits], "seconds": round(time.perf_counter() - t0, 3)})
        return [tuple(k) for k in self._cache[key]["keys"]]

    def seconds(self, query: str) -> float:
        return self._cache.get(f"s:{query}", {}).get("seconds", 0.0)

    def rank(self, question: str, keys: list[Key], law: Law) -> list[Key]:
        """이어진 조문을 cross-encoder 점수순으로."""
        if not keys:
            return []
        ck = f"r:{question}:" + ",".join("/".join(k) for k in keys)
        if ck not in self._cache:
            pairs, owner = [], []
            for i, k in enumerate(keys):
                for t in self.r._texts.get((*k, str(law.articles[k]["valid_from"])), []):
                    pairs.append((question, t))
                    owner.append(i)
            best = [float("-inf")] * len(keys)
            for i, s in zip(owner, self.r._scorer(pairs, batch_size=16) if pairs else []):
                best[i] = max(best[i], float(s))
            self._save(ck, [list(keys[i]) for i in sorted(range(len(keys)), key=lambda i: (-best[i], i))])
        return [tuple(k) for k in self._cache[ck]]


def fixed_arts(arm: str, question: str, base: list[dict], law: Law, retr: Retriever) -> list[dict]:
    """고정 파이프라인의 자료. base: 5장에서 고정한 상위 5개."""
    arts = [dict(a) for a in base]
    if arm in ("focus", "focus_follow"):
        for a in arts:
            if a["cut"]:
                a["text"] = focus(law.articles[(a["doc_id"], a["article"])]["text"], question, MAX_ARTICLE_CHARS)
    if arm in ("short", "short_follow"):
        for a in arts:
            full = law.articles[(a["doc_id"], a["article"])]["text"]
            a["text"], a["cut"] = focus(full, question, SHORT_CHARS), len(full) > SHORT_CHARS
    if arm in ("follow", "focus_follow", "short_follow"):
        extra = SHORT_CHARS if arm == "short_follow" else EXTRA_CHARS
        have = {(a["doc_id"], a["article"]) for a in base}
        cand = [k for a in base[:3] for _, k in law.linked((a["doc_id"], a["article"])) if k not in have]
        for k in retr.rank(question, list(dict.fromkeys(cand)), law)[:3]:
            x = law.articles[k]
            arts.append({"doc_id": k[0], "title_doc": x["doc_title"], "article": k[1], "title": x["title"],
                         "valid_from": str(x["valid_from"]), "cut": len(x["text"]) > extra,
                         "text": focus(x["text"], question, extra)})
    return arts


def linked_top(question: str, shown: list[Key], law: Law, retr: Retriever, n: int = 3) -> list[Key]:
    """상위 3개 조문과 이어진 조문 중 재정렬 점수 상위 n개(follow와 같은 규칙)."""
    cand = [k for s in shown[:3] for _, k in law.linked(s) if k not in shown]
    return retr.rank(question, list(dict.fromkeys(cand)), law)[:n]


def flags(it: EvalItem, shown: set[Key], ctx: str) -> dict:
    gold = {(g.doc_id, g.article) for g in it.gold}
    flat = "".join(ctx.split())
    some = bool(gold & shown)
    return {"gold_in_context": some, "all_gold_in_context": bool(gold) and gold <= shown, "ctx_chars": len(ctx),
            "answer_in_context": some and all("".join(m.split()) in flat for m in it.must_include)}


def run(conn, *, evalset: Path, split: str, arm: str, model: str, retriever_cfg: dict, limit: int | None = None,
        ids: set[str] | None = None, progress=print) -> Path:
    if arm not in FIXED + AGENT:
        raise ValueError(f"모르는 설정: {arm!r}")
    items = [i for i in load_agent_items(evalset, split) if not ids or i.id in ids][:limit]
    law, names = load_law(conn, TODAY), law_names(conn)
    corpus = set(law.articles)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "agent_search.json")
    llm = OllamaChat(model)
    ctx_path = ROOT / "data" / "cache" / "contexts_rerank30_k5.json"
    contexts = json.loads(ctx_path.read_text(encoding="utf-8")) if ctx_path.exists() else {}
    if any(i.id not in contexts for i in items):
        contexts = build_contexts(conn, items, retr.r, k=5, path=ctx_path)
    from proviso.retrieve.query_llm import OllamaClient
    plain = OllamaClient(model, num_ctx=16384, timeout=600, num_predict=500, presence_penalty=0.0)  # 5장과 같은 설정
    box = Toolbox(law, names, retr.search, link_hints=arm == "agent_links")
    recs = []
    fixed = {it.id: fixed_arts(arm, it.question, contexts[it.id]["arts"], law, retr) for it in items} if arm in FIXED else {}
    if arm in FIXED:  # 자료를 다 고른 뒤에 답한다
        retr.free()
    for n, it in enumerate(items, 1):
        if arm in FIXED:
            arts = fixed[it.id]
            ctx = context_text(arts)
            answer = plain.generate(PROMPTS["refuse"].format(context=ctx) + f"질문: {it.question}")
            shown = {(a["doc_id"], a["article"]) for a in arts}
            extra = {"seconds": plain.last_seconds + contexts[it.id]["seconds"], "llm_seconds": plain.last_seconds}
        else:
            t = run_agent(it.question, llm, box, seed_search=arm in ("agent_seed", "agent_seed_follow"),
                          seed_more=(lambda q, shown: box.attach(linked_top(q, shown, law, retr), q, EXTRA_CHARS))
                          if arm == "agent_seed_follow" else None)
            answer, ctx, shown = t.answer, t.context, set(t.shown)
            search_sec = sum(retr.seconds(s["args"]["query"]) for s in t.steps
                             if s.get("tool") == "search" and not s["repeat"] and isinstance(s["args"], dict) and "query" in s["args"])
            llm_sec = sum(s["seconds"] for s in t.steps if s.get("llm"))
            extra = {"seconds": round(llm_sec + search_sec, 2), "llm_seconds": round(llm_sec, 2), "stop": t.stop, "steps": t.steps,
                     "tools": t.tool_calls, "prompt_tokens": t.prompt_tokens, "output_tokens": t.output_tokens}
        a = judge(answer, shown, corpus, ctx, names) | extra | flags(it, shown, ctx)
        recs.append({"id": it.id, **flags(it, shown, ctx), "arms": {arm: a}})
        if n % 10 == 0:
            progress(f"{n}/{len(items)}", flush=True)
    path = RUNS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_agent-{arm}-{split}.json"
    path.write_text(json.dumps({"run_id": path.stem, "kind": "agent", "arm": arm, "model": model, "options": llm.options,
                                "split": split, "k": 5, "evalset": {"path": str(evalset), "hash": file_sha256(evalset)},
                                "retriever": retriever_cfg, "snapshot_id": latest_snapshot(conn), "git": git_state(),
                                "system": SYSTEM, "finish": FINISH, "tools": SPECS, "per_item": recs}, ensure_ascii=False, default=list),
                    encoding="utf-8")
    return path


def merge(runs: list[dict]) -> dict:
    """설정 하나씩 든 결과 파일들을 한 파일처럼 합친다(문항이 같아야 한다)."""
    ids = [r["id"] for r in runs[0]["per_item"]]
    if any([r["id"] for r in x["per_item"]] != ids for x in runs):
        raise ValueError("문항이 다른 결과 파일은 비교할 수 없음")
    return {**runs[0], "per_item": [{**rows[0], "arms": {k: v for r in rows for k, v in r["arms"].items()}}
                                    for rows in zip(*(x["per_item"] for x in runs))]}
