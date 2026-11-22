"""조문 본문에서 다른 조문 참조와 위임 문구를 뽑는다."""

from __future__ import annotations

import re

_PREFIX = (r"(?:(?<![가-힣])(?:(?P<same_dec>같은\s*법(?:률)?\s*시행령)|(?P<same>같은\s*법(?:률)?)|(?P<same_yeong>같은\s*영)"
           r"|(?P<this>이\s*(?:법|영))|(?P<parent>법))\s*)?")
_ART = (r"제\s*(?P<art>\d+)\s*조(?:의(?P<br>\d+))?(?:\s*제(?P<para>\d+)항)?"
        r"(?:\s*제(?P<item>\d+)호(?:의(?P<itembr>\d+))?)?")
REF_RE = re.compile(_PREFIX + _ART)
APPENDIX_REF_RE = re.compile(_PREFIX + r"(?<![가-힣])별표\s*(?P<no>\d+)(?:의(?P<br>\d+))?")
# "1)", "가)" 같은 목록 번호의 닫는 괄호는 괄호 짝에서 뺀다. "(1)"의 괄호는 짝이다
LABEL_RE = re.compile(r"(?:(?<=목)|(?<=\s)|(?<=[,ㆍ지터]))(?:\d{1,2}|[가-하])\)")
NAME_RE = re.compile(r"「(?P<name>[^」]+)」")
# 부칙과 일부개정법률의 조문은 본칙 조문이 아니다
SUPPLEMENT_RE = re.compile(r"(?:부칙|개정법률|개정령)\s*(?:<[^>]*>)?\s*$")
# 앞 조문과 나열로 이어지는 사이 글: "제2항ㆍ제3항, ", "부터 ", " 단서 및 "
LIST_GAP_RE = re.compile(
    r"(?:\s|제\d+항|제\d+호(?:의\d+)?|[가-하]목|\d{1,2}\)|각\s*호|각\s*목|외의\s*부분|본문|단서|전단|후단"
    r"|부터|까지|내지|및|또는|[,ㆍ·])*")
CONNECTOR_RE = re.compile(r"부터|까지|내지|및|또는|[,ㆍ·]")
# 괄호 안이 앞 법의 조문인 문형: 상법 제287조(제200조를 준용하는 경우로 한정한다)
APPLIED_RE = re.compile(r"준용하는\s*경우[로에]\s*(?:한|만)")
DELEGATION_RE = re.compile(r"대통령령(?:으로|이)\s*정(?:하는|한다|하여|할|한)")
# 시행령이 위임받은 조항을 밝히는 전형적인 문형: 법 제N조제M항제K호(…)에서 "대통령령으로 정하는 …"이란
DELEGATED_FROM_RE = re.compile(
    r"(?<![가-힣])법\s*제(?P<art>\d+)조(?:의(?P<br>\d+))?"
    r"(?:\s*제(?P<para>\d+)항)?(?:\s*제(?P<item>\d+)호(?:의(?P<itembr>\d+))?)?"
    r"[^\"“”\n]{0,40}?에서\s*[\"“][^\"“”\n]{0,80}?대통령령[^\"“”\n]{0,80}?[\"”]"
)


