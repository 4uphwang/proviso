"""후속 질문 평가(evalset/followup.jsonl). 두 번째 턴 질문을 전략별로 혼자 읽히는 질문으로 바꾼 뒤 검색해 정답 조문을 찾는지 본다.

- 1턴 기록은 평가셋에 고정돼 있다(첫 질문 + 9장 기본 모델 DeepSeek의 실제 답). 그래서 전략끼리 같은 대화를 놓고 비교한다
- 정답: 검색 상위 k 안에 정답 조문이 있고, 질문에서 읽은 기준일(없으면 오늘)에 시행 중인 본문이 정답 버전이어야 한다.
  시점 후속 질문("2016년엔?")은 연도를 살려야 맞고, 다른 질문에 엉뚱한 연도를 붙이면 틀린다
- 종류: delegate(위임받은 시행령), penalty(어겼을 때 제재), temporal(옛 버전), same(같은 조의 다른 세부), switch(새 주제)
- 다시 쓰기의 경계 표시는 질문으로 정한다(같은 입력이면 저장된 응답을 다시 써서 요금이 들지 않게)
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from proviso.agent.followup import STRATEGIES, Turn, standalone
from proviso.agent.run import RUNS_DIR, TODAY, Retriever
from proviso.config import ROOT
from proviso.eval.metrics import bootstrap_ci, paired_diff_ci

KINDS = ("delegate", "penalty", "temporal", "same", "switch")


def load(path: Path, split: str | None) -> list[dict]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if split in (None, "all", r["split"])]


def hit(keys: list[tuple], when: date | None, gold: dict, spans) -> int | None:
    """정답 순위(1부터) 또는 None. 기준일에 시행 중인 버전이 정답 버전이어야 한다."""
    k = (gold["doc_id"], gold["article"])
    if k not in keys:
        return None
    span = spans.at(k[0], k[1], when or TODAY)
    if span is None or (gold.get("valid_from") and str(span[0]) != gold["valid_from"]):
        return None
    if not gold.get("valid_from") and not (span[1] is None or TODAY < span[1]):
        return None
    return keys.index(k) + 1


def run(conn, *, path: Path, split: str, strategies: list[str], retriever_cfg: dict, model: str,
        k: int = 10, progress=print) -> Path:
    from proviso.agent.loop import chat_client
    from proviso.retrieve.versions import SpanMap
    from proviso.retrieve.when import question_date

    items = load(path, split)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "followup_search.json")
    spans = SpanMap(conn)
    llm = chat_client(model, 300, per_minute=600) if "rewrite" in strategies else None
    per = []
    for n, it in enumerate(items, 1):
        turns = [Turn(t["question"], t["answer"], t.get("cites", [])) for t in it["turns"]]
        row = {"id": it["id"], "kind": it["kind"], "split": it["split"], "q": it["question"], "by": {}}
        for s in strategies:
            tag = "h" + hashlib.sha256(it["question"].encode()).hexdigest()[:8]
            q, info = standalone(it["question"], turns, s, llm, tag=tag)
            when = question_date(q, TODAY)
            keys = retr.search(q, when, k=k)
            row["by"][s] = {"query": q, "as_of": str(when) if when else None, "rank": hit(keys, when, it["gold"][0], spans),
                            "ok": info.get("ok", True)}
        per.append(row)
        if n % 10 == 0:
            progress(f"{n}/{len(items)}", flush=True)
    summary = summarize(per, strategies)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    out = RUNS_DIR / f"{stamp}_followup-{split}-{'+'.join(strategies)}.json"
    out.write_text(json.dumps({"split": split, "evalset": str(path), "retriever": retriever_cfg, "rewrite_model": model,
                               "k": k, "summary": summary, "per_item": per}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _at(rank, n: int) -> float:
    return 1.0 if rank is not None and rank <= n else 0.0


def summarize(per: list[dict], strategies: list[str]) -> dict:
    out = {}
    for s in strategies:
        out[s] = {}
        for kind in ("all",) + KINDS:
            rows = [r for r in per if kind == "all" or r["kind"] == kind]
            if not rows:
                continue
            out[s][kind] = {"n": len(rows)}
            for n in (1, 5, 10):
                v = [_at(r["by"][s]["rank"], n) for r in rows]
                lo, hi = bootstrap_ci(v)
                out[s][kind][f"recall@{n}"] = [round(sum(v) / len(v), 3), round(lo, 3), round(hi, 3)]
    base = strategies[0]
    for s in strategies[1:]:
        for n in (5, 10):
            a = [_at(r["by"][base]["rank"], n) for r in per]
            b = [_at(r["by"][s]["rank"], n) for r in per]
            out[s][f"diff@{n}_vs_{base}"] = [round(x, 3) for x in paired_diff_ci(a, b)]
    return out


def table(summary: dict) -> str:
    lines = ["| 전략 | 종류 | n | recall@1 | recall@5 | recall@10 |", "|---|---|---:|---:|---:|---:|"]
    for s, by in summary.items():
        for kind, v in by.items():
            if kind.startswith("diff"):
                continue
            lines.append(f"| {s} | {kind} | {v['n']} | " + " | ".join(f"{v[f'recall@{n}'][0]:.3f}" for n in (1, 5, 10)) + " |")
    for s, by in summary.items():
        for kind, v in by.items():
            if kind.startswith("diff"):
                lines.append(f"\n{s} {kind}: {v}")
    return "\n".join(lines)


__all__ = ["run", "load", "hit", "summarize", "table", "STRATEGIES"]
