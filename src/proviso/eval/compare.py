"""두 실행 결과 비교: 지표 차이(짝지은 부트스트랩 95% 구간) + 칸별 차이 + 나빠진 문항 목록(md).

같은 문항 세트(해시·split·문항 id)일 때만 비교한다. 데이터 스냅샷이 다르면 법령이 바뀌어 정답 버전이 달라질 수
있으므로 기본은 거부하고, --allow-snapshot-mismatch로만 비교한다(표에 경고를 남긴다).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from proviso.eval.metrics import METRICS, groups, item_values, mean, paired_diff_ci
from proviso.eval.run import load_items
from proviso.config import ROOT


class CompareError(ValueError):
    pass


def _vals(rec: dict) -> dict[str, float]:
    vs = [item_values(r) for r in rec["rank"]]
    return {m: mean([v[m] for v in vs]) for m in METRICS}


def _first_rank(rec: dict):
    return rec["rank"][0]


def compare_runs(a: dict, b: dict, *, allow_snapshot_mismatch: bool = False, n_boot: int = 1000,
                 max_items: int = 30) -> str:
    ea, eb = a["evalset"], b["evalset"]
    if ea["hash"] != eb["hash"]:
        raise CompareError("평가셋 파일이 다름(해시 불일치). 같은 평가셋으로 다시 돌려야 비교할 수 있음")
    if ea["split"] != eb["split"]:
        raise CompareError(f"split이 다름: {ea['split']} vs {eb['split']}")
    ids_a = [r["id"] for r in a["per_item"]]
    if ids_a != [r["id"] for r in b["per_item"]]:
        raise CompareError("문항 목록이 다름(--limit 등)")
    warn = []
    if a["snapshot_id"] != b["snapshot_id"]:
        if not allow_snapshot_mismatch:
            raise CompareError(f"데이터 스냅샷이 다름: {a['snapshot_id']} vs {b['snapshot_id']} "
                               "(--allow-snapshot-mismatch로 비교 가능)")
        warn.append(f"> ⚠ 데이터 스냅샷이 다름: A {a['snapshot_id']} / B {b['snapshot_id']}")

    va = {r["id"]: _vals(r) for r in a["per_item"]}
    vb = {r["id"]: _vals(r) for r in b["per_item"]}
    lines = [f"# 비교: A `{a['run_id']}` → B `{b['run_id']}`", "",
             f"{ea['split']} {len(ids_a)}문항, 평가셋 {ea['hash'][:19]}…, 스냅샷 {b['snapshot_id']}", *warn, "",
             "| 지표 | A | B | B − A | 95% 구간 | 판단 |", "|---|---:|---:|---:|---|---|"]
    for m in METRICS:
        xa = [va[i][m] for i in ids_a]
        xb = [vb[i][m] for i in ids_a]
        d, lo, hi = paired_diff_ci(xa, xb, n=n_boot)
        verdict = "좋아짐" if lo > 0 else "나빠짐" if hi < 0 else "차이 불분명"
        lines.append(f"| {m} | {mean(xa):.3f} | {mean(xb):.3f} | {d:+.3f} | [{lo:+.3f}, {hi:+.3f}] | {verdict} |")
    la, lb = a["summary"]["latency_ms"], b["summary"]["latency_ms"]
    lines.append(f"| 지연 p50 (ms) | {la['p50']:.1f} | {lb['p50']:.1f} | {lb['p50'] - la['p50']:+.1f} | | |")

    # 칸별 recall@10 차이
    evalset = Path(a["evalset"]["path"])
    items = {it.id: it for it in load_items(evalset if evalset.is_absolute() else ROOT / evalset, None)}
    cells: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for i in ids_a:
        if i in items:
            for dim, label in groups(items[i]).items():
                cells[dim][label].append(i)
    for dim in ("유형", "형태", "오타", "법령명", "단어 겹침"):
        lines += ["", f"**{dim}별 recall@10**", "", "| 구분 | n | A | B | B − A | 95% 구간 |", "|---|---:|---:|---:|---:|---|"]
        for label, ids in sorted(cells[dim].items()):
            xa = [va[i]["recall@10"] for i in ids]
            xb = [vb[i]["recall@10"] for i in ids]
            d, lo, hi = paired_diff_ci(xa, xb, n=n_boot)
            lines.append(f"| {label} | {len(ids)} | {mean(xa):.3f} | {mean(xb):.3f} | {d:+.3f} | [{lo:+.3f}, {hi:+.3f}] |")

    # 문항별 변화: 상위 10 안 ↔ 밖
    ra = {r["id"]: r for r in a["per_item"]}
    rb = {r["id"]: r for r in b["per_item"]}
    worse = [i for i in ids_a if va[i]["recall@10"] > vb[i]["recall@10"]]
    better = [i for i in ids_a if va[i]["recall@10"] < vb[i]["recall@10"]]
    lines += ["", f"상위 10 안에 들게 된 문항 {len(better)}개, 밖으로 밀려난 문항 {len(worse)}개", ""]
    if worse:
        lines += [f"**밀려난 문항** (최대 {max_items}개)", "", "| id | 질문 | A 순위 | B 순위 |", "|---|---|---:|---:|"]
        for i in worse[:max_items]:
            q = items[i].question if i in items else ""
            lines.append(f"| {i} | {q} | {_first_rank(ra[i]) or '-'} | {_first_rank(rb[i]) or '-'} |")
    return "\n".join(lines) + "\n"


def compare_files(path_a: Path, path_b: Path, **kw) -> str:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    return compare_runs(a, b, **kw)
