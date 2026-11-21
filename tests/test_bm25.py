"""BM25, 토크나이저, RRF 결합, 약칭 확장."""

from datetime import date

from proviso.eval.metrics import Hit
from proviso.index.bm25 import BM25, char_ngrams, morph_tokens
from proviso.retrieve.hybrid import RRFRetriever, expand_aliases


def test_morph_tokens_drop_particles_and_keep_article_numbers():
    toks = morph_tokens("투자매매업자는 제23조의2제1항에 따라 신고하여야 한다")
    assert "업자" in toks and "는" not in toks
    assert "제23조의2" in toks
    assert "신고" in toks


def test_char_ngrams_stay_inside_words():
    assert char_ngrams("금융 위원회") == ["금융", "위원", "원회"]
    assert char_ngrams("a 별표") == ["a", "별표"]


def test_bm25_prefers_rare_terms_and_shorter_docs():
    docs = [["금융", "위원회", "인가"], ["금융", "회사"], ["금융", "회사", "금융", "회사", "보고", "제출", "기한", "기타"]]
    bm = BM25(docs)
    assert bm.top(["인가"], 3)[0][0] == 0
    # 같은 단어를 담은 문서라면 짧은 문서가 앞선다
    order = [i for i, _ in bm.top(["회사"], 3)]
    assert order.index(1) < order.index(2)
    assert bm.top(["없는말"], 3) == []


class Fixed:
    def __init__(self, arts):
        self.arts = arts

    def describe(self):
        return {}

    def search(self, q, *, k, as_of=None):
        return [Hit(f"c{i}", "법", a, date(2020, 1, 1)) for i, a in enumerate(self.arts[:k])]


def test_rrf_rewards_agreement_and_dedupes_chunks():
    a = Fixed(["제1조", "제1조", "제2조", "제3조"])  # 같은 조문의 두 번째 조각은 순위를 차지하지 않는다
    b = Fixed(["제3조", "제2조", "제9조"])
    hits = RRFRetriever([("a", a), ("b", b)], k_rrf=60, depth=10).search("q", k=4)
    arts = [h.article_no for h in hits]
    assert arts[0] in ("제2조", "제3조")  # 두 검색기 모두에 있는 조문이 먼저
    assert set(arts[:2]) == {"제2조", "제3조"}
    assert len(arts) == len(set(arts)) == 4


def test_expand_aliases_longest_first_and_decree():
    aliases = sorted([("금소법", "금융소비자 보호에 관한 법률"),
                      ("금소법 시행령", "금융소비자 보호에 관한 법률 시행령")], key=lambda x: -len(x[0]))
    assert expand_aliases("금소법 시행령 설명의무", aliases).endswith("금융소비자 보호에 관한 법률 시행령")
    assert expand_aliases("금소법 설명의무", aliases) == "금소법 설명의무 금융소비자 보호에 관한 법률"
    assert expand_aliases("설명의무 범위", aliases) == "설명의무 범위"
