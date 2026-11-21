"""CI 점수 검사: 모델·DB 없이 돌 수 있는 검색(형태소 BM25 + 약칭 확장)으로 recall@10이 기준값 아래로 떨어지면 실패.

말뭉치는 기준일의 현행 조문을 압축 파일(tests/fixtures/corpus.jsonl.gz)로 빼 둔 것이다.
법령 본문은 저작권법 제7조의 보호받지 못하는 저작물이고, 출처(법제처 국가법령정보센터)를 첫 줄에 적는다.
평가 문항은 시점 문항을 뺀 전체(dev + test). 회귀 검사라 고르는 데 쓰지 않으므로 test를 써도 된다.

말뭉치를 다시 뽑거나 검색 코드를 바꿔 점수가 오르면 `proviso gate check --update`로 기준값을 올린다.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

from proviso.config import ROOT
from proviso.eval.metrics import item_values, mean, score_item
from proviso.eval.run import load_items

CORPUS = ROOT / "tests" / "fixtures" / "corpus.jsonl.gz"
GATE = ROOT / "evalset" / "ci_gate.json"
EVALSET = ROOT / "evalset" / "retrieval.jsonl"
SPEC = {"type": "bm25", "params": {"tokenizer": "morph", "header": True}, "expand_aliases": True}


def export_corpus(conn, as_of: date, path: Path = CORPUS) -> int:
    from proviso.eval.run import latest_snapshot
    from proviso.index.build import current_articles

    arts = current_articles(conn, as_of)
    aliases = conn.execute("SELECT a.alias, d.title FROM law_aliases a JOIN documents d USING (doc_id)"
                           " ORDER BY a.alias").fetchall()
    meta = {"source": "법제처 국가법령정보센터(www.law.go.kr) Open API", "snapshot_id": latest_snapshot(conn),
            "as_of": str(as_of), "aliases": [list(r) for r in aliases]}
    n = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for doc_id, rows in arts.items():
            for a in rows:
                f.write(json.dumps({"doc_id": doc_id, **a, "valid_from": str(a["valid_from"]),
                                    "valid_to": str(a["valid_to"]) if a["valid_to"] else None},
                                   ensure_ascii=False) + "\n")
                n += 1
    return n


def load_corpus(path: Path = CORPUS) -> tuple[dict[str, Any], dict[str, list[dict]]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        meta = json.loads(f.readline())
        arts: dict[str, list[dict]] = {}
        for line in f:
            a = json.loads(line)
            a["valid_from"] = date.fromisoformat(a["valid_from"])
            a["valid_to"] = date.fromisoformat(a["valid_to"]) if a["valid_to"] else None
            arts.setdefault(a.pop("doc_id"), []).append(a)
    return meta, arts


def run_gate(corpus: Path = CORPUS, evalset: Path = EVALSET) -> dict[str, Any]:
    from proviso.retrieve.bm25 import BM25Retriever
    from proviso.retrieve.hybrid import AliasExpander, alias_pairs

    meta, arts = load_corpus(corpus)
    as_of = date.fromisoformat(meta["as_of"])
    r = AliasExpander(None, BM25Retriever(None, as_of=as_of, articles=arts, **SPEC["params"]),
                      aliases=alias_pairs([tuple(x) for x in meta["aliases"]]))
    items = [it for it in load_items(evalset, "all") if it.category != "temporal"]
    vals = [item_values(score_item(it, r.search(it.question, k=10), as_of)["rank"]) for it in items]
    return {"n": len(items), "snapshot_id": meta["snapshot_id"],
            "recall@10": round(mean([v["recall@10"] for v in vals]), 4),
            "mrr@10": round(mean([v["mrr@10"] for v in vals]), 4)}


def check(update: bool = False, gate_path: Path = GATE) -> tuple[bool, str]:
    got = run_gate()
    base = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else None
    if update or base is None:
        tol = base["tolerance"] if base else 0.01
        gate_path.write_text(json.dumps({"retriever": SPEC, **got, "tolerance": tol}, ensure_ascii=False, indent=2)
                             + "\n", encoding="utf-8")
        return True, f"기준값 저장: recall@10 {got['recall@10']}, mrr@10 {got['mrr@10']} ({got['n']}문항)"
    floor = base["recall@10"] - base["tolerance"]
    ok = got["n"] == base["n"] and got["recall@10"] >= floor
    msg = (f"recall@10 {got['recall@10']} (기준 {base['recall@10']} − 허용 {base['tolerance']} = {floor:.4f}), "
           f"mrr@10 {got['mrr@10']}, 문항 {got['n']}/{base['n']}")
    return ok, ("통과: " if ok else "실패: ") + msg
