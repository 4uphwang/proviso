"""법령 관계 그래프: 작은 fixture DB로 만들고, 추론한 사실을 SPARQL로 물어 SQL과 맞춘다."""

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("owlrl")

from proviso.db.connect import apply_schema  # noqa: E402
from proviso.db.load import load_versions  # noqa: E402
from proviso.graph.rdf import PV, art_uri, build_graph, checks, reason  # noqa: E402
from tests.test_load import SOURCES, VERSIONS, raw_dir  # noqa: E402,F401

pytestmark = pytest.mark.db


def test_reasoned_graph_matches_sql(db, raw_dir):  # noqa: F811
    apply_schema(db)
    with db.transaction(force_rollback=True):
        load_versions(db, SOURCES, raw_dir, VERSIONS)
        g = build_graph(db)
        reason(g)
        rows = checks(db, g)
        assert all(a == b for _, a, b in rows), rows
        # 위임이 하나라도 있으면 역관계와 속성 사슬이 추론돼 있어야 한다
        pairs = list(g.subject_objects(PV.delegatesTo))
        assert pairs
        law_art, decree_art = pairs[0]
        assert (decree_art, PV.delegatedFrom, law_art) in g
        assert (decree_art, PV.implementsAct, None) in g
        assert str(art_uri("전자금융거래법", "제1조")).startswith("urn:proviso:law:")
