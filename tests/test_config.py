import pytest
from pydantic import ValidationError

from proviso.config import Sources, load_sources


def test_repo_sources_load():
    s = load_sources()
    assert "전자금융거래법" in s.doc_ids
    decrees = [l for l in s.laws if l.type == "decree"]
    assert len(decrees) == 4 and all(d.parent in s.doc_ids for d in decrees)


def _laws(*laws):
    return {"laws": list(laws)}


def test_decree_needs_known_parent():
    with pytest.raises(ValidationError, match="parent"):
        Sources.model_validate(_laws({"id": "A 시행령", "type": "decree", "query": "q", "parent": "없는법"}))


def test_duplicate_id_rejected():
    a = {"id": "A", "type": "act", "query": "q"}
    with pytest.raises(ValidationError, match="중복"):
        Sources.model_validate(_laws(a, a))


def test_alias_clash_rejected():
    with pytest.raises(ValidationError, match="약칭"):
        Sources.model_validate(_laws(
            {"id": "A", "type": "act", "query": "q", "aliases": ["X"]},
            {"id": "B", "type": "act", "query": "q", "aliases": ["X"]},
        ))
