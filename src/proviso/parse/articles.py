"""법령 본문 JSON(lawService.do target=law)을 조(條) 단위 레코드로 바꾼다.

원문 필드는 API가 준 문자열을 그대로 쓰고(앞뒤 공백만 정리), 조문내용·항·호·목을 줄바꿈으로 이어 붙인다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from proviso.parse.refs import appendix_no, extract_delegated_from, extract_refs, find_delegations

CIRCLED = {chr(0x2460 + i): i + 1 for i in range(20)}  # ①~⑳
CIRCLED.update({chr(0x3251 + i): i + 21 for i in range(15)})  # ㉑~㉟
DELETED_RE = re.compile(r"^제\d+조(의\d+)?\s*삭제")
BOX_CHARS_RE = re.compile(r"[\u2500-\u257F]+")  # ┏━┯┃│┠─┼ 등 표 테두리
APPENDIX_SORT_BASE = 1_000_000  # 별표는 조문 뒤에 정렬


@dataclass
class Version:
    doc_id: str
    mst: str
    title: str
    law_id: str
    promulgated_on: date | None
    effective_on: date
    revision_kind: str
    alias: str  # 법령명약칭(없으면 빈 문자열)


@dataclass
class Article:
    doc_id: str
    article_no: str
    sort_key: int
    title: str | None
    text: str
    paragraphs: list[dict]
    heading: str | None  # 속한 장·절 제목
    note: str | None  # 조문참고자료(예: [본조신설 …], [시행일: …])
    deleted: bool
    kind: str = "article"  # 'article' | 'appendix'(별표)
    search_text: str | None = None  # 색인용 파생 텍스트(별표: 테두리 문자 제거). 원문은 text
    refs: list[dict] = field(default_factory=list)
    delegations: list[dict] = field(default_factory=list)  # 법률: 위임 문구 위치
    delegated_from: list[dict] = field(default_factory=list)  # 시행령: 위임한 법 조항

    @property
    def text_id(self) -> str:
        h = hashlib.sha256(f"{self.doc_id}\x1f{self.article_no}\x1f{self.text}".encode()).hexdigest()
        return h[:32]

    def to_dict(self) -> dict:
        return {**asdict(self), "text_id": self.text_id}


def _ymd(s: str | None) -> date | None:
    s = (s or "").strip()
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 and s.isdigit() else None


def _as_list(x) -> list:
    if x is None or x == "":
        return []
    return x if isinstance(x, list) else [x]


def _clean(s) -> str:
    if isinstance(s, list):  # 드물게 여러 줄이 list로 옴
        s = "\n".join(str(x) for x in s)
    return (s or "").strip()


def _num(raw: str | None, branch: str | None = None) -> str:
    n = (raw or "").strip().rstrip(".")
    if branch and branch not in ("", "0"):
        n = f"{n}의{branch}"
    return n


def _paragraphs(unit: dict) -> tuple[list[dict], list[str]]:
    paras, lines = [], []
    for p in _as_list(unit.get("항")):
        raw_no = (p.get("항번호") or "").strip()
        no = CIRCLED.get(raw_no[:1]) if raw_no else None
        ptext = _clean(p.get("항내용"))
        if ptext:
            lines.append(ptext)
        items = []
        for it in _as_list(p.get("호")):
            itext = _clean(it.get("호내용"))
            lines.append(itext)
            subs = []
            for sub in _as_list(it.get("목")):
                stext = _clean(sub.get("목내용"))
                lines.append(stext)
                subs.append({"no": _num(sub.get("목번호")), "text": stext})
            items.append({"no": _num(it.get("호번호"), it.get("호가지번호")), "text": itext, "subitems": subs})
        paras.append({"no": no, "text": ptext, "items": items})
    return paras, lines


def _flatten_lines(x) -> list[str]:
    if isinstance(x, str):
        return x.split("\n")
    out: list[str] = []
    for y in x or []:
        out.extend(_flatten_lines(y))
    return out


def flatten_table(text: str) -> str:
    """별표의 표 테두리 문자를 지우고 공백을 줄여 검색용 텍스트를 만든다(원문은 따로 보관)."""
    lines = [re.sub(r"\s+", " ", BOX_CHARS_RE.sub(" ", ln)).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_appendices(law: dict, doc_id: str, *, parent_doc: str | None, name_to_doc: dict[str, str]) -> list[Article]:
    """본문 JSON의 '별표' 필드(별표구분='별표')를 조문과 같은 단위의 레코드로 만든다. 서식은 뺀다."""
    out: list[Article] = []
    for u in _as_list((law.get("별표") or {}).get("별표단위")):
        if (u.get("별표구분") or "별표") != "별표":
            continue
        no = appendix_no(u.get("별표번호") or "0", u.get("별표가지번호"))
        text = "\n".join(ln.rstrip() for ln in _flatten_lines(u.get("별표내용"))).strip("\n")
        n = int(u.get("별표번호") or 0)
        b = int(u["별표가지번호"]) if (u.get("별표가지번호") or "").strip().isdigit() else 0
        art = Article(
            doc_id=doc_id, article_no=no, sort_key=APPENDIX_SORT_BASE + n * 100 + b,
            title=_clean(u.get("별표제목")) or None, text=text, paragraphs=[], heading=None,
            note=None, deleted=False, kind="appendix", search_text=flatten_table(text),
        )
        art.refs = extract_refs(f"{art.title or ''}\n{text}", self_doc=doc_id, self_article=no,
                                parent_doc=parent_doc, name_to_doc=name_to_doc)
        out.append(art)
    return out


def parse_law(raw: dict, doc_id: str, *, mst: str, parent_doc: str | None, name_to_doc: dict[str, str]) -> tuple[Version, list[Article]]:
    law = raw["법령"]
    info = law["기본정보"]
    version = Version(
        doc_id=doc_id,
        mst=mst,
        title=_clean(info.get("법령명_한글")),
        law_id=_clean(info.get("법령ID")),
        promulgated_on=_ymd(info.get("공포일자")),
        effective_on=_ymd(info.get("시행일자")),
        revision_kind=_clean(info.get("제개정구분")),
        alias=_clean(info.get("법령명약칭")),
    )
    articles: list[Article] = []
    heading: str | None = None
    for unit in _as_list(law.get("조문", {}).get("조문단위")):
        content = _clean(unit.get("조문내용"))
        if unit.get("조문여부") == "전문":
            heading = content or heading
            continue
        n = int(unit["조문번호"])
        br = (unit.get("조문가지번호") or "").strip()
        br_n = int(br) if br.isdigit() else 0
        article_no = f"제{n}조" + (f"의{br_n}" if br_n else "")
        paras, lines = _paragraphs(unit)
        text = "\n".join([content, *lines]) if lines else content
        art = Article(
            doc_id=doc_id,
            article_no=article_no,
            sort_key=n * 100 + br_n,
            title=_clean(unit.get("조문제목")) or None,
            text=text,
            paragraphs=paras,
            heading=heading,
            note=_clean(unit.get("조문참고자료")) or None,
            deleted=bool(DELETED_RE.match(content)),
        )
        art.refs = extract_refs(text, self_doc=doc_id, self_article=article_no, parent_doc=parent_doc, name_to_doc=name_to_doc)
        # 시행령 본문의 "대통령령이 정하는 …"은 법 문구를 인용한 것이라 위임 문구로 보지 않는다
        art.delegations = [] if art.deleted or parent_doc else find_delegations(paras, content)
        art.delegated_from = extract_delegated_from(text, parent_doc) if parent_doc and not art.deleted else []
        articles.append(art)
    articles.extend(parse_appendices(law, doc_id, parent_doc=parent_doc, name_to_doc=name_to_doc))
    return version, articles
