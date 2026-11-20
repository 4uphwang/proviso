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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
