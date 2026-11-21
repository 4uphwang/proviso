"""proviso 명령줄 도구."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from proviso.config import load_sources


def _evalset_check(args: argparse.Namespace) -> int:
    from proviso.eval.schema import validate_file

    doc_ids = load_sources().doc_ids
    failed = False
    for path in args.files:
        report = validate_file(Path(path), doc_ids)
        print(f"== {path}: 문항 {len(report.items)}개, 오류 {len(report.errors)}개")
        for name, counter in report.counts().items():
            print(f"  {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(counter.items())))
        for err in report.errors:
            print(f"  ! {err}")
        failed |= not report.ok
    return 1 if failed else 0


def _db_init(args: argparse.Namespace) -> int:
    from proviso.db.connect import apply_schema, connect, table_names

    with connect() as conn:
        apply_schema(conn)
        print("테이블:", ", ".join(sorted(table_names(conn))))
    return 0


def _paths():
    from proviso.collect.state import StateStore
    from proviso.config import ROOT

    raw_dir = ROOT / "data" / "raw"
    return raw_dir, StateStore(ROOT / "data" / "state" / "collect.json")


def _collect(args: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv

    from proviso.collect.law_api import LawApiAuthError, LawApiClient
    from proviso.collect.laws import collect_versions
    from proviso.config import ROOT

    load_dotenv(ROOT / ".env")
    raw_dir, state = _paths()
    client = LawApiClient(os.environ.get("LAW_OC", ""))
    try:
        versions = collect_versions(load_sources(), client, raw_dir, state, scope="all" if args.all else "current")
    except LawApiAuthError as e:
        print(f"중단: {e}")
        return 2
    finally:
        client.close()
    print(f"{len(versions)}개 문서, 상태: {state.summary()}")
    return 0 if "failed" not in state.summary() else 1


def _load(args: argparse.Namespace) -> int:
    from proviso.db.connect import apply_schema, connect
    from proviso.db.load import load_versions

    raw_dir, state = _paths()
    with connect() as conn:
        apply_schema(conn)
        result = load_versions(conn, load_sources(), raw_dir, state.get_value("versions", {}))
    for doc, r in result["docs"].items():
        print(f"  {doc}: 버전 {r['versions']}개, 조문(버전별 합) {r['article_rows']}개")
    print(f"고유 조문 {result['unique_texts']}개, 위임 연결 {result['delegations']}개, 스냅샷 {result['snapshot_id']}")
    return 0


def _index_build(args: argparse.Namespace) -> int:
    from datetime import date

    from proviso.db.connect import apply_schema, connect
    from proviso.index.build import build_index, load_index_config

    cfg = load_index_config(args.name)
    with connect() as conn:
        apply_schema(conn)
        build_index(conn, cfg, as_of=date.fromisoformat(args.as_of) if args.as_of else None)
    return 0


def _eval(args: argparse.Namespace) -> int:
    import json

    from proviso.db.connect import connect
    from proviso.eval.run import format_summary, load_config, run_eval

    cfg = load_config(Path(args.config))
    if args.repeats:
        cfg["repeats"] = args.repeats
    with connect() as conn:
        path = run_eval(cfg, conn, split=args.split, limit=args.limit)
    print(format_summary(json.loads(path.read_text(encoding="utf-8"))))
    print(f"\n결과: {path}")
    return 0


def _compare(args: argparse.Namespace) -> int:
    from proviso.eval.compare import CompareError, compare_files

    try:
        md = compare_files(Path(args.a), Path(args.b), allow_snapshot_mismatch=args.allow_snapshot_mismatch)
    except CompareError as e:
        print(f"비교 불가: {e}")
        return 2
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


def _gate_export(args: argparse.Namespace) -> int:
    from datetime import date

    from proviso.db.connect import connect
    from proviso.eval.gate import CORPUS, export_corpus

    with connect() as conn:
        n = export_corpus(conn, date.fromisoformat(args.as_of))
    print(f"조문 {n}개 → {CORPUS}")
    return 0


def _gate_check(args: argparse.Namespace) -> int:
    from proviso.eval.gate import check

    ok, msg = check(update=args.update)
    print(msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proviso", description="금융 법령 근거 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evalset", help="평가셋 도구")
    ev_sub = ev.add_subparsers(dest="evalset_command", required=True)
    check = ev_sub.add_parser("check", help="평가셋 형식 검증")
    check.add_argument("files", nargs="+")
    check.set_defaults(func=_evalset_check)

    db = sub.add_parser("db", help="DB 도구")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    init = db_sub.add_parser("init", help="스키마 적용(다시 실행해도 안전)")
    init.set_defaults(func=_db_init)

    col = sub.add_parser("collect", help="법령 원본 수집(시행일 기준, 다시 실행하면 이어서)")
    col.add_argument("--all", action="store_true", help="과거 버전 전체(기본: 현행·시행예정만)")
    col.set_defaults(func=_collect)
    ld = sub.add_parser("load", help="수집한 원본을 파싱해 DB에 적재")
    ld.set_defaults(func=_load)

    ix = sub.add_parser("index", help="검색 색인")
    ix_sub = ix.add_subparsers(dest="index_command", required=True)
    ixb = ix_sub.add_parser("build", help="config/indexes/<이름>.yaml로 색인 만들기(같은 설정·스냅샷이면 건너뜀)")
    ixb.add_argument("name")
    ixb.add_argument("--as-of", help="이날 유효한 조문으로(기본: 오늘)")
    ixb.set_defaults(func=_index_build)

    evp = sub.add_parser("eval", help="검색 평가 실행 → runs/<시각>_<이름>.json")
    evp.add_argument("--config", required=True, help="config/experiments/<이름>.yaml")
    evp.add_argument("--split", choices=["dev", "test", "all"], help="기본: 설정 파일의 split")
    evp.add_argument("--limit", type=int, help="앞에서 N문항만(빠른 확인용. 비교에는 쓰지 말 것)")
    evp.add_argument("--repeats", type=int, help="같은 설정 반복 횟수(기본: 설정 파일)")
    evp.set_defaults(func=_eval)

    cmp = sub.add_parser("compare", help="두 실행 결과 비교(짝지은 부트스트랩)")
    cmp.add_argument("a")
    cmp.add_argument("b")
    cmp.add_argument("--out", help="비교표를 md 파일로도 저장")
    cmp.add_argument("--allow-snapshot-mismatch", action="store_true")
    cmp.set_defaults(func=_compare)

    gt = sub.add_parser("gate", help="CI 점수 검사(모델·DB 없이 BM25 + 약칭 확장)")
    gt_sub = gt.add_subparsers(dest="gate_command", required=True)
    gte = gt_sub.add_parser("export", help="현행 조문을 말뭉치 파일로 빼기(DB 필요)")
    gte.add_argument("--as-of", required=True, help="기준일 YYYY-MM-DD")
    gte.set_defaults(func=_gate_export)
    gtc = gt_sub.add_parser("check", help="recall@10이 기준값 − 허용폭 아래면 실패")
    gtc.add_argument("--update", action="store_true", help="지금 점수를 새 기준값으로 저장")
    gtc.set_defaults(func=_gate_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
