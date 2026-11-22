"""targets.json + out_*.jsonl + unanswerable.json → evalset/agent.jsonl(에이전트 세트 조립)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from proviso.eval.build_agent import to_item
from proviso.eval.build_retrieval import STYLE_LEN
from proviso.eval.schema import EvalItem


def assemble(work: Path, out: Path) -> tuple[list[EvalItem], list[str]]:
    targets = {t["id"]: t for t in json.loads((work / "targets.json").read_text(encoding="utf-8"))}
    written = {}
    for f in sorted(work.glob("out_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                w = json.loads(line)
                written[w["id"]] = w
    items, notes = [], []
    for tid, t in sorted(targets.items()):
        w = written.get(tid)
        if not w or w.get("skip") or not (w.get("question") or "").strip():
            notes.append(f"{tid} 건너뜀: {(w or {}).get('skip')}")
            continue
        it = to_item(t, w)
        body = t["text"] + "\n" + t.get("text2", "")
        for m in it.must_include:
            if m not in body:
                notes.append(f"{tid} must_include가 본문에 없음: {m!r}")
            if m in it.question:
                notes.append(f"{tid} must_include가 질문에 있음: {m!r}")
            if re.fullmatch(r"제\d+조.*", m):
                notes.append(f"{tid} must_include가 조 번호: {m!r}")
        lo, hi = STYLE_LEN[it.style]
        if not lo <= len(it.question) <= hi:
            notes.append(f"{tid} 길이 {len(it.question)} ({it.style})")
        if re.search(r"제\s*\d+\s*조", it.question):
            notes.append(f"{tid} 질문에 조 번호")
        items.append(it)
    for u in json.loads((work / "unanswerable.json").read_text(encoding="utf-8")):
        items.append(EvalItem.model_validate({
            "id": u["id"], "set": "agent", "split": u["split"], "question": u["question"], "category": "unanswerable",
            "answerable": False, "refusal_reason": u["refusal_reason"], "style": u["style"], "origin": "auto",
            "notes": "Claude 작성."}))
    out.write_text("".join(i.model_dump_json(exclude_defaults=False, exclude_none=True) + "\n" for i in items), encoding="utf-8")
    return items, notes
