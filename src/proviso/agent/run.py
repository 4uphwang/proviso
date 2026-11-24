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
- agent_asof(7장): agent_seed에서 코드가 질문의 기준일을 읽어, 날짜가 있으면 도구가 모두 그날 시행 중이던 조문을 보여 주고
  모델에게 기준일을 알린다. 날짜가 없으면 agent_seed와 같다
- agent_asof_k<N>[_ends|_law](8장): agent_asof에서 검색 도구가 보여 주는 조문 수(N, 기본 5)와 배치 순서를 바꾼다.
  ends는 1위와 2위를 처음과 끝에 두고 낮은 순위를 가운데로, law는 법령·조 번호순
- agent_links: agent에서 검색 결과마다 이어진 조문 이름을 함께 보여 준다(12문항 시험에서 검색만 하고 끝내 추가)
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

from proviso.agent.resilience import LLMError
from proviso.agent.loop import FINISH, SYSTEM, chat_client, run_agent
from proviso.agent.tools import SPECS, Key, Law, Toolbox, focus, load_law
from proviso.answer.run import MAX_ARTICLE_CHARS, PROMPTS, build_contexts, context_text, judge, law_names, load_agent_items
from proviso.config import ROOT
from proviso.eval.run import RUNS_DIR, file_sha256, git_state, latest_snapshot
from proviso.eval.schema import EvalItem

TODAY = date(2026, 11, 20)
DEFAULT_MODEL = "qwen3.5:4b"
TOKEN_LIMIT = 11000  # 6장: Ollama num_ctx 16384에서 답 500토큰과 다음 도구 결과 자리를 뺀 값
FIXED = ("fixed", "focus", "follow", "focus_follow", "short", "short_follow")
AGENT = ("agent", "agent_seed", "agent_links", "agent_seed_follow", "agent_asof")
ASOF_ARM = re.compile(r"agent_asof(?:_k(\d+))?(?:_(ends|law))?$")


def assembly(arm: str) -> tuple[int, str] | None:
    """agent_asof 계열이면 (검색 결과 조문 수, 배치 순서)."""
    m = ASOF_ARM.match(arm)
    return (int(m.group(1) or 5), m.group(2) or "score") if m else None
ASOF_NOTE = ("\n기준일: {when}. 질문은 이 시점의 규정을 묻는다. 도구는 그날 시행 중이던 조문(현행과 다를 수 있다)을 보여 준다. "
             "답 첫 문장에 '{when} 시행 조문 기준'이라고 밝히고, 그 조문으로 답하라.{year_only}")
YEAR_ONLY = " 질문이 연도만 말해서 그해 7월 1일을 기준일로 잡았다. 그해 안에 조문이 바뀌었을 수 있다고 덧붙여라."
EXTRA_CHARS = 1500  # 더한 조문 하나의 상한
SHORT_CHARS = 1000  # 검색 도구가 조문 하나에 보여 주는 길이와 같다


class Retriever:
    """R1을 필요할 때만 올리고, 검색 결과를 파일에 저장한다(같은 질의는 다시 검색하지 않는다)."""

    def __init__(self, conn, cfg: dict, path: Path | None):
        """path가 None이면 파일에 저장하지 않고 이 프로세스 안에서만 기억한다(16장: 서버 지연을 잴 때)."""
        self._conn, self._cfg, self._path, self._r = conn, cfg, path, None
        self._cache = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}

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
        if self._path is None:
            return value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        return value

    def search(self, query: str, as_of: date | None = None, k: int = 5) -> list[Key]:
        """(문서, 조) 목록. 저장된 결과를 쓰면 걸린 시간은 0에 가깝다(지연 비교에는 seconds를 더한다).
        as_of가 있으면 그날 시행 중이던 본문에서 찾는다(7장, 본문 기간 색인이 있는 검색 설정이어야 한다)."""
        key = ("s:" if k == 5 else f"s{k}:") + query + (f"@{as_of}" if as_of else "")  # 5개는 6·7장 캐시 그대로
        if key not in self._cache:
            t0 = time.perf_counter()
            hits = self.r.search(query, k=k, as_of=as_of)[:k]
            self._save(key, {"keys": [[h.doc_id, h.article_no] for h in hits], "seconds": round(time.perf_counter() - t0, 3)})
        return [tuple(k) for k in self._cache[key]["keys"]]

    def seconds(self, query: str, as_of: date | None = None, k: int = 5) -> float:
        return self._cache.get(("s:" if k == 5 else f"s{k}:") + query + (f"@{as_of}" if as_of else ""), {}).get("seconds", 0.0)

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


