"""증분 동기화(17장, 계획서 §4.4). proviso sync

1. 법령마다 시행일 버전 목록을 새로 받아 DB(law_versions)와 비교한다: 새 버전, 상태가 바뀐 버전(시행예정 → 현행 등),
   목록에서 사라진 버전.
2. 새 버전의 본문만 받는다(이미 받은 원본은 건너뛴다).
3. 새 버전만 파싱해 넣고, 그 문서의 기간(valid_to)을 다시 계산하고, 위임 연결을 다시 잇고, 새 스냅샷을 만든다.
   사라진 버전은 지우지 않고 보고서에만 남긴다(과거 시점 질문에 답해야 하므로).
4. 색인마다 새 색인을 만들되, 글이 같은 조각은 이전 색인의 임베딩을 그대로 쓰고 바뀐 조각만 임베딩한다.
   새 색인이 점검(모든 현행 조문이 조각에 있는가)을 통과해야 쓰인다. 못 통과하면 지우고 이전 색인을 그대로 쓴다.
5. 보고서(runs/sync/<날짜>.md): 추가·변경·삭제된 현행 조문, 새로 생긴 시행 예정 버전, 정답 조문이 바뀐 평가 문항.

바뀐 것이 없으면 아무것도 쓰지 않고 '변경 없음'만 보고한다. 같은 입력으로 다시 돌리면 결과가 같다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from proviso.config import ROOT, Sources

SYNC_INDEXES = ["c3c1-bge-m3", "c3c1-bge-m3-all"]  # t1-asof(7장부터 쓰는 검색)이 읽는 색인
REPORT_DIR = ROOT / "runs" / "sync"


def db_versions(conn, doc_id: str) -> dict[tuple[str, str], str]:
    return {(m, e.strftime("%Y%m%d")): st or "" for m, e, st in
            conn.execute("SELECT mst, effective_on, status FROM law_versions WHERE doc_id = %s", (doc_id,))}


def diff_versions(live: dict[str, list[dict]], conn) -> dict[str, dict]:
    """문서마다 {new: [버전], status: [(버전, 전, 후)], gone: [(mst, 시행일)]}."""
    out = {}
    for doc, vs in live.items():
        db = db_versions(conn, doc)
        lk = {(v["mst"], v["effective"]): v for v in vs}
        out[doc] = {"new": [v for k, v in lk.items() if k not in db],
                    "status": [(v, db[k], v["status"]) for k, v in lk.items() if k in db and db[k] != v["status"]],
                    "gone": [k for k in db if k not in lk]}
    return out


def current_texts(conn, as_of: date) -> dict[tuple[str, str], str]:
    """기준일에 유효한 조문 → text_id(삭제된 조문 제외)."""
    return {(d, a): t for d, a, t in conn.execute(
        """SELECT s.doc_id, s.article_no, s.text_id FROM article_spans s JOIN article_texts x USING (text_id)
           WHERE s.valid_from <= %(d)s AND (s.valid_to IS NULL OR %(d)s < s.valid_to) AND NOT x.deleted""", {"d": as_of})}


def load_new(conn, sources: Sources, raw_dir: Path, changes: dict[str, dict], versions_by_doc: dict[str, list[dict]]) -> dict:
    """새 버전만 넣고 상태를 고친다. 한 트랜잭션. 반환: {문서: 넣은 버전 수}, 스냅샷 id."""
    from proviso.collect.laws import raw_body_path
    from proviso.db.load import _ymd, build_delegations, create_snapshot, load_version, name_index, recompute_validity
    from proviso.parse.articles import parse_law

    names = name_index(sources)
    loaded: dict[str, int] = {}
    with conn.transaction():
        for src in sorted(sources.laws, key=lambda s: s.type != "act"):  # 시행령이 모법을 참조하므로 법률 먼저
            ch = changes.get(src.id)
            if not ch:
                continue
            for v, _, after in ch["status"]:
                conn.execute("UPDATE law_versions SET status = %s WHERE doc_id = %s AND mst = %s AND effective_on = %s",
                             (after, src.id, v["mst"], _ymd(v["effective"])))
            n = 0
            for v in sorted(ch["new"], key=lambda v: (v["effective"], v["promulgated"], v["mst"])):
                path = raw_body_path(raw_dir, v["mst"], v["effective"])
                if not path.exists():
                    raise FileNotFoundError(f"원본이 없음: {path.name}(먼저 받아야 함)")
                version, articles = parse_law(json.loads(path.read_text(encoding="utf-8")), src.id, mst=v["mst"],
                                              parent_doc=src.parent, name_to_doc=names)
                version.effective_on = _ymd(v["effective"])
                load_version(conn, sources, version, articles, status=v.get("status"))
                n += 1
            if n:
                recompute_validity(conn, src.id)
                loaded[src.id] = n
        delegations = build_delegations(conn)
        snapshot = create_snapshot(conn, versions_by_doc)
    return {"loaded": loaded, "delegations": delegations, "snapshot_id": snapshot}


def check_index(conn, index_id: str, as_of: date, versions: str) -> list[str]:
    """새 색인 점검: 현행 색인이면 기준일의 모든 현행 조문이 조각에 있어야 한다. 문제 목록(비면 통과)."""
    if versions == "all":
        n = conn.execute("SELECT count(*) FROM index_chunks WHERE index_id = %s", (index_id,)).fetchone()[0]
        return [] if n else ["조각이 없음"]
    have = {(d, a["article_no"]) for d, arts in conn.execute(
        "SELECT doc_id, articles FROM index_chunks WHERE index_id = %s", (index_id,)) for a in arts}
    missing = [k for k in current_texts(conn, as_of) if k not in have]
    return [f"현행 조문 {len(missing)}개가 색인에 없음(예: {missing[:3]})"] if missing else []


def update_indexes(conn, names: list[str], as_of: date, log=print, embedder=None) -> list[dict]:
    from proviso.index.build import build_index, index_vectors, latest_index, load_index_config

    out = []
    for name in names:
        cfg = load_index_config(name)
        old = latest_index(conn, cfg["name"])
        t0 = time.perf_counter()
        new = build_index(conn, cfg, as_of=as_of, log=log, embedder=embedder,
                          reuse=index_vectors(conn, old) if old else None)
        problems = check_index(conn, new, as_of, cfg.get("versions", "current")) if new != old else []
        if problems:  # blue-green: 새 색인을 버리고 이전 색인을 그대로 쓴다
            with conn.transaction():
                conn.execute("DELETE FROM index_chunks WHERE index_id = %s", (new,))
                conn.execute("DELETE FROM index_versions WHERE index_id = %s", (new,))
        n, emb = conn.execute("SELECT chunk_count, embedded FROM index_versions WHERE index_id = %s",
                              (old if problems else new,)).fetchone()
        out.append({"name": cfg["name"], "old": old, "new": None if problems else new, "chunks": n, "embedded": emb,
                    "seconds": round(time.perf_counter() - t0, 1), "problems": problems})
    return out


def evalset_impact(before: dict, after: dict, evalset: Path = ROOT / "evalset") -> list[dict]:
    """현행 기준(as_of 없음) 문항 중 정답 조문의 현행 본문이 바뀐 것. 시점 문항은 시행일로 고정돼 영향이 없다."""
    out = []
    for f in ("retrieval.jsonl", "agent.jsonl"):
        p = evalset / f
        if not p.exists():
            continue
        for d in map(json.loads, p.read_text(encoding="utf-8").splitlines()):
            if d.get("as_of") or not d.get("gold"):
                continue
            hit = [g for g in d["gold"] if before.get((g["doc_id"], g["article"])) != after.get((g["doc_id"], g["article"]))]
            if hit:
                out.append({"id": d["id"], "set": f.split(".")[0], "articles": [f"{g['doc_id']} {g['article']}" for g in hit]})
    return out


def article_changes(before: dict, after: dict) -> dict[str, list[str]]:
    name = lambda k: f"{k[0]} {k[1]}"  # noqa: E731
    return {"added": sorted(name(k) for k in after if k not in before),
            "changed": sorted(name(k) for k in after if k in before and before[k] != after[k]),
            "removed": sorted(name(k) for k in before if k not in after)}


def sync(conn, sources: Sources, raw_dir: Path, live: dict[str, list[dict]], *, today: date,
         indexes: list[str] = SYNC_INDEXES, log: Callable[[str], None] = print, embedder=None) -> dict[str, Any]:
    """live: 새로 받은 버전 목록({문서: 버전들}, 원본은 이미 raw_dir에 있어야 한다). 결과 요약을 돌려준다."""
    changes = diff_versions(live, conn)
    n_new = sum(len(c["new"]) for c in changes.values())
    n_status = sum(len(c["status"]) for c in changes.values())
    res: dict[str, Any] = {"today": str(today), "new_versions": n_new, "status_changes": n_status,
                           "gone": {d: c["gone"] for d, c in changes.items() if c["gone"]},
                           "upcoming": sorted(f"{d} {v['effective']}" for d, c in changes.items() for v in c["new"]
                                              if v["effective"] > today.strftime("%Y%m%d"))}
    if not n_new and not n_status:
        res["changed"] = False
        return res
    t0 = time.perf_counter()
    before = current_texts(conn, today)
    res |= load_new(conn, sources, raw_dir, changes, live)
    after = current_texts(conn, today)
    res["load_seconds"] = round(time.perf_counter() - t0, 1)
    res["articles"] = article_changes(before, after)
    res["evalset_impact"] = evalset_impact(before, after)
    res["indexes"] = update_indexes(conn, indexes, today, log, embedder)
    res["changed"] = True
    return res


def report(res: dict[str, Any]) -> str:
    lines = [f"# 동기화 {res['today']}", ""]
    if not res["changed"]:
        lines += ["변경 없음(새 버전 0, 상태 변경 0)."]
        if res["gone"]:
            lines += ["", f"목록에서 사라진 버전(지우지 않음): {res['gone']}"]
        return "\n".join(lines) + "\n"
    a = res["articles"]
    lines += [f"- 새 버전 {res['new_versions']}개, 상태 변경 {res['status_changes']}개, 스냅샷 {res['snapshot_id']}",
              f"- 넣은 버전: {res['loaded']}", f"- 위임 연결 {res['delegations']}개", "",
              f"## 현행 조문({res['today']} 기준)", "",
              f"- 추가 {len(a['added'])}, 변경 {len(a['changed'])}, 삭제 {len(a['removed'])}"]
    for k, title in (("added", "추가"), ("changed", "변경"), ("removed", "삭제")):
        if a[k]:
            lines.append(f"- {title}: " + ", ".join(a[k][:40]) + (" …" if len(a[k]) > 40 else ""))
    if res["upcoming"]:
        lines += ["", "## 새로 생긴 시행 예정 버전", ""] + [f"- {u}" for u in res["upcoming"]]
    lines += ["", "## 색인", "", "| 색인 | 조각 | 새로 임베딩 | 초 | 점검 |", "|---|---:|---:|---:|---|"]
    for ix in res["indexes"]:
        lines.append(f"| {ix['new'] or ix['old']} | {ix['chunks']} | {ix['embedded']} | {ix['seconds']} | "
                     f"{'; '.join(ix['problems']) or '통과'} |")
    imp = res["evalset_impact"]
    lines += ["", f"## 정답 조문이 바뀐 평가 문항({len(imp)}개, 다시 확인 필요)", ""]
    lines += [f"- {i['id']} ({i['set']}): {', '.join(i['articles'])}" for i in imp[:100]]
    if res["gone"]:
        lines += ["", f"목록에서 사라진 버전(지우지 않음): {res['gone']}"]
    return "\n".join(lines) + "\n"


def write_report(res: dict[str, Any], out_dir: Path = REPORT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{res['today'].replace('-', '')}.md"
    p.write_text(report(res), encoding="utf-8")
    (out_dir / f"{res['today'].replace('-', '')}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                                                   encoding="utf-8")
    return p
