import json
import shutil

import pytest

from proviso.config import ROOT, Sources
from proviso.db.connect import apply_schema
from proviso.db.load import load_current

pytestmark = pytest.mark.db
FIX = ROOT / "tests" / "fixtures"
SOURCES = Sources.model_validate({"laws": [
    {"id": "전자금융거래법", "type": "act", "query": "전자금융거래법", "aliases": ["전금법"]},
    {"id": "전자금융거래법 시행령", "type": "decree", "parent": "전자금융거래법", "query": "전자금융거래법 시행령"},
]})
CURRENT = {"전자금융거래법": "290247", "전자금융거래법 시행령": "285727"}


@pytest.fixture
def raw_dir(tmp_path):
    (tmp_path / "law").mkdir()
    shutil.copy(FIX / "act_efta.json", tmp_path / "law" / "290247.json")
    shutil.copy(FIX / "decree_efta.json", tmp_path / "law" / "285727.json")
    return tmp_path


def test_load_is_idempotent_and_links_delegations(db, raw_dir):
    apply_schema(db)
    with db.transaction(force_rollback=True):
        r1 = load_current(db, SOURCES, raw_dir, CURRENT)
        r2 = load_current(db, SOURCES, raw_dir, CURRENT)
        assert r1["articles"] == r2["articles"] == {"전자금융거래법": 5, "전자금융거래법 시행령": 5}  # 법률 쪽 6단위 중 1개는 장 제목
        n = db.execute("SELECT count(*) FROM article_spans WHERE doc_id LIKE '전자금융거래법%'").fetchone()[0]
        assert n == 10  # 두 번 넣어도 늘지 않음
        assert db.execute("SELECT doc_id FROM law_aliases WHERE alias = '전금법'").fetchone()[0] == "전자금융거래법"
        # 시행령 제5조 "법 제3조제1항 단서에서 대통령령이 정하는 경우" → 법 제3조
        rows = db.execute(
            """SELECT a.article_no, b.article_no FROM delegations d
               JOIN article_texts a ON a.text_id = d.from_text_id JOIN article_texts b ON b.text_id = d.to_text_id
               WHERE a.doc_id = '전자금융거래법'""").fetchall()
        assert ("제3조", "제5조") in rows
        # 현행 버전 하나뿐이면 유효 기간은 시행일부터 무기한
        vf, vt = db.execute(
            "SELECT valid_from, valid_to FROM article_spans WHERE chunk_id = '전자금융거래법:제2조@2026-10-02'").fetchone()
        assert vf.isoformat() == "2026-10-02" and vt is None


def test_validity_follows_effective_date_not_promulgation(db, raw_dir):
    """늦게 공포됐지만 먼저 시행되는 버전이 있어도 시행일 순서로 유효 기간을 잇는다."""
    apply_schema(db)
    later = json.loads((FIX / "act_efta.json").read_text(encoding="utf-8"))
    info = later["법령"]["기본정보"]
    info["공포일자"], info["시행일자"] = "20251216", "20261217"  # 먼저 공포, 나중 시행
    later["법령"]["조문"]["조문단위"][1]["조문내용"] += " (개정 예정 본문)"
    (raw_dir / "law" / "280277.json").write_text(json.dumps(later, ensure_ascii=False), encoding="utf-8")
    with db.transaction(force_rollback=True):
        load_current(db, SOURCES, raw_dir, CURRENT)
        load_current(db, SOURCES, raw_dir, {"전자금융거래법": "280277"})
        rows = db.execute(
            """SELECT mst, valid_from, valid_to FROM article_spans
               WHERE doc_id = '전자금융거래법' AND article_no = '제1조' ORDER BY valid_from""").fetchall()
        assert [(m, str(f), str(t)) for m, f, t in rows] == [
            ("290247", "2026-10-02", "2026-12-17"),
            ("280277", "2026-12-17", "None"),
        ]
