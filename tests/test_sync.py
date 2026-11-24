"""17장 증분 동기화: 새 버전만 넣고, 바뀐 조각만 임베딩하고, 다시 돌리면 아무것도 하지 않는다."""

from datetime import date

import pytest
import yaml

from proviso.db.connect import apply_schema
from proviso.db.load import load_versions
from proviso.sync import diff_versions, report, sync
from tests.test_dense import HashEmbedder
from tests.test_load import SOURCES, VERSIONS, put, raw_dir, v  # noqa: F401  (fixture 재사용)

pytestmark = pytest.mark.db
TODAY = date(2026, 11, 20)


def edit_purpose(data):
    for u in data["법령"]["조문"]["조문단위"]:
        if u["조문내용"].startswith("제1조(목적)"):
            u["조문내용"] = "제1조(목적) 이 법은 전자금융거래의 안전을 지키는 것을 목적으로 한다."
    data["법령"]["기본정보"]["시행일자"] = "20261101"


@pytest.fixture
def index_cfg(tmp_path):
    p = tmp_path / "t-sync.yaml"
    p.write_text(yaml.safe_dump({"name": "t-sync", "chunking": {"type": "article", "size": 500, "header": True},
                                 "embedding": {"model": "x"}}, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_sync_loads_only_new_version_and_reuses_embeddings(db, raw_dir, index_cfg):  # noqa: F811
    apply_schema(db)
    emb = HashEmbedder()
    with db.transaction(force_rollback=True):
        load_versions(db, SOURCES, raw_dir, VERSIONS)
        from proviso.index.build import build_index, load_index_config
        first = build_index(db, load_index_config(index_cfg), as_of=TODAY, embedder=emb, log=lambda *_: None)

        # 그 뒤 법률이 개정됐다: 제1조만 바뀐 새 버전(2026-11-01 시행)
        put(raw_dir, "act_efta.json", "300000", "20261101", edit_purpose)
        live = {"전자금융거래법": [v("290247", "20261002", "20260929", "연혁"), v("300000", "20261101", "20261030")],
                "전자금융거래법 시행령": VERSIONS["전자금융거래법 시행령"]}
        ch = diff_versions(live, db)
        assert [x["mst"] for x in ch["전자금융거래법"]["new"]] == ["300000"]
        assert ch["전자금융거래법"]["status"][0][1:] == ("현행", "연혁")

        res = sync(db, SOURCES, raw_dir, live, today=TODAY, indexes=[index_cfg], log=lambda *_: None, embedder=emb)
        assert res["changed"] and res["loaded"] == {"전자금융거래법": 1}
        assert res["articles"]["changed"] == ["전자금융거래법 제1조"] and not res["articles"]["added"]
        ix = res["indexes"][0]
        assert ix["old"] == first and ix["new"] != first and not ix["problems"]
        assert ix["embedded"] == 1  # 제1조 조각만 새로 임베딩, 나머지는 이전 색인에서
        # 옛 본문은 지우지 않고 기간만 닫힌다
        vt = db.execute("SELECT valid_to FROM article_spans WHERE chunk_id = '전자금융거래법:제1조@2026-10-02'").fetchone()[0]
        assert vt == date(2026, 11, 1)
        assert "전자금융거래법 제1조" in report(res)

        # 같은 목록으로 다시 돌리면 아무것도 하지 않는다
        again = sync(db, SOURCES, raw_dir, live, today=TODAY, indexes=[index_cfg], log=lambda *_: None, embedder=emb)
        assert not again["changed"] and "변경 없음" in report(again)
