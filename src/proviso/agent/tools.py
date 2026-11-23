"""에이전트 도구(6장): 검색, 조문 보기, 참조·위임 따라가기, 용어 정의 찾기.

도구는 글(모델이 읽을 자료)과 그 글에 보인 조문 목록을 돌려준다. 보인 조문만 인용해도 되는 근거다.
현행 조문만 다룬다(시점은 7장).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from proviso.answer.cite import CITE_RE, LawNames, article_name
from proviso.index.bm25 import TOKENIZERS

Key = tuple[str, str]  # (doc_id, 조 번호)
CUT = "(중략)"
DEFINE_RE = r"[\"“]\s*{term}\s*[\"”]\s*(?:\([^)]*\)\s*)?(?:이?란|이?라\s*함은|이?라\s*(?:한다|하며|하고)|은|는)"


def focus(text: str, query: str, limit: int) -> str:
    """긴 조문에서 질문과 단어가 겹치는 줄(항·호)을 골라 원래 순서대로 보여 준다. 첫 줄(조 제목)은 늘 넣는다."""
    if len(text) <= limit:
        return text
    tok = TOKENIZERS["morph"]
    q = set(tok(query))
    lines = text.split("\n")
    toks = [set(tok(x)) for x in lines]
    df = {t: sum(t in s for s in toks) for t in q}
    score = [sum(1.0 / df[t] for t in q & s) for s in toks]  # 흔한 말일수록 작게
    keep, size = {0}, len(lines[0][:limit // 3])
    for i in sorted(range(1, len(lines)), key=lambda i: (-score[i], i)):
        for j in (i, _parent(lines, i), i + 1):  # 줄, 그 줄이 딸린 항, 다음 줄
            if j is None or j in keep or j >= len(lines) or size + len(lines[j]) > limit:
                continue
            keep.add(j)
            size += len(lines[j])
        if size > limit * 0.95:
            break
    out, prev = [], -1
    for i in sorted(keep):
        if i != prev + 1:
            out.append(CUT)
        out.append(lines[i] if i else lines[0][:limit // 3] if len(lines[0]) > limit // 3 else lines[0])
        prev = i
    return "\n".join(out + ([CUT] if prev < len(lines) - 1 else []))


def _parent(lines: list[str], i: int) -> int | None:
    """호·목 줄이면 바로 위의 항(①…) 줄."""
    if re.match(r"\s*[①-⑳]", lines[i]):
        return None
    for j in range(i - 1, 0, -1):
        if re.match(r"\s*[①-⑳]", lines[j]):
            return j
    return None


@dataclass
class Law:
    """조문과 그 관계. articles: (문서, 조) → {doc_title, title, valid_from, text}."""
    articles: dict[Key, dict]
    refs: dict[Key, list[Key]]        # 본문이 가리키는 조문(수집 범위 안)
    down: dict[Key, list[Key]]        # 법 조문 → 위임받은 시행령 조문
    up: dict[Key, list[Key]] = field(default_factory=dict)  # 시행령 조문 → 위임한 법 조문

    def linked(self, key: Key) -> list[tuple[str, Key]]:
        out, seen = [], {key}
        for kind, table in (("위임받은 시행령", self.down), ("위임한 법", self.up), ("참조", self.refs)):
            for k in table.get(key, []):
                if k in self.articles and k not in seen:
                    seen.add(k)
                    out.append((kind, k))
        return out

    def label(self, key: Key) -> str:
        a = self.articles[key]
        return f"[{a['doc_title']} {key[1]}({a['title'] or ''}), 시행 {a['valid_from']}]"

    def short(self, key: Key) -> str:
        return f"{self.articles[key]['doc_title']} {key[1]}({self.articles[key]['title'] or ''})"


def load_law(conn, today: date) -> Law:
    from proviso.index.build import current_articles

    arts = {(d, a["article_no"]): a for d, xs in current_articles(conn, today).items() for a in xs}
    ids: dict[str, Key] = {}
    refs: dict[Key, list[Key]] = {}
    for tid, d, art, rs in conn.execute("""SELECT t.text_id, s.doc_id, s.article_no, t.refs FROM article_spans s
            JOIN article_texts t USING (text_id) WHERE s.valid_from <= %s AND (s.valid_to IS NULL OR %s < s.valid_to)""",
                                        (today, today)):
        ids[tid] = (d, art)
        refs[(d, art)] = list(dict.fromkeys((r["doc_id"], r["article"]) for r in rs if r["doc_id"]))
    down: dict[Key, list[Key]] = {}
    up: dict[Key, list[Key]] = {}
    for a, b in conn.execute("SELECT DISTINCT from_text_id, to_text_id FROM delegations"):
        if a in ids and b in ids:
            down.setdefault(ids[a], []).append(ids[b])
            up.setdefault(ids[b], []).append(ids[a])
    order = {k: i for i, k in enumerate(arts)}
    for t in (down, up):
        for k in t:
            t[k].sort(key=lambda x: order.get(x, 1 << 30))
    return Law(arts, refs, down, up)


def arrange(keys: list[Key], order: str, law: Law) -> list[Key]:
    """점수순 목록을 배치 순서대로. ends: 1, 3, 5, … 다음 …, 4, 2(높은 순위가 처음과 끝, 낮은 순위가 가운데)."""
    if order == "ends":
        return keys[0::2] + keys[1::2][::-1]
    if order == "law":
        pos = {k: i for i, k in enumerate(law.articles)}  # 문서별 조 순서(current_articles의 정렬)
        return sorted(keys, key=lambda k: pos.get(k, 1 << 30))
    return keys


@dataclass
class Result:
    text: str
    shown: list[Key] = field(default_factory=list)
    error: bool = False


SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "search", "description": "금융 법령에서 질문과 관련된 조문을 찾는다. 어디를 봐야 할지 모를 때 먼저 쓴다.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "찾을 내용(문장 또는 핵심 용어)"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_article", "description": "조문 하나의 본문과, 그 조문이 참조하거나 위임한 조문 목록을 본다.",
        "parameters": {"type": "object", "properties": {
            "law": {"type": "string", "description": "법령 이름(예: 자본시장법 시행령)"},
            "article": {"type": "string", "description": "조 번호(예: 제9조, 제118조의4, 별표 1)"}},
            "required": ["law", "article"]}}},
    {"type": "function", "function": {
        "name": "follow_reference",
        "description": "조문이 가리키는 다른 조문과, '대통령령으로 정하는'을 실제로 정한 시행령 조문의 본문을 본다.",
        "parameters": {"type": "object", "properties": {
            "law": {"type": "string", "description": "법령 이름"},
            "article": {"type": "string", "description": "조 번호"}}, "required": ["law", "article"]}}},
    {"type": "function", "function": {
        "name": "define_term", "description": "법령이 정한 용어의 뜻을 찾는다.",
        "parameters": {"type": "object", "properties": {
            "term": {"type": "string", "description": "용어(예: 전문투자자)"},
            "law": {"type": "string", "description": "법령 이름(알 때만)"}}, "required": ["term"]}}},
]


class Toolbox:
    """도구 모음. search_fn(query) → (문서, 조) 목록(관련도순)."""

    def __init__(self, law: Law, names: LawNames, search_fn: Callable[[str], list[Key]], k: int = 5,
                 link_hints: bool = False, order: str = "score"):
        """link_hints: 검색 결과마다 이어진 조문 이름을 함께 보여 준다(본문은 아니다).
        order(8장): 검색 결과 배치. score 점수순, ends 1·2위를 처음과 끝에, law 법령·조 번호순."""
        if order not in ("score", "ends", "law"):
            raise ValueError(f"모르는 배치 순서: {order!r}")
        self.law, self.names, self._search, self._k, self._hints, self._order = law, names, search_fn, k, link_hints, order

    def call(self, name: str, args: Any, question: str) -> Result:
        fn = {"search": self.search, "get_article": self.get_article, "follow_reference": self.follow_reference,
              "define_term": self.define_term}.get(name)
        if fn is None:
            return Result(f"없는 도구: {name}", error=True)
        args = {k: v for k, v in args.items() if isinstance(v, (str, int))} if isinstance(args, dict) else {}
        try:
            return fn(question=question, **{k: str(v) for k, v in args.items()})
        except TypeError:
            return Result(f"{name}의 인자가 잘못됨: {sorted(args)}", error=True)

    def key(self, law: str, article: str) -> Key | Result:
        """모델이 쓴 법령 이름과 조 번호 → (문서, 조). 못 찾으면 까닭을 글로 돌려준다."""
        hit = self.names.ending(law)
        m = CITE_RE.search(article) or CITE_RE.search(f"제{article.strip()}조")
        if not hit:
            docs = sorted({a["doc_title"] for a in self.law.articles.values()})
            return Result(f"'{law}'은 수집한 법령이 아님. 수집한 법령: {', '.join(docs)}", error=True)
        if not m:
            return Result(f"조 번호를 읽지 못함: {article}", error=True)
        key = (hit[0], article_name(m))
        if key not in self.law.articles:
            return Result(f"{hit[0]}에 {key[1]}은 없음(현행 기준). search로 찾아라.", error=True)
        return key

    def search(self, query: str, question: str = "") -> Result:
        keys = arrange([k for k in self._search(query) if k in self.law.articles][:self._k], self._order, self.law)
        if not keys:
            return Result("찾은 조문이 없음")
        return Result("\n\n".join(f"{self.law.label(k)}\n{focus(self.law.articles[k]['text'], query + ' ' + question, 1000)}"
                                  + self._hint(k) for k in keys), keys)

    def _hint(self, key: Key) -> str:
        links = self.law.linked(key)[:4] if self._hints else []
        names = ", ".join(f"{self.law.short(k)}({kind})" for kind, k in links)
        return f"\n→ 이어진 조문(본문은 follow_reference로 본다): {names}" if links else ""

    def get_article(self, law: str, article: str, question: str = "") -> Result:
        key = self.key(law, article)
        if isinstance(key, Result):
            return key
        text = f"{self.law.label(key)}\n{focus(self.law.articles[key]['text'], question, 4000)}"
        links = self.law.linked(key)
        if links:
            text += "\n\n연결된 조문(본문은 get_article이나 follow_reference로 본다):\n" + "\n".join(
                f"- {kind}: {self.law.short(k)}" for kind, k in links[:12])
        return Result(text, [key])

    def follow_reference(self, law: str, article: str, question: str = "") -> Result:
        key = self.key(law, article)
        if isinstance(key, Result):
            return key
        links = self.law.linked(key)
        if not links:
            return Result(f"{self.law.short(key)}에 이어진 조문이 없음")
        body = [f"{kind}: {self.law.label(k)}\n{focus(self.law.articles[k]['text'], question, 900)}" for kind, k in links[:4]]
        more = [self.law.short(k) for _, k in links[4:12]]
        return Result("\n\n".join(body) + (f"\n\n더 있음: {', '.join(more)}" if more else ""), [k for _, k in links[:4]])

    def attach(self, keys: list[Key], question: str, limit: int = 1500) -> Result | None:
        """코드가 고른 조문의 본문을 붙인다(모델이 부른 도구가 아니다)."""
        keys = [k for k in keys if k in self.law.articles]
        if not keys:
            return None
        return Result("이어진 조문(위임·참조):\n\n" + "\n\n".join(
            f"{self.law.label(k)}\n{focus(self.law.articles[k]['text'], question, limit)}" for k in keys), keys)

    def define_term(self, term: str, law: str = "", question: str = "") -> Result:
        hit = self.names.ending(law) if law else None
        bare = re.sub(r"\s+", "", term.strip("\"“”' "))
        if not bare:
            return Result("용어가 비어 있음", error=True)
        pat = re.compile(DEFINE_RE.format(term=r"\s*".join(map(re.escape, bare))))
        found: list[tuple[Key, str]] = []
        for key, a in self.law.articles.items():
            lines = a["text"].split("\n")
            for i, ln in enumerate(lines):
                if pat.search(ln):
                    found.append((key, _with_children(lines, i)))
                    break
        found.sort(key=lambda x: (bool(hit) and x[0][0] != hit[0], x[0][1] != "제2조"))  # 지정한 법, 정의 조항 먼저
        if not found:
            return Result(f"'{term}'을(를) 정의한 조문을 찾지 못함. search로 찾아라.")
        found = found[:3]
        return Result("\n\n".join(f"{self.law.label(k)}\n{t}" for k, t in found), [k for k, _ in found])


def _with_children(lines: list[str], i: int, limit: int = 1200) -> str:
    """정의 줄과 그에 딸린 호·목 줄."""
    def level(x: str) -> int:
        x = x.strip()
        return 0 if re.match(r"[①-⑳]|제\d+조", x) else 1 if re.match(r"\d+(?:의\d+)?\.", x) else 2 if re.match(r"[가-하]\.", x) else 3
    out, base = [lines[i]], level(lines[i])
    for ln in lines[i + 1:]:
        if level(ln) <= base or sum(map(len, out)) + len(ln) > limit:
            break
        out.append(ln)
    return "\n".join(out)
