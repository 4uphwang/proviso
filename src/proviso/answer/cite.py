"""답변에서 인용을 찾아 읽고, 그 조문이 근거로 준 자료에 있는지 가린다(5장).

분류(Citation.status)
- shown: 자료로 준 조문. 법령명 없이 "제N조"만 썼으면 자료 안에 그 번호의 조문이 있으면 shown으로 본다(bare로 따로 센다).
- mentioned: 자료로 준 조문의 본문에 적혀 있는 조문 번호(본문을 옮겨 적은 것. 지어낸 것은 아니다).
- unseen: 말뭉치에 있는 조문이지만 이번 자료에 없고 본문에도 안 적혀 있다.
- nonexistent: 우리 법령 이름을 썼는데 말뭉치에 그런 조문이 없다.
- outside: 우리 말뭉치 밖의 법 이름을 썼고 자료 본문에도 없다.
- unresolved: 법령명 없이 쓴 조문 번호가 자료에 없다.
환각 인용 = unseen + nonexistent + outside + unresolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CITE_RE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+)(?!\d|\s*[호항목]))?|별표\s*(\d+)(?:\s*의\s*(\d+))?")
HALLUCINATED = ("unseen", "nonexistent", "outside", "unresolved")
_OUTSIDE_RE = re.compile(r"([가-힣A-Za-z0-9·ㆍ]+(?:법률|법|규정|고시|규칙))(\s*시행령|\s*시행규칙)?\s*[」』”\"'\])]*\s*$")
# 같은 법의 조문을 나열할 때 사이에 오는 말("「상법」 제198조, 제217조제2항부터 제220조까지 및 제224조")
_LIST_RE = re.compile(r"(?:제\s*\d+\s*[항호](?:\s*의\s*\d+)?|[가-하]\s*목|각\s*호|부터|까지|내지|및|또는|[와과]|[,·ㆍ\s」』\])*\[(])*")
_SAME_RE = re.compile(r"(?:같은|동|이|위|해당)\s*(법률|법|시행령|영)\s*$")
_PRONOUN_LAW = {"법", "이법", "동법", "같은법", "위법", "해당법", "관련법", "관계법"}


@dataclass(frozen=True)
class Citation:
    doc_id: str | None
    outside: str | None
    article: str
    start: int


def article_name(m: re.Match) -> str:
    n, sub, b, bsub = m.groups()
    if n:
        return f"제{n}조" + (f"의{sub}" if sub else "")
    return f"별표 {b}" + (f"의{bsub}" if bsub else "")


class LawNames:
    """법령 이름·약칭 → doc_id. pairs는 (이름, doc_id). 긴 이름부터 맞춘다."""

    def __init__(self, pairs: list[tuple[str, str]], parents: dict[str, str]):
        seen = {}
        for name, doc in pairs:
            seen[re.sub(r"\s+", "", name)] = doc
        self.names = sorted(seen.items(), key=lambda x: -len(x[0]))
        self.decree = {law: dec for dec, law in parents.items()}  # 법률 → 시행령
        self.parent = parents  # 시행령 → 법률

    def ending(self, before: str) -> tuple[str, str] | None:
        """before(인용 앞 글자들)의 끝에 붙은 법령 이름을 찾는다. (doc_id, 이름)."""
        t = re.sub(r"[\s「」『』“”\"'\[\](){}《》<>*]", "", before[-60:])
        for name, doc in self.names:
            if t.endswith(name):
                return doc, name
            if t.endswith(name + "시행령") and doc in self.decree:
                return self.decree[doc], name + "시행령"
        return None


def parse(answer: str, names: LawNames) -> list[Citation]:
    out: list[Citation] = []
    last: str | None = None
    prev_end = 0
    for m in CITE_RE.finditer(answer):
        before = answer[prev_end:m.start()]
        doc = outside = None
        hit = names.ending(before)
        if hit:
            doc = hit[0]
        elif (s := _SAME_RE.search(re.sub(r"[「」『』\[\]()]", "", before))) and last:
            law = names.parent.get(last, last)
            doc = names.decree.get(law, last) if s.group(1) in ("시행령", "영") else law
        elif (o := _OUTSIDE_RE.search(before.replace("*", ""))) and re.sub(r"\s", "", o.group(0)).strip("」』”\"')]") not in _PRONOUN_LAW:
            outside = re.sub(r"\s+", " ", (o.group(1) + (o.group(2) or "")).strip())
        elif out and _LIST_RE.fullmatch(before):
            doc, outside = out[-1].doc_id, out[-1].outside  # "자본시장법 제9조, 제10조"
        out.append(Citation(doc, outside, article_name(m), m.start()))
        if doc:
            last = doc
        prev_end = m.end()
    return out


def classify(c: Citation, shown: set[tuple[str, str]], corpus: set[tuple[str, str]], context_text: str,
             names: LawNames) -> str:
    if c.doc_id and (c.doc_id, c.article) in shown:
        return "shown"
    if c.doc_id is None and c.outside is None and any(a == c.article for _, a in shown):
        return "shown"
    arts = {article_name(m) for m in CITE_RE.finditer(context_text)}
    if c.outside:
        named = {(re.sub(r"\s", "", x.outside), x.article) for x in parse(context_text, names) if x.outside}
        return "mentioned" if (re.sub(r"\s", "", c.outside), c.article) in named else "outside"
    if c.article in arts:
        return "mentioned"
    if c.doc_id:
        return "unseen" if (c.doc_id, c.article) in corpus else "nonexistent"
    return "unresolved"


REFUSAL = "제공된 법령에서 근거를 찾지 못해 답할 수 없습니다."
_FIXED = "근거를찾지못해답할수없"  # 정해진 거절 문장의 알맹이. 답의 어디에 있든 거절로 본다
# 형식 지시가 없는 답에서도 거절을 알아보기 위한 말(첫 150자와 끝 120자). dev 답을 사람이 읽고 맞췄다
REFUSAL_RE = re.compile(
    r"답(?:변)?(?:을|은)?\s*(?:드릴|할|하기|제공할)\s*(?:수\s*없|어렵)|답변이\s*(?:제공되지\s*않|불가능)"
    r"|답변은[^.\n]{0,40}?(?:할\s*수\s*없|불가능)"
    r"|근거(?:\s*조문)?(?:를|을|가|이)?\s*(?:찾지\s*못|찾을\s*수\s*없|찾아볼\s*수\s*없|없|존재하지\s*않)"
    r"|(?:자료|문헌|문서|법령|조문|법조문)\S*(?:에는|에서는|에서|에|가|는|은)[^.\n]{0,90}?"
    r"(?:(?:포함|명시|규정|기재|언급|제시|존재|담고)\S*\s*(?:있지\s*)?않|찾을\s*수\s*없"
    r"|(?:내용|규정|조항|조문|정보|기준|사항|자료|언급)(?:이|가|은|는|도)?\**\s*(?:없습니다|없으며|없다))"
    r"|제공된[^.\n]{0,60}?(?:확인할\s*수\s*없|알\s*수\s*없|정보(?:가|는)?\s*(?:없|부족))")


def is_refusal(answer: str) -> bool:
    a = answer.strip()
    return not a or _FIXED in re.sub(r"\s", "", a) or bool(REFUSAL_RE.search(a[:150]) or REFUSAL_RE.search(a[-120:]))
