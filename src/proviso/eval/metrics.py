"""검색 평가 채점: 정답 순위 → recall@k·MRR → 칸별 집계 → 부트스트랩 신뢰구간.

채점 규칙(docs/plan.md §7.2)
- 검색 결과를 같은 조문 버전(문서, 조, 유효 시작일) 단위로 묶어 처음 나온 순위만 남긴다.
  고정 길이 청킹처럼 한 조문이 여러 조각으로 나와도 상위권을 독차지하지 않게 하려는 것.
- 정답과 같다고 보는 기준: 문서·조 번호가 같고, 버전이 맞아야 한다.
  - 시점 문항(gold.valid_from 있음): 그 버전이어야 한다. 같은 조라도 다른 시점 버전이면 오답.
  - 나머지: 평가를 돌린 날(today)에 유효한 버전이어야 한다. 옛 버전을 가져오면 오답.
- 주 정답은 gold[0]. 위임 문항(cross_ref)은 gold[0]이 시행령 조문이고, gold[1](위임한 법 조문)은
  rank_law로 따로 기록한다.
- 정의 문항의 항·호는 아직 채점하지 않는다(조 단위). 조각이 조 단위라서 항·호까지 가릴 수 없다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from proviso.eval.schema import EvalItem, Gold

KS = (1, 5, 10, 20)
MRR_K = 10
METRICS = [f"recall@{k}" for k in KS] + [f"mrr@{MRR_K}"]


@dataclass(frozen=True)
class Hit:
    """검색 결과 한 건. 같은 조문 버전에서 조각이 여러 개 나와도 된다(채점할 때 묶는다)."""

    chunk_id: str
    doc_id: str
    article_no: str
    valid_from: date
    valid_to: date | None = None
    score: float = 0.0


def dedupe(hits: list[Hit]) -> list[Hit]:
    seen: set[tuple] = set()
    out = []
    for h in hits:
        key = (h.doc_id, h.article_no, h.valid_from)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _covers(h: Hit, d: date) -> bool:
    return h.valid_from <= d and (h.valid_to is None or d < h.valid_to)


def matches(g: Gold, h: Hit, today: date) -> bool:
    if g.doc_id != h.doc_id or g.article != h.article_no:
        return False
    if g.valid_from is not None:
        return h.valid_from == g.valid_from
    return _covers(h, today)


def _rank(g: Gold, hits: list[Hit], today: date) -> int | None:
    for i, h in enumerate(hits, start=1):
        if matches(g, h, today):
            return i
    return None


def score_item(item: EvalItem, hits: list[Hit], today: date) -> dict:
    """문항 하나의 정답 순위. 순위는 같은 조문 버전을 묶은 뒤의 순위(1부터), 못 찾으면 None."""
    arts = dedupe(hits)
    out = {"rank": _rank(item.gold[0], arts, today)}
    if item.category == "cross_ref" and len(item.gold) > 1:
        out["rank_law"] = _rank(item.gold[1], arts, today)
    return out


def item_values(rank: int | None) -> dict[str, float]:
    return {
        **{f"recall@{k}": float(rank is not None and rank <= k) for k in KS},
        f"mrr@{MRR_K}": (1.0 / rank) if rank is not None and rank <= MRR_K else 0.0,
    }


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def bootstrap_ci(values: list[float], n: int = 1000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    """문항 단위 부트스트랩: 문항을 중복 허용으로 다시 뽑아 평균을 n번 구하고 양끝 alpha/2를 자른다."""
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    m = len(values)
    means = sorted(sum(values[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return (means[int(n * alpha / 2)], means[min(n - 1, int(n * (1 - alpha / 2)))])


def paired_diff_ci(a: list[float], b: list[float], n: int = 1000, seed: int = 0,
                   alpha: float = 0.05) -> tuple[float, float, float]:
    """짝지은 부트스트랩: 같은 문항 묶음으로 (b − a) 평균 차이를 n번 구한다. 반환값: (차이, 하한, 상한)."""
    if len(a) != len(b):
        raise ValueError("짝지은 비교는 두 결과의 문항이 같아야 함")
    diffs = [y - x for x, y in zip(a, b)]
    lo, hi = bootstrap_ci(diffs, n=n, seed=seed, alpha=alpha)
    return (mean(diffs), lo, hi)


def overlap_bucket(x: float | None) -> str:
    if x is None:
        return "없음"
    for edge, name in ((0.3, "<0.3"), (0.5, "0.3–0.5"), (0.7, "0.5–0.7")):
        if x < edge:
            return name
    return "≥0.7"


def groups(item: EvalItem) -> dict[str, str]:
    """칸별 집계에 쓰는 구분. 별표 문항은 category가 lookup이지만 따로 본다."""
    return {
        "유형": "appendix" if "별표" in item.tags else item.category,
        "형태": item.style or "없음",
        "오타": "오타" if "오타" in item.tags else "정상",
        "법령명": "있음" if "법령명" in item.tags else "없음",
        "법령": item.gold[0].doc_id if item.gold else "없음",
        "단어 겹침": overlap_bucket(item.lexical_overlap),
    }


def summarize(per_item_values: list[dict[str, float]], n_boot: int = 1000, seed: int = 0) -> dict:
    """지표마다 평균과 95% 신뢰구간."""
    out = {"n": len(per_item_values)}
    for m in METRICS:
        vals = [v[m] for v in per_item_values]
        lo, hi = bootstrap_ci(vals, n=n_boot, seed=seed)
        out[m] = {"mean": mean(vals), "ci": [lo, hi]}
    return out
