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


def _refs(text, *, self_doc="A", parent=None):
    rs = extract_refs(text, self_doc=self_doc, self_article="제999조", parent_doc=parent, name_to_doc=NAMES)
    return [(r["doc_id"] or r["law_name"], r["article"]) for r in rs]


@pytest.mark.parametrize("text,expected", [
    # 나열: 이어지는 조문은 앞 법의 것
    ("「상법」 제363조, 제366조의2제2항ㆍ제3항, 제367조 및 제368조를 준용한다", [("상법", a) for a in ("제363조", "제366조의2", "제367조", "제368조")]),
    ("「상법」 제17조부터 제19조까지, 제21조 또는 제22조제1항 단서", [("상법", a) for a in ("제17조", "제19조", "제21조", "제22조")]),
    ("「상법」 제360조의3ㆍ제360조의9", [("상법", "제360조의3"), ("상법", "제360조의9")]),
    ("「상법」 제363조에 따라 같은 법 제360조의3, 제374조", [("상법", "제363조"), ("상법", "제360조의3"), ("상법", "제374조")]),
    # 이어지지 않으면 자기 법
    ("「상법」 제418조의 방식으로 배정할 때 제161조에 따라", [("상법", "제418조"), ("A", "제161조")]),
    ("「상법」 제5조에 적합할 것 6. 대주주(제12조의 대주주를 말한다)", [("상법", "제5조"), ("A", "제12조")]),
    ("1. 「상법」 제5조\n2. 제12조", [("상법", "제5조"), ("A", "제12조")]),
    ("「상법」 제5조 및 이 법 제12조, 제13조", [("상법", "제5조"), ("A", "제12조"), ("A", "제13조")]),
    # 괄호는 건너뛰어 잇는다. 괄호 안은 '…를 준용하는 경우로 한정'일 때만 앞 법의 것
    ("「상법」 제287조(제200조의2, 제201조를 준용하는 경우로 한정한다), 제288조", [("상법", a) for a in ("제287조", "제200조의2", "제201조", "제288조")]),
    ("「상법」 제354조(제189조에서 준용하는 경우를 포함한다), 제355조", [("상법", "제354조"), ("A", "제189조"), ("상법", "제355조")]),
    ("「상법」 제7조(제1호다목1)부터 4)까지로 한정한다), 제8조", [("상법", "제7조"), ("상법", "제8조")]),
    ("「산업발전법」(개정되기 전의 것을 말한다) 제14조", [("산업발전법", "제14조")]),
    ("「자본시장과 금융투자업에\n  관한 법률」 제 9조", [("자본시장법", "제9조")]),  # 별표의 줄바꿈과 띄어쓰기
    # 부칙·일부개정법률의 조문은 본칙 조문이 아니다
    ("법률 제1호 은행법 일부개정법률 제52조제3항, 같은 법률 부칙 제3조", []),
])
def test_refs_in_lists(text, expected):
    assert _refs(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("법 제28조, 제29조ㆍ제33조 및 이 영 제18조", [("A", "제28조"), ("A", "제29조"), ("A", "제33조"), ("A 시행령", "제18조")]),
    ('법 제18조에서 "대통령령으로 정하는 자"란 제11조 각 호의 자', [("A", "제18조"), ("A 시행령", "제11조")]),
    ("「상법」 제5조에 따른 자로서 같은 법 시행령 제7조, 제8조", [("상법", "제5조"), ("상법 시행령", "제7조"), ("상법 시행령", "제8조")]),
    ("「상법 시행령」 제18조 및 같은 영 제19조", [("상법 시행령", "제18조"), ("상법 시행령", "제19조")]),
    ("「상법」 제5조의 법인이 법 제6조 또는 같은 법 제7조를", [("상법", "제5조"), ("A", "제6조"), ("A", "제7조")]),
    ("법 부칙 제3조에 따른 사본", []),
    ("법 별표 2 제2호 및 별표 6", [("A", "별표 2"), ("A 시행령", "별표 6")]),
    ("「상법」 제77조에 따라, 같은 법 별표 2", [("상법", "제77조"), ("상법", "별표 2")]),
])
def test_refs_in_decree(text, expected):
    assert _refs(text, self_doc="A 시행령", parent="A") == expected


def test_parent_ref_only_in_decree():
    refs = extract_refs("법 제3조제1항 단서", self_doc="A 시행령", self_article="제5조", parent_doc="A", name_to_doc={})
    assert refs[0]["doc_id"] == "A"


def test_delegated_from_quoted_pattern(decree):
    d5 = by_no(decree[1], "제5조").delegated_from
    assert {"doc_id": "전자금융거래법", "article": "제3조", "paragraph": 1, "item": None} in d5
    d4 = by_no(decree[1], "제4조").delegated_from
    assert d4 and d4[0]["article"] == "제2조" and d4[0]["item"] == "15"


def test_appendices_parsed_as_units(decree):
    apps = [a for a in decree[1] if a.kind == "appendix"]
    assert [a.article_no for a in apps] == ["별표 1", "별표 1의2"]
    first = apps[0]
    assert first.sort_key > max(a.sort_key for a in decree[1] if a.kind == "article")
    assert first.text.startswith("■ 전자금융거래법 시행령 [별표 1]")  # 원문 유지
    assert first.title.startswith("정보보호최고책임자의 자격")
    # 제목의 "(제11조의4제4항 관련)"이 시행령 조문 참조로 잡힌다
    assert {"doc_id": "전자금융거래법 시행령", "law_name": None, "article": "제11조의4", "paragraph": 4, "item": None} in first.refs


def test_flatten_table_removes_box_chars():
    from proviso.parse.articles import flatten_table
    out = flatten_table("┏━━┯━━┓\n┃인가 │500 ┃\n┗━━┷━━┛")
    assert out == "인가 500"


def test_article_refers_to_appendix():
    refs = extract_refs("자격요건은 별표 1의2와 같다. 「은행법 시행령」 별표 3", self_doc="A", self_article="제11조",
                        parent_doc=None, name_to_doc={})
    got = [(r["doc_id"], r["article"]) for r in refs]
    assert ("A", "별표 1의2") in got and (None, "별표 3") in got


def test_text_id_ignores_whitespace_only_changes(act):
    import dataclasses
    a = by_no(act[1], "제2조")
    b = dataclasses.replace(a, text=a.text.replace("\n", "\n  ").replace(" 이 법", "이 법"))
    c = dataclasses.replace(a, text=a.text.replace("접근매체", "접근 수단"))
    assert a.text_id == b.text_id and a.text_id != c.text_id