def as_of_setup(conn, question: str, names, retr: Retriever, laws_at: dict, k: int = 5,
                order: str = "score") -> tuple[date | None, Law | None, Toolbox | None, str, str]:
    """agent_asof(7장): 질문의 기준일을 읽어 그날 조문으로 도구를 만들고 기준일 문구를 만든다.
    반환: (기준일, 법령, 도구, 시스템 지시, 기준일 문구). 날짜가 없으면 (None, None, None, SYSTEM, "")이라 부른 쪽이 현행 도구를 쓴다.
    18장: 기준일 문구는 시스템 지시에 붙이지 않고 질문 앞에 둔다. 시스템 지시가 늘 같아야 앞부분 캐시를 읽는다."""
    from proviso.retrieve.when import question_date

    when = question_date(question, TODAY)
    if not when:
        return None, None, None, SYSTEM, ""
    if when not in laws_at:
        laws_at[when] = load_law(conn, when)
    law = laws_at[when]
    box = Toolbox(law, names, lambda q: retr.search(q, when, k=k), k=k, order=order)
    note = ASOF_NOTE.format(when=f"{when.year}년 {when.month}월 {when.day}일",
                            year_only=YEAR_ONLY if year_only(question) else "").strip()
    return when, law, box, SYSTEM, note


def year_only(question: str) -> bool:
    return not re.search(r"\d+\s*월", question) and not re.search(r"\d{4}\s*[.\-/]\s*\d", question)


def year_notice(question: str, when: date | None, cites: list[dict], law: Law, hist) -> str | None:
    """연도만 말한 질문에서, 답이 인용한 조문(보여 준 것)이 그해 안에 바뀌었으면 코드가 붙이는 고지(7장).
    모델에게 지시해서는 거의 쓰지 않았다(8문항 중 1개)."""
    if not when or not year_only(question):
        return None
    keys = list(dict.fromkeys((c["doc"], c["article"]) for c in cites if c["status"] == "shown" and c["doc"]))
    changed = []
    for k in keys:
        days = hist.changes(k[0], k[1], date(when.year, 1, 1), date(when.year, 12, 31))
        if days and k in law.articles:
            changed.append(f"{law.short(k)}({', '.join(f'{d.month}월 {d.day}일' for d in days)} 시행)")
    if not changed:
        return None
    return (f"※ 질문이 연도만 말해 {when.year}년 {when.month}월 {when.day}일 시행 조문으로 답했습니다. "
            f"{', '.join(changed)}은(는) {when.year}년 중에 바뀌어, 그해 다른 때에는 내용이 다를 수 있습니다.")


def version_flag(it: EvalItem, shown: set[Key], law: Law, period: dict) -> bool | None:
    """시점 문항: 정답 조문을 정답과 같은 본문(본문 기간)으로 봤는가. 현행 본문만 보이면 늘 False다."""
    if not it.as_of or not it.answerable:
        return None
    for g in it.gold:
        k = (g.doc_id, g.article)
        if k not in shown or k not in law.articles:
            return False
        want = period.get((g.doc_id, g.article, g.valid_from))
        if want is None or want != period.get((g.doc_id, g.article, law.articles[k]["valid_from"])):
            return False
    return True


