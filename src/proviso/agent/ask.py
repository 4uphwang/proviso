"""질문 하나에 답하고 기록을 남긴다. 답 뒤에 확인되지 않은 인용 표시와 고지를 붙인다.

7장부터 질문에 기준일이 있으면 그날 시행 중이던 조문으로 답한다(agent_asof). 검색 설정에 본문 기간 색인이 있어야 한다."""

from __future__ import annotations

from proviso.agent.loop import chat_client, run_agent
from proviso.agent.resilience import FallbackChat, LLMError
from proviso.agent.run import DEFAULT_MODEL, TODAY, Retriever, as_of_setup, year_notice
from proviso.agent.tools import Toolbox, load_law
from proviso.answer.run import judge, law_names, present
from proviso.config import ROOT
from proviso.eval.run import latest_snapshot
from proviso.trace.store import save


# 9·10장 결과로 고른 질문 하나용 기본 모델: test 핵심어 0.962, 인용 재현율 0.939, 판별기와 함께 거절 정확도 1.000,
# 질문당 약 0.001달러. 키가 없으면 로컬 4b로 답한다. 평가(agent run)의 기본은 비용이 들지 않게 로컬 4b로 둔다.
ASK_MODEL = "openrouter:deepseek/deepseek-v4-flash-0731"
ASK_MAX_TOKENS = 1500  # 9장: API 모델은 500이면 답이 잘린다
# 14장: 기본 모델이 (재시도까지) 실패하면 다른 회사 모델, 그다음 로컬 4b로 답한다. 순서는 9·13장 결과(거절 정확도, 충실도)다.
ASK_FALLBACKS = ["openrouter:z-ai/glm-5.3-flash@low", DEFAULT_MODEL]
UNAVAILABLE = "지금은 답변 모델에 연결하지 못해 답할 수 없습니다. 잠시 뒤 다시 물어 주세요."


def pick_model(model: str | None) -> str:
    """model을 주지 않으면 ASK_MODEL, 오픈라우터 키가 없으면 로컬 4b."""
    if model:
        return model
    import os

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return ASK_MODEL if os.environ.get("OPENROUTER_API_KEY") else DEFAULT_MODEL


def chain(model: str, fallbacks: list[str] | None = None) -> FallbackChat:
    """model 다음에 대체 모델들(같은 것은 뺀다). 로컬 모델은 키가 없어도 된다."""
    names = list(dict.fromkeys([model] + (ASK_FALLBACKS if fallbacks is None else fallbacks)))
    clients = []
    for m in names:
        try:
            clients.append(chat_client(m, ASK_MAX_TOKENS if m.startswith("openrouter:") else 500))
        except RuntimeError:  # 키가 없으면 그 API 모델은 건너뛴다
            continue
    return FallbackChat(clients)


def ask(conn, question: str, *, retriever_cfg: dict, model: str | None = None, seed_search: bool = True) -> tuple[str, str]:
    """(사용자에게 보일 글, trace_id). 검색 캐시는 평가용과 나눈다."""
    model = pick_model(model)
    law, names = load_law(conn, TODAY), law_names(conn)
    retr = Retriever(conn, retriever_cfg, ROOT / "data" / "cache" / "ask_search.json")
    from proviso.answer.guard import REFUSAL, leaked, screen
    from proviso.answer.pii import mask
    from proviso.retrieve.versions import period_starts

    question, _ = mask(question)  # 11장: 개인정보는 모델과 기록에 넘기기 전에 가린다(원문은 저장하지 않는다)
    known = {(d, a) for d, a, _ in period_starts(conn)}
    kind = screen(question, attacks=True, names=names, known=known)  # 10·11장: 모델 전에 거른다
    if kind:
        return present(REFUSAL[kind], []), ""
    when, law_at, box, system = as_of_setup(conn, question, names, retr, {})
    llm = chain(model)
    try:
        tr = run_agent(question, llm, box or Toolbox(law, names, retr.search), system=system, seed_search=seed_search)
    except LLMError:
        return UNAVAILABLE, ""
    if leaked(tr.answer):  # 11장: 시스템 지시문이 답에 나오면 거절로 바꾼다
        tr.answer = REFUSAL["injection"]
    r = judge(tr.answer, set(tr.shown), set(law.articles) | set((law_at or law).articles), tr.context, names)
    tid = save(conn, tr, question=question, answer=tr.answer, refused=r["refused"],
               config={"model": model, "fallbacks": [c.model for c in llm.clients[1:]], "llm_events": llm.events,
                       "seed_search": seed_search, "retriever": retriever_cfg,
                       "as_of": str(when) if when else None}, snapshot_id=latest_snapshot(conn))
    from proviso.retrieve.versions import TextHistory

    note = year_notice(question, when, r["cites"], law_at or law, TextHistory(conn))
    text = present(tr.answer, r["cites"])
    return (text.replace("\n\n※ 이 답변은", f"\n\n{note}\n\n※ 이 답변은", 1) if note else text), tid
