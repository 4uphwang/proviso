"""사람 검수 시트(eval/review.py): 의심 판정과 결과 되돌리기."""

import json

from proviso.eval.review import agent_flags, apply
from proviso.eval.schema import EvalItem


def item(**kw):
    base = {"id": "a1", "set": "agent", "split": "dev", "question": "질문입니다요", "category": "lookup", "answerable": True,
            "gold": [{"doc_id": "법", "article": "제1조"}], "must_include": ["5억원"], "origin": "auto"}
    return EvalItem(**{**base, **kw})


def test_agent_flags():
    ok = {"refused": False, "answer": "5억원 [법 제1조]", "cites": [{"doc": "법", "article": "제1조", "status": "shown"}]}
    assert agent_flags(item(), ok) == []
    assert agent_flags(item(), {**ok, "answer": "3억원"}) == ["핵심어 빠짐: 5억원"]
    assert agent_flags(item(), {**ok, "refused": True}) == ["답할 수 있는 문항인데 거절"]
    no = item(answerable=False, gold=[], must_include=[], category="unanswerable", refusal_reason="out_of_corpus")
    assert agent_flags(no, ok) == ["답할 수 없는 문항인데 답함"]


def test_apply(tmp_path):
    p = tmp_path / "set.jsonl"
    p.write_text("".join(json.dumps({"id": i, "origin": "auto", "notes": "Claude 작성."}) + "\n" for i in ("a", "b", "c")))
    n = apply(p, {"a": {"verdict": "ok"}, "b": {"verdict": "wrong", "memo": "제2조가 맞음"}})
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert n == {"ok": 1, "wrong": 1, "unsure": 0}
    assert rows[0]["origin"] == "human" and rows[1]["origin"] == "auto" and "제2조가 맞음" in rows[1]["notes"]
    assert rows[2] == {"id": "c", "origin": "auto", "notes": "Claude 작성."}


def test_parse_triage():
    from proviso.eval.review import parse_triage

    assert parse_triage('{"verdict": "wrong", "reason": "제2조가 맞음"}') == {"verdict": "wrong", "reason": "제2조가 맞음"}
    assert parse_triage("음… ```json\n{\"verdict\": \"ok\", \"reason\": \"\"}\n```")["verdict"] == "ok"
    assert parse_triage("모르겠음")["verdict"] == "unsure"
    assert parse_triage('{"verdict": "maybe"}')["verdict"] == "unsure"


def test_triage_prompt_wraps_material():
    from proviso.eval.review import triage_prompt

    it = {"id": "r1", "question": "자본금은?", "as_of": None, "answerable": True, "must_include": [], "flags": ["x"],
          "gold_texts": [{"doc_id": "법", "article": "제6조", "title": "요건", "valid_from": "2020-01-01", "text": "30억원"}]}
    p = triage_prompt(it, ["검색 조각"])
    tag = p.split("<", 2)[1].split(">")[0]
    assert p.count(f"<{tag}>") == 2 and "검색 조각" in p and "30억원" in p
    assert triage_prompt(it) == triage_prompt(it)  # 같은 문항이면 같은 지시(저장된 응답을 다시 쓴다)
