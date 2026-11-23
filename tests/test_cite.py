"""인용 파서·분류 규칙."""

from proviso.answer.cite import CITE_RE, LawNames, classify, is_refusal, parse

N = LawNames([("자본시장법", "자본시장법"), ("자본시장과 금융투자업에 관한 법률", "자본시장법"),
              ("자본시장법 시행령", "자본시장법 시행령"), ("금소법", "금융소비자보호법")],
             {"자본시장법 시행령": "자본시장법"})


def cites(t):
    return [(c.doc_id, c.outside, c.article) for c in parse(t, N)]


def test_parse_names_and_lists():
    assert cites("[자본시장법 제9조 제1항] 이고") == [("자본시장법", None, "제9조")]
    assert cites("「자본시장과 금융투자업에 관한 법률」 제249조의2에 따라") == [("자본시장법", None, "제249조의2")]
    assert cites("자본시장법 시행령 제10조") == [("자본시장법 시행령", None, "제10조")]
    assert cites("자본시장과 금융투자업에 관한 법률 시행령 제10조") == [("자본시장법 시행령", None, "제10조")]
    assert cites("자본시장법 제9조, 제10조") == [("자본시장법", None, "제9조"), ("자본시장법", None, "제10조")]
    assert cites("금소법 제19조와 별표 1") [0] == ("금융소비자보호법", None, "제19조")


def test_parse_same_law_outside_and_bare():
    assert cites("자본시장법 제9조를 본다. 같은 법 시행령 제10조")[0] == ("자본시장법", None, "제9조")
    assert cites("자본시장법 제9조. 같은 법 제11조")[1] == ("자본시장법", None, "제11조")
    assert cites("은행법 제2조에서")[0] == (None, "은행법", "제2조")
    assert cites("제5조에 따라") == [(None, None, "제5조")]


def test_classify():
    shown = {("자본시장법", "제9조")}
    corpus = shown | {("자본시장법", "제12조")}
    ctx = "제9조(정의) 은행법 제2조에 따른 자와 제8조의 자"
    st = lambda t: classify(parse(t, N)[0], shown, corpus, ctx, N)  # noqa: E731
    assert st("자본시장법 제9조") == "shown"
    assert st("제9조에 따라") == "shown"
    assert st("자본시장법 제8조") == "mentioned"
    assert st("은행법 제2조") == "mentioned"
    assert st("은행법 제99조") == "outside"
    assert st("자본시장법 제12조") == "unseen"
    assert st("자본시장법 제999조") == "nonexistent"
    assert st("제77조에 따라") == "unresolved"


def test_refusal_detection():
    assert is_refusal("제공된 법령에서 근거를 찾지 못해 답할 수 없습니다.")
    assert is_refusal("제공된 자료에는 해당 내용이 포함되어 있지 않습니다.")
    assert is_refusal("제공된 자료에는 외국환 해외송금 신고 한도에 관한 규정은 없습니다. 제시된 법령은 …")
    assert not is_refusal("거래소는 신고할 의무가 없습니다. [자본시장법 제9조]")
    assert not is_refusal("금융위원회는 등록을 거부할 수 있습니다. [자본시장법 제9조]")
    assert CITE_RE.search("제 45 조")


def test_list_of_articles_keeps_law_name():
    cs = parse("「상법」 제 198 조, 제 217 조제 2 항부터 제 220 조까지 및 제 224 조를 적용하지 않는다", N)
    assert [(c.outside, c.article) for c in cs] == [("상법", a) for a in ("제198조", "제217조", "제220조", "제224조")]
    ctx = "「상법」 제173조, 제198조 및 제217조제2항에도 불구하고"
    assert classify(parse("상법 제217조", N)[0], set(), set(), ctx, N) == "mentioned"
    assert classify(parse("상법 제300조", N)[0], set(), set(), ctx, N) == "outside"


def test_item_number_is_not_branch_article():
    assert parse("제 239 조의 1 호의 10에 따라", N)[0].article == "제239조"
    assert parse("제324조의10제1호", N)[0].article == "제324조의10"


def test_refusal_anywhere_but_not_legal_negation():
    assert is_refusal("[자본시장법 제9조]는 정의일 뿐이다. " * 10 + "제공된 법령에서 근거를 찾지 못해 답할 수 없습니다.")
    assert not is_refusal("투자합자회사에는 법령에 따라 상법 제198조가 적용되지 않습니다. 겸직할 수 없습니다.")


def test_verifier_excerpt_and_pick():
    from proviso.eval.verify_agent import excerpt, read_pick

    long = "가" * 5000 + "정답 문구" + "나" * 2000
    assert "정답 문구" in excerpt(long, ["정답 문구"]) and len(excerpt(long, ["정답 문구"])) <= 3010
    assert excerpt(long, ["없는 말"]) == long[:3000] and excerpt("짧다", ["짧다"]) == "짧다"
    assert read_pick("A, C", 5) == {1, 3} and read_pick("198,224", 5) is None and read_pick("F", 5) is None


def test_present_marks_bad_citations_and_adds_disclaimer():
    from proviso.answer.run import DISCLAIMER, present

    out = present("답 [보험업법 제1조]", [{"doc": None, "outside": "보험업법", "article": "제1조", "status": "outside"}])
    assert "확인되지 않은 인용: 보험업법 제1조" in out and out.endswith(DISCLAIMER)
    assert "확인되지" not in present("답", [{"doc": "상법", "outside": None, "article": "제1조", "status": "shown"}])


def test_keywords_found_through_markdown_bold():
    from proviso.answer.score import item_scores
    from types import SimpleNamespace as NS
    it = NS(category="lookup", answerable=True, gold=[NS(doc_id="법", article="제1조")], must_include=["6개월 이내"])
    a = {"answer": "**6 개월 이내**에 해야 합니다.", "cites": [], "refused": False}
    assert item_scores(it, {"answer_in_context": True}, a)["핵심어 포함률"] == 1.0


def test_keyword_rules_ch8():
    from proviso.answer.score import keyword_hit, keywords

    assert keywords(["다음 각 목의 정보", "어느 하나에 해당하는 주주", "고시하는 것", "청문"]) == ["청문"]
    assert keyword_hit("투자매매업ㆍ투자중개업", "투자매매업 및 투자중개업을 할 수 있다")
    assert keyword_hit("투자매매업ㆍ투자중개업", "**투자중개업**, 투자매매업")
    assert not keyword_hit("투자매매업ㆍ투자중개업", "투자매매업만 할 수 있다")
    assert keyword_hit("6 개월 이내", "**6 개월 이내**")
