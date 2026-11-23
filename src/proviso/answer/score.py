"""답변 채점(5장). 시점(temporal) 문항은 현행만 색인한 5장에서는 올바른 자료를 줄 수 없어 따로 센다(7장)."""

from __future__ import annotations

import re
from typing import Any, Callable

from proviso.answer.cite import HALLUCINATED
from proviso.eval.metrics import bootstrap_ci, mean, paired_diff_ci
from proviso.eval.schema import EvalItem


def item_scores(it: EvalItem, rec: dict, a: dict) -> dict[str, float]:
    """문항 하나의 값. 해당 없는 값은 넣지 않는다."""
    out: dict[str, float] = {}
    st = [c["status"] for c in a["cites"]]
    if "all_gold_in_context" in a and it.answerable and it.category != "temporal":  # 6장: 설정마다 자료가 다르다
        out["정답 조문을 모두 봄"] = float(a["all_gold_in_context"])
        out["답이 자료에 있음"] = float(a["answer_in_context"])
    out["환각 인용 있는 답"] = float(any(s in HALLUCINATED for s in st))  # 거절처럼 시작해도 지어낸 조문을 쓰면 센다
    if not a["refused"]:
        out["인용 없는 답"] = float(not st)
    if st:
        out["환각 인용률(인용 단위 평균)"] = mean([float(s in HALLUCINATED) for s in st])
        out["법령 이름 없는 인용"] = mean([float(c["doc"] is None and c["outside"] is None) for c in a["cites"]])
    if not it.answerable:
        out["거절 정확도"] = float(a["refused"])
    elif it.category == "temporal":
        out["시점 문항 거절"] = float(a["refused"])
    else:
        out["과잉 거절"] = float(a["refused"])
        if a.get("answer_in_context", rec["answer_in_context"]):
            out["과잉 거절(답이 자료에 있을 때)"] = float(a["refused"])
        golds = {(g.doc_id, g.article) for g in it.gold}
        got = {(c["doc"], c["article"]) for c in a["cites"] if c["doc"]}
        out["인용 재현율"] = float(len(golds & got) / len(golds))
        if got:
            out["인용 정밀도"] = len(golds & got) / len(got)
        if it.must_include:
            flat = re.sub(r"[\s*_`]", "", a["answer"])  # 굵은 글씨 표시(**)가 핵심어 사이에 끼면 못 찾는다(6장에서 고침)
            out["핵심어 포함률"] = mean([float(re.sub(r"\s", "", m) in flat) for m in it.must_include])
            if a.get("answer_in_context", rec["answer_in_context"]):
                out["핵심어 포함률(답이 자료에 있을 때)"] = out["핵심어 포함률"]
    return out


METRICS = ["정답 조문을 모두 봄", "답이 자료에 있음", "환각 인용 있는 답", "환각 인용률(인용 단위 평균)", "인용 없는 답", "거절 정확도", "과잉 거절",
           "과잉 거절(답이 자료에 있을 때)", "인용 재현율", "인용 정밀도", "핵심어 포함률", "핵심어 포함률(답이 자료에 있을 때)",
           "법령 이름 없는 인용", "시점 문항 거절"]


def report(run: dict, items: dict[str, EvalItem], arms: list[str], pred: Callable[[EvalItem], bool] = lambda i: True,
           base: str | None = None) -> str:
    recs = [r for r in run["per_item"] if pred(items[r["id"]]) and r["id"] in items]
    val = {arm: [item_scores(items[r["id"]], r, r["arms"][arm]) for r in recs] for arm in arms}
    lines = ["| 지표 | " + " | ".join(arms) + " |", "|---|" + "---:|" * len(arms)]
    for m in METRICS:
        cells = []
        for arm in arms:
            xs = [v[m] for v in val[arm] if m in v]
            cells.append(f"{mean(xs):.3f} (n={len(xs)})" if xs else "-")
        lines.append(f"| {m} | " + " | ".join(cells) + " |")
    lines += ["", "짝지은 차이(같은 문항에서 둘 다 값이 있는 것만):"]
    for a, b in zip(arms, arms[1:]):
        for m in METRICS:
            both = [(x[m], y[m]) for x, y in zip(val[a], val[b]) if m in x and m in y]
            if not both:
                continue
            d, lo, hi = paired_diff_ci([x for x, _ in both], [y for _, y in both])
            lines.append(f"- {a} → {b} {m}: {d:+.3f} [{lo:+.3f}, {hi:+.3f}] (n={len(both)})")
    return "\n".join(lines)
