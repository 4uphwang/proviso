"""13장 충실도 판정: 응답 읽기, 점수, 본 조문 모으기."""

from proviso.answer.faith import Faith, format_sources, parse, shown_keys


def test_parse_reads_json_inside_text():
    f = parse('설명\n```json\n{"claims": [{"claim": "a", "verdict": "supported", "source": 1},'
              ' {"claim": "b", "verdict": "Unsupported", "source": null}]}\n```')
    assert f.parsed and f.n == 2 and f.score == 0.5 and f.unfaithful


def test_parse_rejects_unknown_verdict_and_garbage():
    assert not parse('{"claims": [{"claim": "a", "verdict": "maybe"}]}').parsed
    assert not parse("판정할 수 없음").parsed


def test_no_claims_has_no_score():
    f = Faith([])
    assert f.score is None and not f.unfaithful


def test_shown_keys_in_order_without_repeats():
    steps = [{"tool": "search", "shown": [["법", "제1조"], ["법", "제2조"]]}, {"llm": True},
             {"tool": "get_article", "shown": [["법", "제1조"], ["영", "제3조"]]}]
    assert shown_keys(steps) == [("법", "제1조"), ("법", "제2조"), ("영", "제3조")]


def test_format_sources_numbers_and_cuts():
    arts = {("법", "제1조"): {"doc_title": "어떤법", "title": "목적", "text": "가" * 10, "valid_from": "2020-01-01"}}
    s = format_sources([("법", "제1조"), ("없음", "제9조")], arts, limit=5)
    assert s.startswith("(1) [어떤법 제1조(목적), 시행 2020-01-01]") and "가" * 5 + " …(이하 생략)" in s
    assert format_sources([], arts) == "(본 자료 없음)"
