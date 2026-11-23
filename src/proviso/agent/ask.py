"""질문 하나에 답하고 기록을 남긴다. 답 뒤에 확인되지 않은 인용 표시와 고지를 붙인다."""

from __future__ import annotations

from proviso.agent.loop import OllamaChat, run_agent
from proviso.agent.run import TODAY, Retriever
from proviso.agent.tools import Toolbox, load_law
from proviso.answer.run import judge, law_names, present
from proviso.config import ROOT
from proviso.eval.run import latest_snapshot
from proviso.trace.store import save


def ask(conn, question: str, *, retriever_cfg: dict, model: str, seed_search: bool = True) -> tuple[str, str]:
    """(사용자에게 보일 글, trace_id). 검색 캐시는 평가용과 나눈다."""
    law, names = load_law(conn, TODAY), law_names(conn)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "ask_search.json")
    tr = run_agent(question, OllamaChat(model), Toolbox(law, names, retr.search), seed_search=seed_search)
    r = judge(tr.answer, set(tr.shown), set(law.articles), tr.context, names)
    tid = save(conn, tr, question=question, answer=tr.answer, refused=r["refused"],
               config={"model": model, "seed_search": seed_search, "retriever": retriever_cfg}, snapshot_id=latest_snapshot(conn))
    return present(tr.answer, r["cites"]), tid