def run(conn, *, evalset: Path, split: str, arm: str, model: str, retriever_cfg: dict, limit: int | None = None,
        ids: set[str] | None = None, progress=print, token_limit: int = TOKEN_LIMIT, max_tokens: int = 500,
        repeat: int = 0, guard: bool = False) -> Path:
    """token_limit(9장): 대화가 이만큼 차면 도구를 그만 쓰고 답하게 한다. 기본값은 로컬 문맥 창(16K)에 맞춘 값이다."""
    asm = assembly(arm)
    if arm not in FIXED + AGENT and not asm:
        raise ValueError(f"모르는 설정: {arm!r}")
    items = [i for i in load_agent_items(evalset, split) if not ids or i.id in ids][:limit]
    law, names = load_law(conn, TODAY), law_names(conn)
    corpus = set(law.articles)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "agent_search.json")
    llm = chat_client(model, max_tokens, repeat)
    ctx_path = ROOT / "data" / "cache" / "contexts_rerank30_k5.json"
    contexts = json.loads(ctx_path.read_text(encoding="utf-8")) if ctx_path.exists() else {}
    if any(i.id not in contexts for i in items):
        contexts = build_contexts(conn, items, retr.r, k=5, path=ctx_path)
    from proviso.retrieve.query_llm import OllamaClient
    plain = OllamaClient(model, num_ctx=16384, timeout=600, num_predict=500, presence_penalty=0.0)  # 5장과 같은 설정
    k, order = asm or (5, "score")
    box = Toolbox(law, names, lambda q: retr.search(q, k=k), link_hints=arm == "agent_links", k=k, order=order)
    from proviso.retrieve.versions import period_starts

    period = period_starts(conn)
    from proviso.retrieve.versions import TextHistory

    hist = TextHistory(conn)
    laws_at: dict[date, Law] = {}
    recs = []
    failed = 0
    fixed = {it.id: fixed_arts(arm, it.question, contexts[it.id]["arts"], law, retr) for it in items} if arm in FIXED else {}
    if arm in FIXED:  # 자료를 다 고른 뒤에 답한다
        retr.free()
    from proviso.answer.guard import REFUSAL, screen

    for n, it in enumerate(items, 1):
        kind = screen(it.question) if guard else None
        when = None
        if kind:  # 거절 판별(answer/guard.py): 모델을 부르지 않고 정해진 문장으로 거절한다
            answer, ctx, shown = REFUSAL[kind], "", set()
            extra = {"seconds": 0.0, "llm_seconds": 0.0, "stop": "guard", "steps": [], "tools": [], "prompt_tokens": 0,
                     "output_tokens": 0, "cost": 0.0, "guard": kind}
        elif arm in FIXED:
            arts = fixed[it.id]
            ctx = context_text(arts)
            answer = plain.generate(PROMPTS["refuse"].format(context=ctx) + f"질문: {it.question}")
            shown = {(a["doc_id"], a["article"]) for a in arts}
            extra = {"seconds": plain.last_seconds + contexts[it.id]["seconds"], "llm_seconds": plain.last_seconds}
        else:
            when, use_law, use_box, system, note = (as_of_setup(conn, it.question, names, retr, laws_at, k, order) if asm
                                                    else (None, law, box, SYSTEM, ""))
            if when is None:
                use_law, use_box = law, box
            try:
                t = run_agent(it.question, llm, use_box, system=system, note=note, token_limit=token_limit,
                              seed_search=arm in ("agent_seed", "agent_seed_follow") or bool(asm),
                              seed_more=(lambda q, shown: box.attach(linked_top(q, shown, law, retr), q, EXTRA_CHARS))
                              if arm == "agent_seed_follow" else None)
            except LLMError as e:  # 14장: 평가 실행은 대체 모델을 쓰지 않고 실패로 남긴다(채점에서 빠진다)
                failed += 1
                recs.append({"id": it.id, "arms": {arm: {"answer": "", "refused": False, "cites": [], "error": str(e)[:300],
                                                        "attempts": getattr(e, "attempts", 1)}}})
                progress(f"{it.id} 모델 호출 실패: {str(e)[:120]}", flush=True)
                continue
            answer, ctx, shown = t.answer, t.context, set(t.shown)
            search_sec = sum(retr.seconds(s["args"]["query"], when, k) for s in t.steps
                             if s.get("tool") == "search" and not s["repeat"] and isinstance(s["args"], dict) and "query" in s["args"])
            llm_sec = sum(s["seconds"] for s in t.steps if s.get("llm"))
            extra = {"seconds": round(llm_sec + search_sec, 2), "llm_seconds": round(llm_sec, 2), "stop": t.stop, "steps": t.steps,
                     "tools": t.tool_calls, "prompt_tokens": t.prompt_tokens, "output_tokens": t.output_tokens,
                     "cost": round(t.cost, 6)}
        used = laws_at.get(when, law) if asm and when else law
        vf = version_flag(it, shown, used, period)
        a = judge(answer, shown, corpus | set(used.articles), ctx, names) | extra | flags(it, shown, ctx) \
            | ({"gold_version_in_context": vf} if vf is not None else {}) | ({"as_of": str(when)} if asm else {})
        if asm:
            a["year_notice"] = year_notice(it.question, when, a["cites"], used, hist)
        recs.append({"id": it.id, **flags(it, shown, ctx), "arms": {arm: a}})
        if n % 10 == 0:
            progress(f"{n}/{len(items)}", flush=True)
    tag = "" if model == DEFAULT_MODEL else "-" + re.sub(r"[^A-Za-z0-9.]+", "_", model)  # 9장: 기본 모델이 아니면 이름에 모델을 붙인다
    if token_limit != TOKEN_LIMIT:
        tag += f"-t{token_limit // 1000}k"
    if max_tokens != 500:
        tag += f"-o{max_tokens}"
    if repeat:
        tag += f"-r{repeat}"
    if guard:
        tag += "-g"
    path = RUNS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_agent-{arm}-{split}{tag}.json"
    path.write_text(json.dumps({"run_id": path.stem, "kind": "agent", "arm": arm, "model": model, "options": llm.options, "token_limit": token_limit, "guard": guard,
                                "split": split, "k": 5, "evalset": {"path": str(evalset), "hash": file_sha256(evalset)},
                                "retriever": retriever_cfg, "snapshot_id": latest_snapshot(conn), "git": git_state(),
                                "system": SYSTEM, "finish": FINISH, "tools": SPECS, "failed": failed, "per_item": recs}, ensure_ascii=False, default=list),
                    encoding="utf-8")
    return path


def merge(runs: list[dict]) -> dict:
    """설정 하나씩 든 결과 파일들을 한 파일처럼 합친다(문항이 같아야 한다)."""
    ids = [r["id"] for r in runs[0]["per_item"]]
    if any([r["id"] for r in x["per_item"]] != ids for x in runs):
        raise ValueError("문항이 다른 결과 파일은 비교할 수 없음")
    return {**runs[0], "per_item": [{**rows[0], "arms": {k: v for r in rows for k, v in r["arms"].items()}}
                                    for rows in zip(*(x["per_item"] for x in runs))]}
