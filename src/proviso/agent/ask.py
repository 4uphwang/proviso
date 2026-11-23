"""질문 하나에 답하고 기록을 남긴다. 답 뒤에 확인되지 않은 인용 표시와 고지를 붙인다.

7장부터 질문에 기준일이 있으면 그날 시행 중이던 조문으로 답한다(agent_asof). 검색 설정에 본문 기간 색인이 있어야 한다."""

from __future__ import annotations

from proviso.agent.loop import OllamaChat, run_agent
from proviso.agent.run import TODAY, Retriever, as_of_setup, year_notice
from proviso.agent.tools import Toolbox, load_law
from proviso.answer.run import judge, law_names, present
from proviso.config import ROOT
from proviso.eval.run import latest_snapshot
from proviso.trace.store import save


def ask(conn, question: str, *, retriever_cfg: dict, model: str, seed_search: bool = True) -> tuple[str, str]:
    """(사용자에게 보일 글, trace_id). 검색 캐시는 평가용과 나눈다."""
    law, names = load_law(conn, TODAY), law_names(conn)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "ask_search.json")
    from proviso.answer.guard import REFUSAL, screen

    kind = screen(question)  # 10장: 개인 사안·투자 조언은 모델 전에 거절한다
    if kind:
        return present(REFUSAL[kind], []), ""
    when, law_at, box, system = as_of_setup(conn, question, names, retr, {})
    tr = run_agent(question, OllamaChat(model), box or Toolbox(law, names, retr.search), system=system, seed_search=seed_search)
    r = judge(tr.answer, set(tr.shown), set(law.articles) | set((law_at or law).articles), tr.context, names)
    tid = save(conn, tr, question=question, answer=tr.answer, refused=r["refused"],
               config={"model": model, "seed_search": seed_search, "retriever": retriever_cfg,
                       "as_of": str(when) if when else None}, snapshot_id=latest_snapshot(conn))
    from proviso.retrieve.versions import TextHistory

    note = year_notice(question, when, r["cites"], law_at or law, TextHistory(conn))
    text = present(tr.answer, r["cites"])
    return (text.replace("\n\n※ 이 답변은", f"\n\n{note}\n\n※ 이 답변은", 1) if note else text), tid