def extract_refs(
    text: str,
    *,
    self_doc: str,
    self_article: str,
    parent_doc: str | None,
    name_to_doc: dict[str, str],
) -> list[dict]:
    """본문 속 '제N조…', '별표 N' 참조를 doc_id까지 풀어서 돌려준다(조문 먼저, 별표는 뒤).

    - 「법령명」 제N조: name_to_doc로 doc_id를 찾고, 없으면 doc_id=None, law_name에 이름. 「법령명」(설명) 제N조도 같다
    - 같은 법·같은 법 시행령·같은 영 제N조: 바로 앞에 나온 법(「법령명」이나 '법')이나 시행령
    - (시행령에서) 법 제N조, 법 별표 N: 모법(parent_doc)
    - 앞 조문에 쉼표·및·부터 등으로 이어지는 제N조: 앞 조문과 같은 법. 사이에 낀 괄호는 건너뛴다
    - 이 법·이 영 제N조, 그냥 제N조: 자기 문서
    부칙·일부개정법률의 조문과 자기 자신(같은 문서, 같은 조)은 뺀다. 순서를 지키며 중복 제거.
    """
    own = (self_doc, None)

    def owner_of(name: str) -> tuple[str | None, str | None]:
        name = " ".join(name.split())  # 별표는 줄이 바뀌며 이름이 끊긴다
        doc = name_to_doc.get(name)
        return (doc, None if doc else name)

    def decree_of(owner: tuple[str | None, str | None]) -> tuple[str | None, str | None]:
        name = owner[0] or owner[1]
        return owner_of(f"{name} 시행령") if name else own

    last_law = last_decree = None
    # 바로 앞 참조의 주인: (주인, 끝 위치, 법령 이름만 나온 것인지). 괄호를 열면 쌓고 닫으면 되돌린다
    prev: tuple[tuple, int, bool] | None = None
    stack: list[tuple | None] = []
    arts: list[dict] = []
    apps: list[dict] = []
    seen: set[tuple] = set()

    def inherits(start: int) -> bool:
        gap = text[prev[1]:start]
        return not gap.strip() if prev[2] else bool(LIST_GAP_RE.fullmatch(gap) and CONNECTOR_RE.search(gap))

    pos = 0
    while pos < len(text):
        c = text[pos]
        if (m := LABEL_RE.match(text, pos)):
            pos = m.end()
        elif c == "「" and (m := NAME_RE.match(text, pos)):
            o = owner_of(m.group("name"))
            if m.group("name").endswith(("시행령", "규정")):
                last_decree = o
            else:
                last_law = o
            prev, pos = (o, m.end(), True), m.end()
        elif c in "([{〔":
            stack.append(prev)
            close = _close(text, pos)
            direct = prev and not prev[2] and not text[prev[1]:pos].strip() and APPLIED_RE.search(text, pos, close)
            prev = (prev[0], pos + 1, True) if direct else None
            pos += 1
        elif c in ")]}〕":
            if stack:
                prev = stack.pop()
                if prev and LIST_GAP_RE.fullmatch(text[prev[1]:pos].split(_opener(c), 1)[0]):
                    prev = (prev[0], pos + 1, prev[2])  # 괄호를 건너뛴 자리에서 이어 본다
                else:
                    prev = None
            pos += 1
        elif c in "같이법제별" and (m := REF_RE.match(text, pos) or APPENDIX_REF_RE.match(text, pos)):
            is_app = "no" in m.re.groupindex
            start, pos = m.start(), m.end()
            if SUPPLEMENT_RE.search(text, max(0, start - 30), start):
                prev = None
                continue
            if m.group("same_dec"):
                o = last_decree = decree_of(last_law) if last_law else own
            elif m.group("same"):
                o = last_law or own
            elif m.group("same_yeong"):
                o = last_decree or own
            elif m.group("parent") and parent_doc:
                o = last_law = (parent_doc, None)
            elif m.group("this") or m.group("parent"):
                o = own
            elif prev and inherits(start) and (prev[2] or not is_app):
                o = prev[0]
            else:
                o = own
            prev = (o, pos, False)
            if is_app:
                article, para, item = appendix_no(m.group("no"), m.group("br")), None, None
            else:
                article = f"제{m.group('art')}조" + (f"의{m.group('br')}" if m.group("br") else "")
                para = int(m.group("para")) if m.group("para") else None
                item = m.group("item")
                if item and m.group("itembr"):
                    item = f"{item}의{m.group('itembr')}"
            ref = {"doc_id": o[0], "law_name": o[1], "article": article, "paragraph": para, "item": item}
            key = tuple(ref.values())
            if key not in seen and (o[0], article) != (self_doc, self_article):
                seen.add(key)
                (apps if is_app else arts).append(ref)
        else:
            pos += 1
    return arts + apps


def _opener(close: str) -> str:
    return "([{〔"[")]}〕".index(close)]


def _close(text: str, start: int) -> int:
    """start에 있는 여는 괄호의 짝. 못 찾으면 글 끝."""
    depth, pos = 0, start
    while pos < len(text):
        if (m := LABEL_RE.match(text, pos)):
            pos = m.end()
            continue
        depth += (text[pos] in "([{〔") - (text[pos] in ")]}〕")
        if depth == 0:
            return pos
        pos += 1
    return len(text)


def appendix_no(no: str, branch: str | None = None) -> str:
    """'별표 1', '별표 1의2'. 별표번호 '0001', 가지번호 '02' 같은 0 채움도 받는다."""
    n = int(no)
    b = int(branch) if branch and branch.strip().isdigit() else 0
    return f"별표 {n}" + (f"의{b}" if b else "")


def find_delegations(paragraphs: list[dict], header: str) -> list[dict]:
    """'대통령령으로 정하는 …' 같은 위임 문구가 나오는 위치(항·호·목)를 찾는다."""
    found: list[dict] = []

    def scan(text: str, paragraph, item, subitem):
        for m in DELEGATION_RE.finditer(text):
            found.append({
                "paragraph": paragraph, "item": item, "subitem": subitem,
                "phrase": text[max(0, m.start() - 30): m.end() + 20].strip(),
            })

    scan(header, None, None, None)
    for p in paragraphs:
        scan(p["text"], p["no"], None, None)
        for it in p.get("items", []):
            scan(it["text"], p["no"], it["no"], None)
            for sub in it.get("subitems", []):
                scan(sub["text"], p["no"], it["no"], sub["no"])
    return found


def extract_delegated_from(text: str, parent_doc: str) -> list[dict]:
    """시행령 본문에서 '법 제N조…에서 "대통령령으로 정하는 …"' 문형을 찾아 위임한 법 조항을 돌려준다."""
    out, seen = [], set()
    for m in DELEGATED_FROM_RE.finditer(text):
        article = f"제{m.group('art')}조" + (f"의{m.group('br')}" if m.group("br") else "")
        item = m.group("item")
        if item and m.group("itembr"):
            item = f"{item}의{m.group('itembr')}"
        ref = {"doc_id": parent_doc, "article": article,
               "paragraph": int(m.group("para")) if m.group("para") else None, "item": item}
        key = tuple(ref.values())
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out
