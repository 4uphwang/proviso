from datetime import date

from proviso.index.chunking import fixed_chunks, join_document


def char_tokens(text):
    """테스트용 토크나이저: 공백 아닌 글자 하나가 토큰 하나."""
    return [(i, i + 1) for i, ch in enumerate(text) if not ch.isspace()]


ARTS = [
    {"article_no": "제1조", "valid_from": date(2020, 1, 1), "valid_to": None, "text": "가" * 30},
    {"article_no": "제2조", "valid_from": date(2020, 1, 1), "valid_to": None, "text": "나" * 30},
    {"article_no": "제3조", "valid_from": date(2021, 5, 1), "valid_to": None, "text": "다" * 30},
]


def test_join_keeps_offsets():
    text, spans = join_document(ARTS)
    assert [text[a:b] for a, b, _ in spans] == [a["text"] for a in ARTS]


def test_fixed_chunks_ignore_article_boundaries_and_record_articles():
    chunks = fixed_chunks(ARTS, char_tokens, size=40, overlap=10)
    # 토큰 90개, 보폭 30 → 0~40, 30~70, 60~90
    assert len(chunks) == 3
    assert [a["article_no"] for a in chunks[0]["articles"]] == ["제1조", "제2조"]
    assert chunks[0]["articles"][0]["chars"] == 30 and chunks[0]["articles"][1]["chars"] == 10
    assert [a["article_no"] for a in chunks[2]["articles"]] == ["제3조"]
    assert chunks[2]["articles"][0]["valid_from"] == "2021-05-01"
    # 겹침: 앞 조각의 끝 10토큰이 다음 조각 앞에 다시 나온다
    assert chunks[0]["text"][-10:] == chunks[1]["text"][:10]


def test_short_document_is_one_chunk():
    chunks = fixed_chunks(ARTS[:1], char_tokens, size=500, overlap=50)
    assert len(chunks) == 1 and chunks[0]["text"] == "가" * 30


def test_article_chunks_one_per_article_and_split_long_at_paragraphs():
    from proviso.index.chunking import article_chunks
    long_text = "제5조(제목)\n① " + "가" * 30 + "\n② " + "나" * 30 + "\n③ " + "다" * 30
    arts = [{"article_no": "제4조", "valid_from": date(2020, 1, 1), "text": "제4조(짧음) 짧은 조문",
             "doc_title": "전자금융거래법", "heading": "제1장 총칙", "title": "짧음"},
            {"article_no": "제5조", "valid_from": date(2020, 1, 1), "text": long_text,
             "doc_title": "전자금융거래법", "heading": None, "title": "제목"}]
    chunks = article_chunks(arts, char_tokens, size=70)
    assert [c["articles"][0]["article_no"] for c in chunks] == ["제4조", "제5조", "제5조"]
    # 항 경계에서 끊김: 두 번째 조각은 ③으로 시작
    assert chunks[1]["text"].startswith("제5조(제목)\n①") and chunks[2]["text"].startswith("③")
    assert chunks[2]["articles"][0]["parts"] == 2

    with_header = article_chunks(arts, char_tokens, size=70, header=True)
    assert with_header[0]["text"].startswith("[전자금융거래법] 제1장 총칙 > 제4조(짧음)\n")
    assert with_header[2]["text"].startswith("[전자금융거래법] 제5조(제목)\n③")


def test_article_chunks_hard_split_single_huge_line():
    from proviso.index.chunking import article_chunks
    arts = [{"article_no": "별표 1", "valid_from": date(2020, 1, 1), "text": "라" * 250, "doc_title": "법"}]
    chunks = article_chunks(arts, char_tokens, size=100)
    assert [len(c["text"]) for c in chunks] == [100, 100, 50]
