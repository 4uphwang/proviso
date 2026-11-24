"""답변 채점(5장). 시점(temporal) 문항은 따로 센다: 정답 버전을 봤는지, 인용, 핵심어, 답에 시점을 밝혔는지(7장)."""

from __future__ import annotations

import re
from typing import Any, Callable

from proviso.answer.cite import HALLUCINATED
from proviso.eval.metrics import bootstrap_ci, mean, paired_diff_ci
from proviso.eval.schema import EvalItem


# 8장: 핵심 내용이 아닌 형식 문구 핵심어는 채점에서 뺀다(평가셋 문구만 보고 정한 규칙).
BOILERPLATE = re.compile(r"다음\s*각\s*(?:호|목)|어느\s*하나에\s*해당|고시하는\s*것")


def keywords(must_include: list[str]) -> list[str]:
    return [m for m in must_include if not BOILERPLATE.search(m)]


def keyword_hit(keyword: str, answer: str) -> bool:
    """답에 핵심어가 있는가. 공백과 굵은 글씨 표시(*, _, `)는 무시한다(6장).
    8장: "투자매매업ㆍ투자중개업"처럼 ㆍ로 묶은 핵심어는 묶인 말이 모두 있으면 있다고 본다(답이 "및"이나 쉼표로 이어도)."""
    flat = re.sub(r"[\s*_`]", "", answer)
    parts = [re.sub(r"\s", "", x) for x in re.split(r"[ㆍ·‧]", keyword)]
    return all(x in flat for x in parts if x)


def item_scores(it: EvalItem, rec: dict, a: dict) -> dict[str, float]:
    """문항 하나의 값. 해당 없는 값은 넣지 않는다. 14장: 모델 호출이 실패한 문항은 값이 없다(거절로 세지 않는다)."""
    out: dict[str, float] = {}
    if a.get("error"):
        return out
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
        # 7장: 현행 본문만 보여 준 설정은 기록이 없고, 정답 본문은 현행과 모두 달라 0이다
        out["정답 버전을 봄"] = float(bool(a.get("gold_version_in_context", False)))
        golds = {(g.doc_id, g.article) for g in it.gold}
        got = {(c["doc"], c["article"]) for c in a["cites"] if c["doc"]}
        out["시점 인용 재현율"] = float(len(golds & got) / len(golds))
        if it.must_include:
            if keywords(it.must_include):
                out["시점 핵심어 포함률"] = mean([float(keyword_hit(m, a["answer"])) for m in keywords(it.must_include)])
        years = set(re.findall(r"(?:19|20)\d{2}", it.question))
        if years and not a["refused"]:
            out["답에 시점을 밝힘"] = float(any(y in a["answer"] for y in years))
    else:
        out["과잉 거절"] = float(a["refused"])
        if a.get("answer_in_context", rec["answer_in_context"]):
            out["과잉 거절(답이 자료에 있을 때)"] = float(a["refused"])
        golds = {(g.doc_id, g.article) for g in it.gold}
        got = {(c["doc"], c["article"]) for c in a["cites"] if c["doc"]}
        out["인용 재현율"] = float(len(golds & got) / len(golds))
        if got:
            out["인용 정밀도"] = len(golds & got) / len(got)
        if keywords(it.must_include):
            out["핵심어 포함률"] = mean([float(keyword_hit(m, a["answer"])) for m in keywords(it.must_include)])
            if a.get("answer_in_context", rec["answer_in_context"]):
                out["핵심어 포함률(답이 자료에 있을 때)"] = out["핵심어 포함률"]
    return out


METRICS = ["정답 조문을 모두 봄", "답이 자료에 있음", "환각 인용 있는 답", "환각 인용률(인용 단위 평균)", "인용 없는 답", "거절 정확도", "과잉 거절",
           "과잉 거절(답이 자료에 있을 때)", "인용 재현율", "인용 정밀도", "핵심어 포함률", "핵심어 포함률(답이 자료에 있을 때)",
           "법령 이름 없는 인용", "시점 문항 거절", "정답 버전을 봄", "시점 인용 재현율", "시점 핵심어 포함률", "답에 시점을 밝힘"]


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
