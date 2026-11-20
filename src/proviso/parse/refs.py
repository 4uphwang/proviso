"""조문 본문에서 다른 조문 참조와 위임 문구를 뽑는다."""

from __future__ import annotations

import re

REF_RE = re.compile(
    r"(?:「(?P<law>[^」]+)」\s*"
    r"|(?P<same>같은\s*법)\s*"
    r"|(?P<this>이\s*법)\s*"
    r"|(?<![가-힣])(?P<parent>법)\s*)?"
    r"제(?P<art>\d+)조(?:의(?P<br>\d+))?"
    r"(?:\s*제(?P<para>\d+)항)?"
    r"(?:\s*제(?P<item>\d+)호(?:의(?P<itembr>\d+))?)?"
)
LAW_NAME_RE = re.compile(r"「(?P<law>[^」]+)」")
APPENDIX_REF_RE = re.compile(r"(?:「(?P<law>[^」]+)」\s*)?(?<![가-힣])별표\s*(?P<no>\d+)(?:의(?P<br>\d+))?")
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
    """본문 속 '제N조…' 참조를 doc_id까지 풀어서 돌려준다.

    - 「법령명」 제N조: name_to_doc로 doc_id를 찾고, 없으면 doc_id=None, law_name에 이름
    - 같은 법 제N조: 바로 앞에 나온 「법령명」
    - (시행령에서) 법 제N조: 모법(parent_doc)
    - 이 법 제N조, 그냥 제N조: 자기 문서
    자기 자신(같은 문서, 같은 조)은 뺀다. 순서를 지키며 중복 제거.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    last_law: str | None = None
    for m in REF_RE.finditer(text):
        # 이 참조 앞에 나온 「법령명」을 기억(같은 법 해석용)
        for nm in LAW_NAME_RE.finditer(text, 0, m.start()):
            last_law = nm.group("law")
        law_name: str | None = None
        if m.group("law"):
            law_name = m.group("law")
            last_law = law_name
        elif m.group("same"):
            law_name = last_law
        if law_name is not None:
            doc_id = name_to_doc.get(law_name)
        elif m.group("parent") and parent_doc:
            doc_id = parent_doc
        else:
            doc_id = self_doc
        article = f"제{m.group('art')}조" + (f"의{m.group('br')}" if m.group("br") else "")
        item = m.group("item")
        if item and m.group("itembr"):
            item = f"{item}의{m.group('itembr')}"
        ref = {
            "doc_id": doc_id,
            "law_name": law_name if doc_id is None else None,
            "article": article,
            "paragraph": int(m.group("para")) if m.group("para") else None,
            "item": item,
        }
        if doc_id == self_doc and article == self_article:
            continue
        key = tuple(ref.values())
        if key not in seen:
            seen.add(key)
            out.append(ref)
    for m in APPENDIX_REF_RE.finditer(text):  # "별표 1", "「…」 별표 2의2"
        law_name = m.group("law")
        doc_id = name_to_doc.get(law_name) if law_name else self_doc
        article = appendix_no(m.group("no"), m.group("br"))
        ref = {"doc_id": doc_id, "law_name": law_name if doc_id is None else None,
               "article": article, "paragraph": None, "item": None}
        key = tuple(ref.values())
        if article != self_article and key not in seen:
            seen.add(key)
            out.append(ref)
    return out


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
