"""proviso 명령줄 도구."""

from __future__ import annotations

import argparse
import json
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


def _sync(args: argparse.Namespace) -> int:
    import os
    from datetime import date

    from dotenv import load_dotenv

    from proviso.collect.law_api import LawApiAuthError, LawApiClient
    from proviso.collect.laws import collect_versions
    from proviso.config import ROOT
    from proviso.db.connect import apply_schema, connect
    from proviso.sync import SYNC_INDEXES, sync, write_report

    load_dotenv(ROOT / ".env")
    raw_dir, state = _paths()
    client = LawApiClient(os.environ.get("LAW_OC", ""))
    try:  # 목록은 새로 받고, 아직 없는 본문만 받는다
        live = collect_versions(load_sources(), client, raw_dir, state, scope="all")
    except LawApiAuthError as e:
        print(f"중단(인증/IP 오류): {e}")
        return 2
    finally:
        client.close()
    with connect() as conn:
        apply_schema(conn)
        res = sync(conn, load_sources(), raw_dir, live, today=date.fromisoformat(args.today) if args.today else date.today(),
                   indexes=[] if args.no_index else SYNC_INDEXES)
    path = write_report(res)
    print(open(path, encoding="utf-8").read())
    print(f"보고서: {path}")
    return 0


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
        path = run_eval(cfg, conn, split=args.split, limit=args.limit, category=args.category)
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


def _graph_export(args: argparse.Namespace) -> int:
    from proviso.db.connect import connect
    from proviso.graph.rdf import export

    with connect() as conn:
        n, out = export(conn, Path(args.out))
    print(f"트리플 {n}개 → {out} (스키마: ontology/proviso.ttl)")
    return 0


def _graph_check(args: argparse.Namespace) -> int:
    from proviso.db.connect import connect
    from proviso.graph.rdf import build_graph, checks, reason

    with connect() as conn:
        g = build_graph(conn)
        n0 = len(g)
        secs = reason(g)
        print(f"트리플 {n0}개 → 추론 뒤 {len(g)}개 ({secs}초)")
        rows = checks(conn, g)
    print("| 질문 | SPARQL | SQL | 같음 |\n|---|---:|---:|---|")
    for name, a, b in rows:
        print(f"| {name} | {a} | {b} | {'예' if a == b else '아니오'} |")
    return 0 if all(a == b for _, a, b in rows) else 1


def _answer_run(args: argparse.Namespace) -> int:
    import yaml

    from proviso.answer.run import run
    from proviso.db.connect import connect

    retr = yaml.safe_load(open(args.retriever_config, encoding="utf-8"))["retriever"]
    with connect() as conn:
        path = run(conn, evalset=Path(args.evalset), split=args.split, arms=args.arms.split(","), model=args.model,
                   k=args.k, retriever_cfg=retr, name=args.name, limit=args.limit)
    print(f"결과: {path}")
    return 0


def _answer_ask(args: argparse.Namespace) -> int:
    import yaml

    from proviso.answer.run import ask
    from proviso.db.connect import connect

    retr = yaml.safe_load(open(args.retriever_config, encoding="utf-8"))["retriever"]
    with connect() as conn:
        print(ask(conn, args.question, retriever_cfg=retr, model=args.model, k=args.k))
    return 0


def _answer_report(args: argparse.Namespace) -> int:
    import json

    from proviso.answer.run import load_agent_items, rescore
    from proviso.answer.score import report

    run_ = json.loads(open(args.run, encoding="utf-8").read())
    items = {i.id: i for i in load_agent_items(Path(run_["evalset"]["path"]), "all")}
    if args.rescore:
        from proviso.db.connect import connect

        with connect() as conn:
            run_ = rescore(conn, Path(args.run), items)
    print(report(run_, items, args.arms.split(",")))
    return 0


def _agent_run(args: argparse.Namespace) -> int:
    import yaml

    from proviso.agent.run import run
    from proviso.db.connect import connect

    retr = yaml.safe_load(open(args.retriever_config, encoding="utf-8"))["retriever"]
    with connect() as conn:
        for arm in args.arms.split(","):
            path = run(conn, evalset=Path(args.evalset), split=args.split, arm=arm, model=args.model, retriever_cfg=retr,
                       limit=args.limit, ids=set(args.ids.split(",")) if args.ids else None, token_limit=args.token_limit,
                       max_tokens=args.max_tokens, repeat=args.repeat,
                       guard=args.guard)
            print(f"결과: {path}", flush=True)
    return 0


def _agent_report(args: argparse.Namespace) -> int:
    import json

    from proviso.agent.run import merge
    from proviso.answer.run import load_agent_items
    from proviso.answer.score import report

    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.runs]
    items = {i.id: i for i in load_agent_items(Path(runs[0]["evalset"]["path"]), "all")}
    cats = set(args.category.split(",")) if args.category else None
    print(report(merge(runs), items, [r["arm"] for r in runs], lambda i: not cats or i.category in cats))
    return 0


