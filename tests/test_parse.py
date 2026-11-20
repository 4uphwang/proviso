import json

import pytest

from proviso.config import ROOT, load_sources
from proviso.db.load import name_index
from proviso.parse.articles import parse_law
from proviso.parse.refs import extract_refs

FIX = ROOT / "tests" / "fixtures"
NAMES = name_index(load_sources())


def parse(name, doc_id, mst, parent=None):
    raw = json.loads((FIX / name).read_text(encoding="utf-8"))
    return parse_law(raw, doc_id, mst=mst, parent_doc=parent, name_to_doc=NAMES)


@pytest.fixture(scope="module")
def act():
    return parse("act_efta.json", "전자금융거래법", "290247")


@pytest.fixture(scope="module")
def decree():
    return parse("decree_efta.json", "전자금융거래법 시행령", "285727", parent="전자금융거래법")


def by_no(arts, no):
    return next(a for a in arts if a.article_no == no)


def test_version_info(act):
    v, _ = act
    assert v.title == "전자금융거래법" and v.law_id == "010199" and v.mst == "290247"
    assert v.effective_on.isoformat() == "2026-10-02" and v.promulgated_on.isoformat() == "2026-09-29"


def test_chapter_header_skipped_and_kept_as_heading(act):
    _, arts = act
    assert [a.article_no for a in arts][:2] == ["제1조", "제2조"]
    assert by_no(arts, "제1조").heading == "제1장 총칙"


def test_branch_article_number(act):
    a = by_no(act[1], "제6조의2")
    assert a.sort_key == 602 and a.note.startswith("[본조신설")


def test_paragraph_dict_and_list_shapes(act):
    # 제2조: 항 번호 없이 호만 있는 dict 형태, 제3조: 항 list + 목
    defs = by_no(act[1], "제2조")
    assert len(defs.paragraphs) == 1 and defs.paragraphs[0]["no"] is None
    item10 = next(i for i in defs.paragraphs[0]["items"] if i["no"] == "10")
    assert "접근매체" in item10["text"] and [s["no"] for s in item10["subitems"]][:2] == ["가", "나"]
    scope = by_no(act[1], "제3조")
    assert [p["no"] for p in scope.paragraphs] == [1, 2, 3]


def test_text_keeps_original_pieces(act):
    a = by_no(act[1], "제2조")
    assert a.text.startswith("제2조(정의) 이 법에서 사용하는 용어의 정의는")
    assert "마. 가목 또는 나목의 수단이나 정보를 사용하는데 필요한 비밀번호" in a.text


def test_item_branch_and_deleted(decree):
    _, arts = decree
    items = by_no(arts, "제2조").paragraphs[0]["items"]
    assert items[1]["no"] == "1의2" and "삭제" in items[1]["text"]
    assert by_no(arts, "제3조").deleted is True
    assert by_no(arts, "제2조").deleted is False


def test_delegation_phrases_only_in_act(act, decree):
    assert by_no(act[1], "제28조").delegations, "법 제28조의 '대통령령이 정하는 금융회사'"
    assert all(not a.delegations for a in decree[1])


def test_decree_refs_resolve_to_parent(decree):
    refs = by_no(decree[1], "제5조").refs
    assert {"doc_id": "전자금융거래법", "law_name": None, "article": "제3조", "paragraph": 1, "item": None} in refs
    hanbank = [r for r in refs if r["law_name"] == "한국은행법"]
    assert hanbank and hanbank[0]["doc_id"] is None


@pytest.mark.parametrize("text,expected", [
    ("제28조제1항에 따라", [("A", "제28조", 1, None)]),
    ("제7조의2제3항제1호의2에 따른", [("A", "제7조의2", 3, "1의2")]),
    ("「자본시장과 금융투자업에 관한 법률」 제9조 및 같은 법 제10조", [("자본시장법", "제9조", None, None), ("자본시장법", "제10조", None, None)]),
    ("「은행법」 제2조", [(None, "제2조", None, None)]),
    ("이 법 제5조", [("A", "제5조", None, None)]),
    ("은행법 제2조", [("A", "제2조", None, None)]),  # 「」 없는 다른 법 이름은 '법'으로 오인하지 않음
    ("제1조를 준용한다", []),  # 자기 자신은 뺀다
])
def test_extract_refs(text, expected):
    refs = extract_refs(text, self_doc="A", self_article="제1조", parent_doc=None, name_to_doc=NAMES)
    assert [(r["doc_id"], r["article"], r["paragraph"], r["item"]) for r in refs] == expected


def test_parent_ref_only_in_decree():
    refs = extract_refs("법 제3조제1항 단서", self_doc="A 시행령", self_article="제5조", parent_doc="A", name_to_doc={})
    assert refs[0]["doc_id"] == "A"


def test_delegated_from_quoted_pattern(decree):
    d5 = by_no(decree[1], "제5조").delegated_from
    assert {"doc_id": "전자금융거래법", "article": "제3조", "paragraph": 1, "item": None} in d5
    d4 = by_no(decree[1], "제4조").delegated_from
    assert d4 and d4[0]["article"] == "제2조" and d4[0]["item"] == "15"
