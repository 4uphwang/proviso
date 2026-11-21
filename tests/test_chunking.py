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
