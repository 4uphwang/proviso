from proviso.answer.judge_llm import build_prompt, keyword_context, parse


def test_parse_lines():
    assert parse("1: 있음\n2: **없음**\n3. 틀림", 3) == ["yes", "no", "wrong"]
    assert parse("1: 있음", 2) == ["yes", None]
    assert parse("1: 있음\n1: 없음", 1) == ["yes"]


def test_keyword_context_ignores_spaces():
    t = "제2조(정의) 1의3. \"거래내용 판단정보\"란 다음 각 목의 정보를 말한다."
    assert "거래내용 판단정보" in keyword_context("다음 각목의 정보", [t], width=20)
    assert keyword_context("없는 말", [t]) is None


def test_prompt_numbers_keywords():
    p = build_prompt("질문", "답", ["가", "나"], ["문장 가", None])
    assert "1. 가\n   (조문 속 문장: …문장 가…)\n2. 나" in p


def test_parse_loose_formats():
    assert parse("1 번: 있음\n2 번: 없음", 2) == ["yes", "no"]
    assert parse("1. 청문: 없음\n2. 등록을 취소: 있음", 2) == ["no", "yes"]
    assert parse("1. 번호: 틀림", 1) == ["wrong"]
    assert parse("있음\n없음", 2) == ["yes", "no"]
