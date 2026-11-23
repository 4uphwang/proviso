"""요청 기록(6장): 단계가 span으로 남고, DB와 파일에 같은 id로 저장되고, 표로 읽힌다."""

import json

import pytest

from proviso.agent.loop import Reply, run_agent
from proviso.db.connect import apply_schema
from proviso.trace.store import save, show, spans
from test_agent import Script, box, call


def trace():
    llm = Script(call("search", query="자기자본"), call("follow_reference", law="가상법", article="제5조"), Reply("5억원", [], 200, 20))
    return run_agent("자기자본 얼마?", llm, box())


def test_spans_mark_choice_and_final():
    s = spans(trace())
    assert [(x["kind"], x["name"]) for x in s] == [("llm", "chat"), ("tool", "search"), ("llm", "chat"), ("tool", "follow_reference"), ("llm", "chat")]
    assert [x["output"]["final"] for x in s if x["kind"] == "llm"] == [False, False, True]
    assert s[1]["output"]["shown"] == ["가상법 제5조"] and "5억원" not in json.dumps(s[3], ensure_ascii=False)  # 본문은 남기지 않는다


@pytest.mark.db
def test_save_and_show(db, tmp_path):
    apply_schema(db)
    tid = save(db, trace(), question="자기자본 얼마?", answer="5억원", refused=False, config={"model": "m"}, snapshot_id=None, out_dir=tmp_path)
    (f,) = tmp_path.glob("*.jsonl")
    rec = json.loads(f.read_text())
    assert rec["trace_id"] == tid and len(rec["spans"]) == 5 and rec["stop"] == "answered"
    table = show(db, tid[:8])
    assert "tool:follow_reference" in table and "가상법 시행령 제3조" in table and "5억원" in table
    db.rollback()
