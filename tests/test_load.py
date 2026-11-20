import json
import shutil

import pytest

from proviso.config import ROOT, Sources
from proviso.db.connect import apply_schema
from proviso.db.load import load_versions

pytestmark = pytest.mark.db
FIX = ROOT / "tests" / "fixtures"
SOURCES = Sources.model_validate({"laws": [
    {"id": "전자금융거래법", "type": "act", "query": "전자금융거래법", "aliases": ["전금법"]},
    {"id": "전자금융거래법 시행령", "type": "decree", "parent": "전자금융거래법", "query": "전자금융거래법 시행령"},
]})


def v(mst, eff, prom, status="현행"):
    return {"mst": mst, "effective": eff, "promulgated": prom, "revision_kind": "일부개정", "status": status}


VERSIONS = {"전자금융거래법": [v("290247", "20261002", "20260929")],
            "전자금융거래법 시행령": [v("285727", "20260428", "20260428")]}


def put(raw_dir, src, mst, eff, edit=None):
    data = json.loads((FIX / src).read_text(encoding="utf-8"))
    if edit:
        edit(data)
    (raw_dir / "eflaw").mkdir(exist_ok=True)
    (raw_dir / "eflaw" / f"{mst}_{eff}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def raw_dir(tmp_path):
    put(tmp_path, "act_efta.json", "290247", "20261002")
    put(tmp_path, "decree_efta.json", "285727", "20260428")
    return tmp_path


def test_load_is_idempotent_and_links_delegations(db, raw_dir):
    apply_schema(db)
    with db.transaction(force_rollback=True):
        r1 = load_versions(db, SOURCES, raw_dir, VERSIONS)
        r2 = load_versions(db, SOURCES, raw_dir, VERSIONS)
        # 법률 6단위 중 1개는 장 제목, 시행령 5조 + 별표 2
        assert r1["docs"] == r2["docs"] == {"전자금융거래법": {"versions": 1, "article_rows": 5},
                                             "전자금융거래법 시행령": {"versions": 1, "article_rows": 7}}
        n = db.execute("SELECT count(*) FROM article_spans WHERE doc_id LIKE '전자금융거래법%'").fetchone()[0]
        assert n == 12  # 두 번 넣어도 늘지 않음
        assert db.execute("SELECT doc_id FROM law_aliases WHERE alias = '전금법'").fetchone()[0] == "전자금융거래법"
        rows = db.execute(
            """SELECT a.article_no, b.article_no FROM delegations d
               JOIN article_texts a ON a.text_id = d.from_text_id JOIN article_texts b ON b.text_id = d.to_text_id
               WHERE a.doc_id = '전자금융거래법'""").fetchall()
        assert ("제3조", "제5조") in rows
        vf, vt = db.execute(
            "SELECT valid_from, valid_to FROM article_spans WHERE chunk_id = '전자금융거래법:제2조@2026-10-02'").fetchone()
        assert vf.isoformat() == "2026-10-02" and vt is None


def _tweak(suffix, drop_last=False):
    def edit(data):
        units = data["법령"]["조문"]["조문단위"]
        units[1]["조문내용"] += suffix
        if drop_last:
            units.pop()
    return edit


def test_validity_by_effective_date_and_unchanged_text_shared(db, raw_dir):
    """늦게 공포됐지만 먼저 시행되는 버전, 부분 시행(같은 MST 두 시행일), 바뀌지 않은 조문의 본문 공유."""
    apply_schema(db)
    put(raw_dir, "act_efta.json", "280277", "20251216", _tweak(" (부분 시행분)"))
    put(raw_dir, "act_efta.json", "280277", "20261217", _tweak(" (나중 시행분)", drop_last=True))
    versions = {"전자금융거래법": [
        v("280277", "20251216", "20251216", "연혁"),
        v("290247", "20261002", "20260929"),
        v("280277", "20261217", "20251216", "시행예정"),
    ]}
    with db.transaction(force_rollback=True):
        load_versions(db, SOURCES, raw_dir, versions)
        rows = db.execute(
            """SELECT mst, valid_from::text, valid_to::text FROM article_spans
               WHERE doc_id = '전자금융거래법' AND article_no = '제1조' ORDER BY valid_from""").fetchall()
        assert rows == [("280277", "2025-12-16", "2026-10-02"),
                        ("290247", "2026-10-02", "2026-12-17"),
                        ("280277", "2026-12-17", None)]
        # 제2조는 세 버전 모두 같은 본문 → 고유 본문은 하나
        assert db.execute("SELECT count(*) FROM article_texts WHERE doc_id='전자금융거래법' AND article_no='제2조'").fetchone()[0] == 1
        # 마지막 버전에서 빠진 조문(제28조)은 2026-12-17에 닫힌다
        last = db.execute(
            """SELECT max(valid_to)::text, bool_or(valid_to IS NULL) FROM article_spans
               WHERE doc_id='전자금융거래법' AND article_no='제28조'""").fetchone()
        assert last == ("2026-12-17", False)
        # as_of 질의: 2026-11-01에는 290247 본문
        q = """SELECT s.mst FROM article_spans s WHERE s.doc_id='전자금융거래법' AND s.article_no='제1조'
               AND s.valid_from <= %s AND (s.valid_to IS NULL OR %s < s.valid_to)"""
        assert db.execute(q, ("2026-11-01", "2026-11-01")).fetchone()[0] == "290247"
