"""출제 대상(targets.json)과 작성된 질문(out_*.jsonl)을 합쳐 검색 세트(evalset/retrieval.jsonl)를 만든다."""

from __future__ import annotations

import json
import re
from pathlib import Path

from proviso.eval.schema import EvalItem

HANGUL_RE = re.compile(r"[가-힣]+")
ARTICLE_NO_RE = re.compile(r"제\s*\d+\s*조")


def bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for w in HANGUL_RE.findall(text or ""):
        out.update(w[i:i + 2] for i in range(len(w) - 1))
    return out


def lexical_overlap(question: str, gold_text: str) -> float:
    """질문의 한글 2글자 조각 중 정답 본문에도 나오는 비율(0~1). 높을수록 조문 표현을 베낀 질문."""
    q = bigrams(question)
    return round(len(q & bigrams(gold_text)) / len(q), 3) if q else 0.0


# 형태별 길이 범위(GUIDELINES.md '질문 형태')
STYLE_LEN = {"keyword": (6, 30), "casual": (8, 40), "full": (15, 120)}
ENDING_RE = re.compile(r"(\?|(?<!필)요|까|죠|니)\s*$")  # 키워드형에 붙으면 안 되는 문장 끝


def to_item(t: dict, w: dict, style: dict | None = None) -> EvalItem:
    cat = t["category"]
    gold = [{"doc_id": t["doc_id"], "article": t["article_no"]}]
    tags: list[str] = []
    as_of = None
    if cat == "definition":
        gold[0]["item"] = str(t["item"])
        if t.get("paragraph"):
            gold[0]["paragraph"] = t["paragraph"]
    elif cat == "cross_ref":
        gold.append({"doc_id": t["law_doc"], "article": t["law_article"]})
        tags.append("위임")
    elif cat == "temporal":
        gold[0]["valid_from"] = t["valid_from"]
        as_of = t["as_of"]
    elif cat == "appendix":
        tags.append("별표")
    if w.get("law_named"):
        tags.append("법령명")
    if style and style.get("noisy"):
        tags.append("오타")
    gold_text = t.get("item_text") or t["text"]
    return EvalItem.model_validate({
        "id": t["id"],
        "set": "retrieval",
        "split": t["split"],
        "question": w["question"].strip(),
        "category": "lookup" if cat == "appendix" else cat,
        "answerable": True,
        "gold": gold,
        "as_of": as_of,
        "tags": tags,
        "difficulty": w.get("difficulty") or "medium",
        "style": (style or {}).get("style"),
        "origin": "auto",
        "lexical_overlap": lexical_overlap(w["question"], gold_text),
        "notes": ("Claude 작성. " + (w.get("notes") or "")).strip(),
    })


def build(work_dir: Path, out_path: Path) -> dict:
    targets = {t["id"]: t for t in json.loads((work_dir / "targets.json").read_text(encoding="utf-8"))}
    styles_path = work_dir / "styles.json"
    styles = json.loads(styles_path.read_text(encoding="utf-8")) if styles_path.exists() else {}
    written: dict[str, dict] = {}
    problems: list[str] = []
    for f in sorted(work_dir.glob("out_*.jsonl")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                w = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{f.name}:{n} JSON 오류")
                continue
            # out_fix_*(고쳐 쓴 문항), out_style_*(형태를 바꿔 쓴 문항)는 앞의 답을 덮어쓴다
            if w.get("id") in written and not f.name.startswith(("out_fix", "out_style")):
                problems.append(f"{f.name}:{n} id 중복 {w.get('id')}")
            written[w.get("id")] = w
    missing = sorted(set(targets) - set(written))
    extra = sorted(set(written) - set(targets))
    items, skipped, flags = [], [], []
    seen_q: dict[str, str] = {}
    for tid in sorted(targets):
        w = written.get(tid)
        if not w:
            continue
        if w.get("skip") or not (w.get("question") or "").strip():
            skipped.append((tid, w.get("skip")))
            continue
        try:
            item = to_item(targets[tid], w, styles.get(tid))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{tid}: {e}")
            continue
        q = item.question
        reasons = []
        if ARTICLE_NO_RE.search(q):
            reasons.append("조문 번호 포함")
        lo, hi = STYLE_LEN.get(item.style or "full")
        if not lo <= len(q) <= hi:
            reasons.append(f"길이 {len(q)}")
        if item.style == "keyword" and ENDING_RE.search(q):
            reasons.append("키워드형인데 문장 끝맺음")
        if item.style and w.get("style") and w["style"] != item.style:
            reasons.append(f"형태 불일치({w['style']})")
        # 키워드형은 낱말이 적어 겹침이 높게 나오는 게 자연스럽다(형태별 평균으로 따로 본다)
        if item.style != "keyword" and item.lexical_overlap is not None and item.lexical_overlap >= 0.8:
            reasons.append(f"겹침 {item.lexical_overlap}")
        if targets[tid]["category"] == "temporal" and not re.search(r"(19|20)\d{2}", q):
            reasons.append("시점 연도 없음")
        if "차이 미미" in (w.get("notes") or ""):
            reasons.append("차이 미미")
        if q in seen_q:
            reasons.append(f"질문 중복({seen_q[q]})")
        seen_q[q] = tid
        if reasons:
            flags.append((tid, reasons))
        items.append(item)
    out_path.write_text("".join(i.model_dump_json(exclude_none=True) + "\n" for i in items), encoding="utf-8")
    return {"written": len(written), "items": len(items), "skipped": skipped, "missing": missing,
            "extra": extra, "problems": problems, "flags": flags}
