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
    from proviso.collect.laws import collect_current
    from proviso.config import ROOT

    load_dotenv(ROOT / ".env")
    raw_dir, state = _paths()
    client = LawApiClient(os.environ.get("LAW_OC", ""))
    try:
        current = collect_current(load_sources(), client, raw_dir, state)
    except LawApiAuthError as e:
        print(f"중단: {e}")
        return 2
    finally:
        client.close()
    print(f"현행 {len(current)}개 문서, 상태: {state.summary()}")
    return 0 if "failed" not in state.summary() else 1


def _load(args: argparse.Namespace) -> int:
    from proviso.db.connect import apply_schema, connect
    from proviso.db.load import load_current

    raw_dir, state = _paths()
    with connect() as conn:
        apply_schema(conn)
        result = load_current(conn, load_sources(), raw_dir, state.get_value("current_mst", {}))
    for doc, n in result["articles"].items():
        print(f"  {doc}: 조문 {n}개")
    print(f"위임 연결 {result['delegations']}개, 스냅샷 {result['snapshot_id']}")
    return 0


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

    col = sub.add_parser("collect", help="현행 법령 원본 수집(API, 다시 실행하면 이어서)")
    col.set_defaults(func=_collect)
    ld = sub.add_parser("load", help="수집한 원본을 파싱해 DB에 적재")
    ld.set_defaults(func=_load)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
