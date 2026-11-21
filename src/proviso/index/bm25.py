"""키워드 검색(3장). 메모리 BM25와 토크나이저 두 가지.

B1 형태소: kiwipiepy로 형태소를 나눈 뒤 내용어(명사, 동사·형용사 어간, 어근, 숫자, 외국어)만 쓴다.
   조사·어미가 빠지므로 "업자는"과 "업자가"가 같은 단어가 된다. "제23조의2" 같은 조 번호는 통째로도 넣는다.
B3 글자 n-gram: 형태소 분석 없이 어절 안의 2글자 묶음. 분석기가 모르는 신조어·오타·띄어쓰기 차이에 강하다.

코퍼스가 조문 수천 개라 Postgres 밖에서 메모리로 충분하다(docs/plan.md 3.5).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from functools import lru_cache

CONTENT_TAGS = {"NNG", "NNP", "NR", "SL", "SN", "SH", "XR", "VV", "VA"}
ARTICLE_NO_RE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?")
WORD_RE = re.compile(r"[가-힣A-Za-z0-9]+")


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi

    return Kiwi()


def morph_tokens(text: str) -> list[str]:
    out = [t.form for t in _kiwi().tokenize(text) if t.tag.split("-")[0] in CONTENT_TAGS]
    out += [re.sub(r"\s+", "", m) for m in ARTICLE_NO_RE.findall(text)]
    return out


def char_ngrams(text: str, n: int = 2) -> list[str]:
    out = []
    for w in WORD_RE.findall(text):
        if len(w) < n:
            out.append(w)
        else:
            out += [w[i:i + n] for i in range(len(w) - n + 1)]
    return out


TOKENIZERS: dict[str, Callable[[str], list[str]]] = {"morph": morph_tokens, "char2": char_ngrams}


class BM25:
    """Okapi BM25. idf는 Lucene식(log(1 + (N - df + 0.5) / (df + 0.5)))이라 음수가 없다."""

    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / self.n if self.n else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            for term, tf in Counter(d).items():
                self.postings.setdefault(term, []).append((i, tf))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def scores(self, query: list[str]) -> dict[int, float]:
        acc: dict[int, float] = {}
        for term in set(query):
            p = self.postings.get(term)
            if not p:
                continue
            idf = self.idf[term]
            for i, tf in p:
                norm = tf + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg)
                acc[i] = acc.get(i, 0.0) + idf * tf * (self.k1 + 1) / norm
        return acc

    def top(self, query: list[str], k: int) -> list[tuple[int, float]]:
        return sorted(self.scores(query).items(), key=lambda x: (-x[1], x[0]))[:k]
