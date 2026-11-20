import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from proviso.config import ROOT, load_sources
from proviso.eval.schema import EvalItem, validate_file

BASE = {
    "id": "t1", "set": "agent", "split": "dev", "question": "접근매체가 무엇인가요?",
    "category": "definition", "answerable": True,
    "gold": [{"doc_id": "전자금융거래법", "article": "제2조", "item": "10"}],
    "origin": "human",
}


def item(**over):
    return EvalItem.model_validate({**BASE, **over})


def test_valid_item():
    assert item().gold[0].article == "제2조"


@pytest.mark.parametrize("article", ["2조", "제2", "제2조의", "제 2조"])
def test_bad_article_format(article):
    with pytest.raises(ValidationError, match="조 번호"):
        item(gold=[{"doc_id": "전자금융거래법", "article": article}])


def test_branch_article_ok():
    assert item(gold=[{"doc_id": "전자금융거래법", "article": "제7조의2"}])


def test_answerable_needs_gold():
    with pytest.raises(ValidationError, match="gold"):
        item(gold=[])


def test_unanswerable_rules():
    ok = item(category="unanswerable", answerable=False, gold=[], refusal_reason="out_of_corpus")
    assert ok.refusal_reason == "out_of_corpus"
    with pytest.raises(ValidationError, match="refusal_reason"):
        item(category="unanswerable", answerable=False, gold=[])
    with pytest.raises(ValidationError, match="unanswerable"):
        item(answerable=False, gold=[])  # category가 definition인데 거절 정답


def test_temporal_needs_as_of_and_valid_from():
    g = [{"doc_id": "전자금융거래법", "article": "제2조", "valid_from": "2020-12-10"}]
    assert item(category="temporal", as_of="2021-01-01", gold=g)
    with pytest.raises(ValidationError, match="as_of"):
        item(category="temporal", gold=g)
    with pytest.raises(ValidationError, match="valid_from"):
        item(category="temporal", as_of="2021-01-01")


def test_retrieval_set_has_no_unanswerable():
    with pytest.raises(ValidationError, match="검색 세트"):
        item(set="retrieval", category="unanswerable", answerable=False, gold=[], refusal_reason="out_of_corpus")


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        item(answer="오타 필드")


def test_file_level_checks(tmp_path: Path):
    p = tmp_path / "set.jsonl"
    rows = [BASE, BASE, {**BASE, "id": "t2", "gold": [{"doc_id": "없는법", "article": "제1조"}]}]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{깨진 줄\n", encoding="utf-8")
    report = validate_file(p, load_sources().doc_ids)
    assert len(report.items) == 3
    joined = "\n".join(report.errors)
    assert "중복" in joined and "없는법" in joined and "JSON 오류" in joined


def test_repo_example_set_is_valid():
    report = validate_file(ROOT / "evalset" / "examples.jsonl", load_sources().doc_ids)
    assert report.ok, report.errors
    assert report.counts()["set/split"]["agent/dev"] == 2
