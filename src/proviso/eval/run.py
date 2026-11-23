"""검색 평가 실행: 설정 파일 → 평가셋 문항마다 검색 → 채점 → runs/<시각>_<이름>.json.

결과 파일에는 문항별 순위를 다 남긴다. 그래야 나중에 다시 돌리지 않고도 칸별 점수·부트스트랩을 다시 계산할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from proviso.config import ROOT
from proviso.eval.metrics import METRICS, groups, item_values, mean, score_item, summarize
from proviso.eval.schema import EvalItem

RUNS_DIR = ROOT / "runs"


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for key in ("name", "retriever", "evalset"):
        if key not in cfg:
            raise ValueError(f"{path}: '{key}'가 없음")
    cfg.setdefault("split", "dev")
    cfg.setdefault("k", 20)
    cfg.setdefault("repeats", 1)
    cfg.setdefault("use_as_of", True)  # 시점 문항의 기준일을 검색기에 넘길지(True: 평가셋 정답 날짜, 날짜 추출이 완벽하다고 가정)
    if cfg["use_as_of"] not in (True, False, "question"):
        raise ValueError(f"{path}: use_as_of는 true, false, question 중 하나")  # question: 질문 글에서 읽은 날짜(7장)
    return cfg


def load_items(path: Path, split: str | None) -> list[EvalItem]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            it = EvalItem.model_validate(json.loads(line))
            if it.answerable and (split in (None, "all") or it.split == split):
                items.append(it)
    return items


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_state() -> dict[str, Any]:
    def run(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    return {"commit": run("rev-parse", "--short", "HEAD") or None, "dirty": bool(run("status", "--porcelain", "--", "src"))}


def latest_snapshot(conn) -> str | None:
    row = conn.execute("SELECT snapshot_id FROM snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _as_of(it: EvalItem, use_as_of: bool | str, today: date) -> date | None:
    if use_as_of == "question":
        from proviso.retrieve.when import question_date

        return question_date(it.question, today)
    return it.as_of if use_as_of else None


def run_items(retriever, items: list[EvalItem], *, k: int, today: date, repeats: int = 1,
              use_as_of: bool | str = True, period: dict | None = None, log=print) -> list[dict]:
    """문항마다 repeats번 검색해 순위와 지연을 남긴다. 상위 5개 결과는 첫 회만 남긴다(나빠진 문항 확인용)."""
    out = []
    for n, it in enumerate(items, start=1):
        rec: dict[str, Any] = {"id": it.id, "rank": [], "latency_ms": []}
        for r in range(repeats):
            t0 = time.perf_counter()
            hits = retriever.search(it.question, k=k, as_of=_as_of(it, use_as_of, today))
            rec["latency_ms"].append(round((time.perf_counter() - t0) * 1000, 2))
            s = score_item(it, hits, today, period)
            rec["rank"].append(s["rank"])
            if "rank_law" in s:
                rec.setdefault("rank_law", []).append(s["rank_law"])
            if r == 0:
                rec["top"] = [h.chunk_id for h in hits[:5]]
        out.append(rec)
        if n % 200 == 0:
            log(f"  {n}/{len(items)}")
    return out


def _avg_values(rec: dict) -> dict[str, float]:
    """반복 실행이면 문항별로 회차 평균을 낸 뒤 집계한다(부트스트랩은 문항 단위)."""
    vals = [item_values(r) for r in rec["rank"]]
    return {m: mean([v[m] for v in vals]) for m in METRICS}


def _percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def summarize_run(items: list[EvalItem], per_item: list[dict], n_boot: int = 1000) -> dict:
    by_id = {it.id: it for it in items}
    values = {rec["id"]: _avg_values(rec) for rec in per_item}
    summary: dict[str, Any] = {"overall": summarize(list(values.values()), n_boot=n_boot)}

    # 칸별
    cells: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for iid, v in values.items():
        for dim, label in groups(by_id[iid]).items():
            cells[dim][label].append(v)
    summary["by"] = {dim: {label: summarize(vs, n_boot=n_boot) for label, vs in sorted(labs.items())}
                     for dim, labs in cells.items()}

    # 위임 문항: 위임한 법 조문까지 함께 찾았는지
    both = [float(all(r is not None and r <= 10 for r in (rec["rank"][0], rec["rank_law"][0])))
            for rec in per_item if rec.get("rank_law")]
    if both:
        summary["cross_ref_both@10"] = mean(both)

    # 반복 실행이면 회차별 평균의 범위
    repeats = len(per_item[0]["rank"]) if per_item else 0
    if repeats > 1:
        per_rep = [mean([item_values(rec["rank"][r])["recall@10"] for rec in per_item]) for r in range(repeats)]
        summary["repeats"] = {"recall@10": {"mean": mean(per_rep), "min": min(per_rep), "max": max(per_rep)}}

    lat = [x for rec in per_item for x in rec["latency_ms"]]
    summary["latency_ms"] = {"p50": _percentile(lat, 0.5), "p95": _percentile(lat, 0.95)}
    return summary


def run_eval(cfg: dict[str, Any], conn, *, split: str | None = None, limit: int | None = None,
             category: str | None = None,
             runs_dir: Path = RUNS_DIR, log=print) -> Path:
    from proviso.retrieve import build_retriever

    split = split or cfg["split"]
    evalset = ROOT / cfg["evalset"] if not Path(cfg["evalset"]).is_absolute() else Path(cfg["evalset"])
    items = load_items(evalset, split)
    if category:
        items = [it for it in items if it.category in category.split(",")]
    if limit:
        items = items[:limit]
    retriever = build_retriever(cfg["retriever"], conn)
    today = date.today()
    log(f"{cfg['name']}: {split} {len(items)}문항, k={cfg['k']}, 반복 {cfg['repeats']}회")
    t0 = time.perf_counter()
    from proviso.retrieve.versions import period_starts

    # 시점 정답은 같은 본문 기간이면 맞다(7장). 그전에는 span 시작일이 같아야 했다. 현행만 찾는 검색기는 어차피 0이다.
    period = period_starts(conn) if any(it.as_of for it in items) else None
    per_item = run_items(retriever, items, k=cfg["k"], today=today, repeats=cfg["repeats"],
                         use_as_of=cfg["use_as_of"], period=period, log=log)
    created = datetime.now(timezone.utc)
    run = {
        "run_id": created.strftime("%Y%m%dT%H%M%S") + "_" + cfg["name"],
        "created_at": created.isoformat(),
        "config": cfg,
        "retriever": retriever.describe(),
        "snapshot_id": latest_snapshot(conn),
        "evalset": {"path": str(cfg["evalset"]), "hash": file_sha256(evalset), "split": split,
                    "n": len(items), "limit": limit, "category": category},
        "today": today.isoformat(),
        "git": git_state(),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "summary": summarize_run(items, per_item),
        "per_item": per_item,
    }
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run['run_id']}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def format_summary(run: dict) -> str:
    s = run["summary"]
    lines = [f"## {run['run_id']} ({run['evalset']['split']} {run['evalset']['n']}문항, 스냅샷 {run['snapshot_id']})", "",
             "| 지표 | 값 | 95% 구간 |", "|---|---:|---|"]
    for m in METRICS:
        v = s["overall"][m]
        lines.append(f"| {m} | {v['mean']:.3f} | [{v['ci'][0]:.3f}, {v['ci'][1]:.3f}] |")
    if "cross_ref_both@10" in s:
        lines.append(f"| 위임: 법·시행령 둘 다 @10 | {s['cross_ref_both@10']:.3f} | |")
    lines.append(f"| 지연 p50 / p95 | {s['latency_ms']['p50']:.1f} / {s['latency_ms']['p95']:.1f} ms | |")
    for dim in ("유형", "형태", "오타", "법령명", "단어 겹침"):
        lines += ["", f"**{dim}별 recall@10**", "", "| 구분 | n | recall@10 | 95% 구간 |", "|---|---:|---:|---|"]
        for label, v in s["by"].get(dim, {}).items():
            r = v["recall@10"]
            lines.append(f"| {label} | {v['n']} | {r['mean']:.3f} | [{r['ci'][0]:.3f}, {r['ci'][1]:.3f}] |")
    return "\n".join(lines)
