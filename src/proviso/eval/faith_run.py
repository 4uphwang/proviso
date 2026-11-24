"""충실도 채점 실행(13장). 에이전트 결과 파일의 답마다 본 조문을 다시 불러 판정자에게 묻는다.

거절한 답은 주장이 없으므로 채점하지 않는다. 결과: runs/<시각>_faith-<원래 실행 이름>-by-<판정자>.json
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from proviso.agent.loop import chat_client
from proviso.agent.run import RUNS_DIR, TODAY
from proviso.agent.tools import load_law
from proviso.answer.faith import format_sources, judge, shown_keys
from proviso.config import ROOT

EVALSET = ROOT / "evalset" / "agent.jsonl"


def questions(path: Path = EVALSET) -> dict[str, dict]:
    return {d["id"]: d for d in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def answered(run: dict, arm: str = "agent_asof") -> list[tuple[dict, dict]]:
    """(문항 기록, 그 팔의 결과) 중 거절하지 않은 답."""
    return [(it, it["arms"][arm]) for it in run["per_item"] if not it["arms"][arm]["refused"] and not it["arms"][arm].get("error")]


class Sources:
    """기준일마다 조문을 한 번만 불러 둔다."""

    def __init__(self, conn):
        self._conn = conn
        self._laws: dict[date, dict] = {}

    def text(self, a: dict) -> str:
        when = date.fromisoformat(a["as_of"]) if a.get("as_of") not in (None, "None") else TODAY
        if when not in self._laws:
            self._laws[when] = load_law(self._conn, when).articles
        return format_sources(shown_keys(a["steps"]), self._laws[when])


def run_faith(conn, run_path: Path, model: str, *, arm: str = "agent_asof", ids: set[str] | None = None,
              max_tokens: int = 2000, progress=print) -> Path:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    qs, src = questions(), Sources(conn)
    llm = chat_client(model, max_tokens)
    recs, cost = [], 0.0
    pairs = [(it, a) for it, a in answered(run, arm) if ids is None or it["id"] in ids]
    for n, (it, a) in enumerate(pairs, 1):
        f, c = judge(llm, qs[it["id"]]["question"], a["answer"], src.text(a))
        cost += c
        recs.append({"id": it["id"], "parsed": f.parsed, "n_claims": f.n, "score": f.score,
                     "unfaithful": f.unfaithful if f.parsed else None, "claims": f.claims})
        if n % 20 == 0:
            progress(f"{n}/{len(pairs)}", flush=True)
    tag = re.sub(r"[^A-Za-z0-9.]+", "_", model)
    out = RUNS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_faith-{run['run_id'][16:]}-by-{tag}.json"
    out.write_text(json.dumps({"run_id": out.stem, "kind": "faith", "of": run["run_id"], "answer_model": run["model"],
                               "judge": model, "split": run["split"], "cost": round(cost, 6), "per_item": recs},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def summary(faith: dict) -> dict:
    rs = [r for r in faith["per_item"] if r["parsed"] and r["n_claims"]]
    return {"answers": len(faith["per_item"]), "judged": len(rs),
            "unparsed": sum(not r["parsed"] for r in faith["per_item"]),
            "faithfulness": sum(r["score"] for r in rs) / len(rs) if rs else None,
            "unfaithful_rate": sum(r["unfaithful"] for r in rs) / len(rs) if rs else None}


def run_faith_batch(conn, run_paths: list[Path], model: str, *, arm: str = "agent_asof", max_tokens: int = 2000,
                    client=None, progress=print, every: float = 30.0,
                    ids: dict[str, set[str]] | None = None) -> tuple[list[Path], dict]:
    """19장: 여러 실행 결과의 답을 배치 하나로 맡겨 판정한다. 결과는 run_faith와 같은 모양으로 실행마다 쓴다.
    ids: {실행 run_id: 문항 id들}을 주면 그 문항만.
    반환: (판정 결과 파일들, 배치 요약 {batch_id, status, requests, seconds, cost, errors})."""
    from proviso.agent.batch import BatchClient, content_of
    from proviso.answer.faith import build_prompt, parse

    llm = chat_client(model, max_tokens)  # 요청 본문을 보통 호출과 똑같이 만들려고 쓴다(부르지는 않는다)
    qs, src = questions(), Sources(conn)
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in run_paths]
    reqs, where = [], {}
    for k, run in enumerate(runs):
        for it, a in answered(run, arm):
            if ids is not None and it["id"] not in ids.get(run["run_id"], set()):
                continue
            cid = f"{k}:{it['id']}"
            msgs = [{"role": "user", "content": build_prompt(qs[it["id"]]["question"], a["answer"], src.text(a))}]
            reqs.append((cid, llm.request_body(msgs, None)))
            where[cid] = (k, it["id"])
    client = client or BatchClient()
    batch_id = client.submit(llm.model, reqs)
    progress(f"배치 {batch_id}: 요청 {len(reqs)}개를 맡김", flush=True)
    res = client.wait(batch_id, every=every, progress=lambda m: progress(m, flush=True))
    per_run: list[list[dict]] = [[] for _ in runs]
    for cid, (k, iid) in where.items():
        if cid in res.bodies:
            f = parse(content_of(res.bodies[cid]))
            per_run[k].append({"id": iid, "parsed": f.parsed, "n_claims": f.n, "score": f.score,
                               "unfaithful": f.unfaithful if f.parsed else None, "claims": f.claims})
        else:
            per_run[k].append({"id": iid, "parsed": False, "n_claims": 0, "score": None, "unfaithful": None, "claims": [],
                               "error": res.errors.get(cid, "결과 없음")})
    tag = re.sub(r"[^A-Za-z0-9.]+", "_", model) + "-batch"
    now = datetime.now(timezone.utc)
    outs = []
    for run, recs in zip(runs, per_run):
        out = RUNS_DIR / f"{now:%Y%m%dT%H%M%S}_faith-{run['run_id'][16:]}-by-{tag}.json"
        out.write_text(json.dumps({"run_id": out.stem, "kind": "faith", "mode": "batch", "batch_id": batch_id,
                                   "of": run["run_id"], "answer_model": run["model"], "judge": model, "split": run["split"],
                                   "cost": None, "per_item": recs}, ensure_ascii=False, indent=1), encoding="utf-8")
        outs.append(out)
    info = {"batch_id": batch_id, "status": res.status, "requests": len(reqs), "results": len(res.bodies),
            "errors": len(res.errors), "seconds": res.seconds, "polls": res.polls, "usage": res.usage}
    (RUNS_DIR / f"{now:%Y%m%dT%H%M%S}_batch-{tag}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                                                      encoding="utf-8")
    return outs, info
