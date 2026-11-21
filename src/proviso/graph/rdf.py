"""법령 관계를 RDF로 내보내고(OWL 스키마: ontology/proviso.ttl), OWL 2 RL 규칙으로 추론한 뒤
SPARQL로 물어 SQL과 같은 답이 나오는지 확인한다.

검색·에이전트는 계속 Postgres를 쓴다. 이 그래프는 같은 구조를 표준 어휘(ELI)로 표현하고,
역관계·전이·속성 사슬 같은 규칙을 데이터 대신 스키마에 적어 두는 용도다.

조문 버전은 시행일 구간(article_spans) 중 본문이 같은 연속 구간을 하나로 이어 붙인다.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef

from proviso.config import ROOT

PV = Namespace("urn:proviso:ns#")
ELI = Namespace("http://data.europa.eu/eli/ontology#")
SCHEMA = ROOT / "ontology" / "proviso.ttl"


def law_uri(doc_id: str) -> URIRef:
    return URIRef("urn:proviso:law:" + quote(doc_id))


def art_uri(doc_id: str, article_no: str) -> URIRef:
    return URIRef(f"{law_uri(doc_id)}/{quote(article_no)}")


def ver_uri(doc_id: str, article_no: str, valid_from: date) -> URIRef:
    return URIRef(f"{art_uri(doc_id, article_no)}@{valid_from}")


def _versions(conn):
    """(doc, 조)마다 본문이 같은 연속 구간을 이어 붙인 버전 목록."""
    rows = conn.execute(
        "SELECT s.doc_id, s.article_no, s.text_id, s.valid_from, s.valid_to, t.kind, t.article_title, t.deleted"
        " FROM article_spans s JOIN article_texts t USING (text_id) ORDER BY s.doc_id, s.sort_key, s.article_no,"
        " s.valid_from").fetchall()
    out: dict[tuple, list[dict]] = {}
    for doc, art, tid, vf, vt, kind, title, deleted in rows:
        vs = out.setdefault((doc, art), [])
        if vs and vs[-1]["text_id"] == tid and vs[-1]["valid_to"] == vf:
            vs[-1]["valid_to"] = vt
        else:
            vs.append({"text_id": tid, "valid_from": vf, "valid_to": vt, "kind": kind, "title": title,
                       "deleted": deleted})
    return out


def build_graph(conn, *, with_schema: bool = True) -> Graph:
    g = Graph()
    g.bind("pv", PV)
    g.bind("eli", ELI)
    if with_schema:
        g.parse(SCHEMA, format="turtle")
    for doc_id, doc_type, parent, title in conn.execute(
            "SELECT doc_id, doc_type, parent_doc_id, title FROM documents WHERE doc_type IN ('act', 'decree')"):
        d = law_uri(doc_id)
        g.add((d, RDF.type, PV.Act if doc_type == "act" else PV.Decree))
        g.add((d, ELI.title, Literal(title, lang="ko")))
        if parent:
            g.add((d, ELI.based_on, law_uri(parent)))

    text_owner: dict[str, tuple] = {}
    for (doc_id, art), vs in _versions(conn).items():
        a = art_uri(doc_id, art)
        g.add((a, RDF.type, PV.Appendix if vs[0]["kind"] == "appendix" else PV.Article))
        g.add((a, ELI.is_part_of, law_uri(doc_id)))
        prev = None
        for v in vs:
            text_owner[v["text_id"]] = (doc_id, art)
            u = ver_uri(doc_id, art, v["valid_from"])
            g.add((u, RDF.type, PV.ArticleVersion))
            g.add((a, PV.hasVersion, u))
            g.add((u, ELI.first_date_entry_in_force, Literal(v["valid_from"], datatype=XSD.date)))
            if v["valid_to"]:
                g.add((u, ELI.date_no_longer_in_force, Literal(v["valid_to"], datatype=XSD.date)))
            if v["title"]:
                g.add((u, ELI.title, Literal(v["title"], lang="ko")))
            if v["deleted"]:
                g.add((u, PV.deleted, Literal(True)))
            if prev is not None:
                g.add((prev, PV.nextVersion, u))
            prev = u

    arts = {k for k in text_owner.values()}
    for f, t in conn.execute("SELECT from_text_id, to_text_id FROM delegations"):
        if f in text_owner and t in text_owner:
            g.add((art_uri(*text_owner[f]), PV.delegatesTo, art_uri(*text_owner[t])))
    for tid, doc_id, refs in conn.execute("SELECT text_id, doc_id, refs FROM article_texts WHERE refs <> '[]'"):
        if tid not in text_owner:
            continue
        src = art_uri(*text_owner[tid])
        for r in refs:
            if r.get("doc_id") and (r["doc_id"], r.get("article")) in arts and (r["doc_id"], r["article"]) != text_owner[tid]:
                g.add((src, ELI.cites, art_uri(r["doc_id"], r["article"])))
    return g


def reason(g: Graph) -> float:
    """OWL 2 RL 규칙(역관계, 전이, 하위 클래스·속성, 속성 사슬)으로 추론한 사실을 그래프에 더한다. 걸린 초를 돌려준다."""
    import owlrl

    t0 = time.perf_counter()
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(g)
    return round(time.perf_counter() - t0, 1)


def export(conn, out: Path) -> tuple[int, Path]:
    g = build_graph(conn, with_schema=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(out, format="turtle")
    return len(g), out


PREFIX = "PREFIX pv: <urn:proviso:ns#>\nPREFIX eli: <http://data.europa.eu/eli/ontology#>\n" \
         "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"


def _one(g: Graph, q: str) -> int:
    return int(next(iter(g.query(PREFIX + q)))[0])


def in_force_sparql(day: date) -> str:
    return f"""SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE {{
  ?a pv:hasVersion ?v . ?v eli:first_date_entry_in_force ?f .
  FILTER(?f <= "{day}"^^xsd:date)
  OPTIONAL {{ ?v eli:date_no_longer_in_force ?t }}
  FILTER(!BOUND(?t) || "{day}"^^xsd:date < ?t)
  FILTER NOT EXISTS {{ ?v pv:deleted true }} }}"""


def checks(conn, g: Graph) -> list[tuple[str, int, int]]:
    """(무엇을, SPARQL 답, SQL 답). 추론이 끝난 그래프에서 돌린다. 역관계·사슬·전이로 '적지 않은' 사실을 묻는다."""
    out = []
    sql_pairs = conn.execute(
        "SELECT count(DISTINCT (f.doc_id, f.article_no, t.doc_id, t.article_no)) FROM delegations d"
        " JOIN article_texts f ON f.text_id = d.from_text_id JOIN article_texts t ON t.text_id = d.to_text_id"
        " WHERE EXISTS (SELECT 1 FROM article_spans s WHERE s.text_id = f.text_id)"
        " AND EXISTS (SELECT 1 FROM article_spans s WHERE s.text_id = t.text_id)").fetchone()[0]
    out.append(("위임받음(delegatedFrom, 역관계로 추론) 쌍 수",
                _one(g, "SELECT (COUNT(*) AS ?n) WHERE { ?d pv:delegatedFrom ?l }"), sql_pairs))
    sql_impl = conn.execute(
        "SELECT count(DISTINCT (t.doc_id, t.article_no, f.doc_id)) FROM delegations d"
        " JOIN article_texts f ON f.text_id = d.from_text_id JOIN article_texts t ON t.text_id = d.to_text_id"
        " WHERE EXISTS (SELECT 1 FROM article_spans s WHERE s.text_id = f.text_id)"
        " AND EXISTS (SELECT 1 FROM article_spans s WHERE s.text_id = t.text_id)").fetchone()[0]
    out.append(("구체화하는 법률(implementsAct, 속성 사슬로 추론) 쌍 수",
                _one(g, "SELECT (COUNT(*) AS ?n) WHERE { ?d pv:implementsAct ?act . ?act a pv:Act }"), sql_impl))
    sql_later = conn.execute(
        "WITH v AS (SELECT doc_id, article_no, text_id, valid_from, lag(text_id) OVER w AS prev_t,"
        "  lag(valid_to) OVER w AS prev_to FROM article_spans WINDOW w AS (PARTITION BY doc_id, article_no"
        "  ORDER BY valid_from)),"
        " n AS (SELECT doc_id, article_no, count(*) FILTER (WHERE prev_t IS DISTINCT FROM text_id"
        "  OR prev_to IS DISTINCT FROM valid_from) AS k FROM v GROUP BY 1, 2)"
        " SELECT coalesce(sum(k * (k - 1) / 2), 0) FROM n").fetchone()[0]
    out.append(("이후 버전(laterVersion, 전이로 추론) 쌍 수",
                _one(g, "SELECT (COUNT(*) AS ?n) WHERE { ?a pv:laterVersion ?b }"), int(sql_later)))
    for day in (date(2026, 11, 20), date(2019, 1, 1)):
        sql_n = conn.execute(
            "SELECT count(DISTINCT (s.doc_id, s.article_no)) FROM article_spans s JOIN article_texts t USING (text_id)"
            " WHERE s.valid_from <= %(d)s AND (s.valid_to IS NULL OR %(d)s < s.valid_to) AND NOT t.deleted",
            {"d": day}).fetchone()[0]
        out.append((f"{day}에 시행 중인 조문 수", _one(g, in_force_sparql(day)), sql_n))
    sql_res = conn.execute("SELECT (SELECT count(*) FROM documents WHERE doc_type IN ('act','decree'))"
                           " + (SELECT count(DISTINCT (doc_id, article_no)) FROM article_spans)").fetchone()[0]
    out.append(("법령 자원(LegalResource, 하위 클래스로 추론) 수",
                _one(g, "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a eli:LegalResource }"), sql_res))
    return out