def _agent_ask(args: argparse.Namespace) -> int:
    import yaml

    from proviso.agent.ask import ask
    from proviso.db.connect import connect

    retr = yaml.safe_load(open(args.retriever_config, encoding="utf-8"))["retriever"]
    with connect() as conn:
        text, tid = ask(conn, args.question, retriever_cfg=retr, model=args.model, seed_search=not args.no_seed)
    print(f"{text}\n\n기록: proviso trace {tid[:8]}" if tid else text)
    return 0


def _agent_attack(args: argparse.Namespace) -> int:
    import yaml

    from proviso.agent.attack import run_attacks, summary
    from proviso.db.connect import connect

    retr = yaml.safe_load(open(args.retriever_config, encoding="utf-8"))["retriever"]
    with connect() as conn:
        path = run_attacks(conn, path=Path(args.evalset), split=args.split, model=args.model, retriever_cfg=retr,
                           max_tokens=args.max_tokens, shield=args.shield, workers=args.workers,
                           scope=args.scope)
    run = json.loads(path.read_text(encoding="utf-8"))
    for k, (a, b) in summary(run).items():
        print(f"{k:10s} 통한 공격 {a}/{b}")
    print(f"결과: {path}")
    return 0


def _agent_faith(args: argparse.Namespace) -> int:
    from proviso.db.connect import connect
    from proviso.eval.faith_run import run_faith, summary

    ids = set(args.ids.split(",")) if args.ids else None
    with connect() as conn:
        path = run_faith(conn, Path(args.run), args.judge, ids=ids, max_tokens=args.max_tokens)
    s = summary(json.loads(path.read_text(encoding="utf-8")))
    print(f"답 {s['answers']}개(못 읽은 판정 {s['unparsed']}), 충실도 {s['faithfulness'] or 0:.3f}, "
          f"근거 없는 주장이 있는 답 {s['unfaithful_rate'] or 0:.3f}")
    print(f"결과: {path}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from proviso.serve import default_app

    uvicorn.run(default_app(args.retriever_config, args.model), host=args.host, port=args.port)
    return 0


def _otel(args: argparse.Namespace) -> int:
    from proviso.trace.otel import export_records, load_records, otlp_exporter

    recs = load_records(args.trace_ids or None, args.day)
    if not recs:
        print("내보낼 기록이 없음")
        return 1
    n = export_records(recs, otlp_exporter(args.endpoint))
    print(f"기록 {len(recs)}개, span {n}개를 {args.endpoint}로 보냄")
    return 0


def _trace_show(args: argparse.Namespace) -> int:
    from proviso.db.connect import connect
    from proviso.trace.store import show

    with connect() as conn:
        print(show(conn, args.trace_id))
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
    evp.add_argument("--category", help="이 유형만(쉼표로 여럿). 예: temporal")
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

    gr = sub.add_parser("graph", help="법령 관계 그래프(RDF/OWL). pip install -e '.[graph]'")
    gr_sub = gr.add_subparsers(dest="graph_command", required=True)
    gre = gr_sub.add_parser("export", help="DB → Turtle 파일")
    gre.add_argument("--out", default="data/graph/proviso.ttl")
    gre.set_defaults(func=_graph_export)
    grc = gr_sub.add_parser("check", help="OWL 2 RL 추론 뒤 SPARQL 답을 SQL 답과 대조")
    grc.set_defaults(func=_graph_check)

    ag = sub.add_parser("agent", help="도구를 쓰는 에이전트(6장)")
    ag_sub = ag.add_subparsers(dest="agent_command", required=True)
    agr = ag_sub.add_parser("run", help="고정 파이프라인·에이전트 설정을 평가셋으로 돌린다")
    agr.add_argument("--evalset", default="evalset/agent.jsonl")
    agr.add_argument("--split", choices=["dev", "test"], default="dev")
    agr.add_argument("--arms", default="fixed,focus,follow,focus_follow,short,short_follow,agent,agent_seed,agent_links")
    agr.add_argument("--model", default="qwen3.5:4b")
    agr.add_argument("--retriever-config", default="config/experiments/rerank30.yaml")
    agr.add_argument("--limit", type=int)
    agr.add_argument("--guard", action="store_true", help="개인 사안·투자 조언 질문을 모델 전에 거른다(answer/guard.py)")
    agr.add_argument("--repeat", type=int, default=0, help="API 모델 반복 번호(0이면 저장된 첫 응답을 다시 씀)")
    agr.add_argument("--max-tokens", type=int, default=500, help="답 길이 상한(5장 4B 기준 500)")
    agr.add_argument("--token-limit", type=int, default=11000, help="대화가 이만큼 차면 도구를 그만 쓰고 답한다(로컬 16K 문맥 기준 11000)")
    agr.add_argument("--ids", help="쉼표로 나열한 문항 id만")
    agr.set_defaults(func=_agent_run)
    agp = ag_sub.add_parser("report", help="결과 파일들을 나란히 비교한다")
    agp.add_argument("runs", nargs="+")
    agp.add_argument("--category", help="유형만(쉼표)")
    agp.set_defaults(func=_agent_report)

    aga = ag_sub.add_parser("ask", help="질문 하나에 답하고 단계별 기록을 남긴다")
    aga.add_argument("question")
    aga.add_argument("--model", default=None, help="기본: 오픈라우터 DeepSeek V4 Flash, 키가 없으면 qwen3.5:4b")
    aga.add_argument("--retriever-config", default="config/experiments/t1-asof.yaml")
    aga.add_argument("--no-seed", action="store_true", help="첫 검색도 모델에게 맡긴다")
    aga.set_defaults(func=_agent_ask)

    atk = ag_sub.add_parser("attack", help="공격 문항으로 가드레일을 잰다(11장)")
    atk.add_argument("--evalset", default="evalset/attack/attacks.jsonl")
    atk.add_argument("--split", choices=["dev", "test"], default="dev")
    atk.add_argument("--model", default="qwen3.5:4b")
    atk.add_argument("--max-tokens", type=int, default=500)
    atk.add_argument("--retriever-config", default="config/experiments/t1-asof.yaml")
    atk.add_argument("--scope", default="", help="범위 제한: prompt(시스템 지시), code(답의 코드 블록 지움), 둘 다면 prompt,code")
    atk.add_argument("--workers", type=int, default=1, help="동시에 돌릴 문항 수(API 모델일 때)")
    atk.add_argument("--shield", action="store_true", help="11장 방어(개인정보 가림, 지시 바꾸기·없는 조문 거름, 지시문 유출 검사)")
    atk.set_defaults(func=_agent_attack)

    fa = ag_sub.add_parser("faith", help="답이 본 조문에 근거하는지 판정자 모델로 잰다(13장)")
    fa.add_argument("run", help="runs/<…>_agent-….json")
    fa.add_argument("--judge", default="openrouter:z-ai/glm-5.3-flash@low", help="13장에서 라벨과 가장 잘 맞은 판정자")
    fa.add_argument("--max-tokens", type=int, default=2000)
    fa.add_argument("--ids", help="이 문항만(쉼표로 여럿)")
    fa.set_defaults(func=_agent_faith)

    tc = sub.add_parser("trace", help="기록 하나를 단계별 표로 본다")
    tc.add_argument("trace_id", help="앞 몇 글자만 줘도 된다")
    tc.set_defaults(func=_trace_show)

    sy = sub.add_parser("sync", help="바뀐 법령만 받아 넣고 바뀐 조각만 임베딩해 색인을 새로 만든다(17장)")
    sy.add_argument("--today", help="현행 기준일(기본: 오늘)")
    sy.add_argument("--no-index", action="store_true", help="색인은 새로 만들지 않는다")
    sy.set_defaults(func=_sync)

    sv = sub.add_parser("serve", help="모델을 한 번 불러 두고 HTTP로 질문을 받는다(16장). pip install -e '.[serve]'")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--model", help="기본: agent ask와 같다(DeepSeek V4 Flash, 키가 없으면 4b)")
    sv.add_argument("--retriever-config", default="config/experiments/t1-asof.yaml")
    sv.set_defaults(func=_serve)

    ot = sub.add_parser("otel", help="기록을 OpenTelemetry(OTLP)로 내보낸다(15장). pip install -e '.[otel]'")
    ot.add_argument("trace_ids", nargs="*", help="기록 id(앞 몇 글자). 없으면 --day의 기록 전부, 그것도 없으면 전부")
    ot.add_argument("--day", help="YYYYMMDD")
    ot.add_argument("--endpoint", default="http://localhost:6006/v1/traces", help="기본: 로컬 Phoenix")
    ot.set_defaults(func=_otel)

    an = sub.add_parser("answer", help="답변 생성·채점(5장, 로컬 LLM)")
    an_sub = an.add_subparsers(dest="answer_command", required=True)
    anr = an_sub.add_parser("run", help="자료(검색 결과) + 질문 → 답. runs/에 저장")
    anr.add_argument("--evalset", default="evalset/agent.jsonl")
    anr.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    anr.add_argument("--arms", default="plain,cite,refuse,verify")
    anr.add_argument("--model", default="qwen3.5:4b")
    anr.add_argument("--k", type=int, default=5)
    anr.add_argument("--retriever-config", default="config/experiments/rerank30.yaml")
    anr.add_argument("--name", default="rerank30")
    anr.add_argument("--limit", type=int)
    anr.set_defaults(func=_answer_run)
    ana = an_sub.add_parser("ask", help="질문 하나에 답한다(인용 확인 + 고지)")
    ana.add_argument("question")
    ana.add_argument("--model", default="qwen3.5:4b")
    ana.add_argument("--k", type=int, default=5)
    ana.add_argument("--retriever-config", default="config/experiments/rerank30.yaml")
    ana.set_defaults(func=_answer_ask)
    anp = an_sub.add_parser("report", help="실행 결과 채점표")
    anp.add_argument("run")
    anp.add_argument("--arms", default="plain,cite,refuse,verify")
    anp.add_argument("--rescore", action="store_true", help="저장된 답을 지금 채점기로 다시 채점한다(LLM 호출 없음)")
    anp.set_defaults(func=_answer_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
