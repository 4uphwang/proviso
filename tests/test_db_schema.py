import pytest

from proviso.db.connect import apply_schema, table_names

pytestmark = pytest.mark.db

EXPECTED = {
    "documents", "law_aliases", "law_versions", "article_texts", "article_spans",
    "delegations", "snapshots", "traces", "spans",
}


def test_schema_applies_twice(db):
    apply_schema(db)
    apply_schema(db)  # 다시 실행해도 안전해야 함
    assert EXPECTED <= table_names(db)
    ext = db.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert ext is not None


def test_span_validity_query(db):
    """시점 질의(valid_from <= as_of < valid_to)가 버전별로 맞는 조문을 고르는지 확인."""
    apply_schema(db)
    with db.transaction(force_rollback=True):
        db.execute("INSERT INTO documents (doc_id, doc_type, title) VALUES ('T법', 'act', '테스트법')")
        db.execute("""INSERT INTO law_versions (doc_id, mst, effective_on) VALUES
                      ('T법', '1', '2020-01-01'), ('T법', '2', '2023-07-01')""")
        db.execute("""INSERT INTO article_texts (text_id, doc_id, article_no, text) VALUES
                      ('old', 'T법', '제1조', '옛 본문'), ('new', 'T법', '제1조', '새 본문')""")
        db.execute("""INSERT INTO article_spans (chunk_id, doc_id, article_no, sort_key, text_id, mst, version_effective_on, valid_from, valid_to) VALUES
                      ('T법:제1조@2020-01-01', 'T법', '제1조', 100, 'old', '1', '2020-01-01', '2020-01-01', '2023-07-01'),
                      ('T법:제1조@2023-07-01', 'T법', '제1조', 100, 'new', '2', '2023-07-01', '2023-07-01', NULL)""")
        q = """SELECT t.text FROM article_spans s JOIN article_texts t USING (text_id)
               WHERE s.doc_id = 'T법' AND s.article_no = '제1조'
                 AND s.valid_from <= %s AND (s.valid_to IS NULL OR %s < s.valid_to)"""
        assert db.execute(q, ("2021-05-05", "2021-05-05")).fetchone()[0] == "옛 본문"
        assert db.execute(q, ("2023-07-01", "2023-07-01")).fetchone()[0] == "새 본문"
        with pytest.raises(Exception):
            with db.transaction():
                db.execute("""INSERT INTO article_spans (chunk_id, doc_id, article_no, sort_key, text_id, mst, version_effective_on, valid_from, valid_from_basis)
                              VALUES ('x', 'T법', '제1조', 100, 'new', '2', '2023-07-01', '2024-01-01', '추측')""")
