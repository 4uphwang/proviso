"""색인 만들기 → 벡터 검색 → 조문 단위 결과. 실제 모델 대신 글자 해시 임베딩을 쓴다(빠르고 결정적)."""

import hashlib
import math
from datetime import date

import pytest

from proviso.db.connect import apply_schema
from proviso.db.load import load_versions
from proviso.index.build import build_index
from proviso.retrieve.dense import DenseRetriever
from tests.test_load import SOURCES, VERSIONS, raw_dir  # noqa: F401  (fixture 재사용)

pytestmark = pytest.mark.db


class HashEmbedder:
    model_name = "test-hash"

    def tokenize_offsets(self, text):
        return [(i, i + 1) for i, ch in enumerate(text) if not ch.isspace()]

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 64
            for i in range(len(t) - 1):
                v[int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16) % 64] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1
            out.append([x / n for x in v])
        return out

    def describe(self):
        return {"model": self.model_name}


def test_build_and_search_returns_articles(db, raw_dir):  # noqa: F811
    apply_schema(db)
    cfg = {"name": "test-c0", "chunking": {"type": "fixed", "size": 120, "overlap": 20}, "embedding": {"model": "x"}}
    with db.transaction(force_rollback=True):
        load_versions(db, SOURCES, raw_dir, VERSIONS)
        emb = HashEmbedder()
        index_id = build_index(db, cfg, as_of=date(2026, 11, 20), embedder=emb, log=lambda *_: None)
        assert build_index(db, cfg, as_of=date(2026, 11, 20), embedder=emb, log=lambda *_: None) == index_id
        n, = db.execute("SELECT count(*) FROM index_chunks WHERE index_id = %s", (index_id,)).fetchone()
        assert n > 1
        # 어떤 조각의 본문을 그대로 물으면 그 조각의 조문들이 맨 앞에 나온다
        text, arts = db.execute("SELECT text, articles FROM index_chunks WHERE index_id = %s AND chunk_no = 1",
                                (index_id,)).fetchone()
        r = DenseRetriever(db, "test-c0", embedder=emb)
        hits = r.search(text, k=3)
        assert [h.article_no for h in hits[:len(arts)]] == [a["article_no"] for a in arts]
        assert r.describe()["uses_as_of"] is False
