"""질문 하나에 답하고 기록을 남긴다. 답 뒤에 확인되지 않은 인용 표시와 고지를 붙인다.

7장부터 질문에 기준일이 있으면 그날 시행 중이던 조문으로 답한다(agent_asof). 검색 설정에 본문 기간 색인이 있어야 한다."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from proviso.agent.prompt import prompt_version
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
CHECK_MODEL = ASK_MODEL  # 범위 검사기: 질문당 한 번 더, 약 0.0003달러
REWRITE_MODEL = ASK_MODEL  # 후속 질문 다시 쓰기
FOLLOWUP = "rewrite"
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


@dataclass
class Answer:
    text: str                  # 사용자에게 보일 글(인용 확인 표시와 고지 포함)
    trace_id: str = ""
    refused: bool = False
    kind: str = "answer"       # answer | guard | unavailable
    cites: list[dict] = field(default_factory=list)
    model: str = ""            # 실제로 답한 모델(대체가 답했으면 그 모델)
    fallback_from: str = ""
    seconds: float = 0.0       # 질문을 받아 답을 돌려줄 때까지(벽시계)
    asked: str = ""            # 실제로 처리한 질문(후속 질문이면 앞 대화를 보고 고쳐 쓴 것)


def to_turn(a: Answer) -> "Turn":
    """다음 후속 질문에 넘길 앞 대화 한 턴."""
    from proviso.agent.followup import Turn

    cites = list(dict.fromkeys(f"{c['doc']} {c['article']}" for c in a.cites if c.get("doc") and c.get("status") == "shown"))
    return Turn(a.asked, a.text, cites)


class Asker:
    """질문에 답하는 데 필요한 것(법령, 법령 이름, 검색기, 기준일별 법령)을 한 번만 불러 두고 질문마다 쓴다(16장).

    CLI(proviso agent ask)는 질문 하나에 하나를 만들고 버린다. 서버(proviso serve)는 하나를 띄워 두고 계속 쓴다.
    검색·재정렬 모델은 첫 검색 때 올라온다. warm()을 부르면 미리 올린다.
    """

    def __init__(self, conn, *, retriever_cfg: dict, model: str | None = None,
                 search_cache: Path | None = ROOT / "data" / "cache" / "ask_search.json"):
        from proviso.retrieve.versions import TextHistory, period_starts

        self.conn, self.retriever_cfg, self.model = conn, retriever_cfg, pick_model(model)
        self.law, self.names = load_law(conn, TODAY), law_names(conn)
        self.retr = Retriever(conn, retriever_cfg, search_cache)
        self.known = {(d, a) for d, a, _ in period_starts(conn)}
        self.hist = TextHistory(conn)
        self.laws_at: dict = {}
        self._checker = None
        self._rewriter = None

    def _offtopic(self, question: str, answer: str) -> str:
        """범위 검사(answer/offtopic.py). 걸리면 '해 준 다른 일', 아니면 빈 글. 오픈라우터 키가 없으면 건너뛴다.
        검사기가 (재시도까지) 실패하면 막는다(fail closed)."""
        from proviso.answer.cite import is_refusal
        from proviso.answer.offtopic import check

        if is_refusal(answer):
            return ""
        if self._checker is None:
            try:
                self._checker = chat_client(CHECK_MODEL, 200)
            except RuntimeError:
                self._checker = False
        if not self._checker:
            return ""
        try:
            v = check(self._checker, question, answer)
        except LLMError:
            return "검사기 연결 실패"
        return (v["what"] or "범위 밖 요청") if v["extra"] else ""

    def _standalone(self, question: str, history: list, strategy: str) -> tuple[str, dict]:
        """후속 질문 다시 쓰기(agent/followup.py). 키가 없으면 앞 질문을 붙이는 방법으로 대신한다."""
        from proviso.agent.followup import standalone

        if strategy == "rewrite" and self._rewriter is None:
            try:
                self._rewriter = chat_client(REWRITE_MODEL, 300)
            except RuntimeError:
                self._rewriter = False
        if strategy == "rewrite" and not self._rewriter:
            strategy = "concat"
        return standalone(question, history, strategy, self._rewriter)

    def warm(self) -> float:
        """검색·재정렬 모델을 올리고 걸린 초를 돌려준다."""
        t0 = time.perf_counter()
        self.retr.r.search("금융", k=1)
        return time.perf_counter() - t0

    def answer(self, question: str, *, model: str | None = None, seed_search: bool = True,
               on_step: Callable[[dict], None] | None = None, history: list | None = None,
               followup: str = FOLLOWUP) -> Answer:
        """history: 앞 대화(followup.Turn 목록, to_turn으로 만든다). 있으면 후속 질문을 혼자 읽히는 질문으로 고쳐 쓴 뒤 답한다."""
        from proviso.answer.guard import REFUSAL, leaked, screen, strip_code, unverified
        from proviso.answer.normalize import normalize
        from proviso.answer.offtopic import REFUSAL_EXTRA
        from proviso.answer.pii import mask

        t0 = time.perf_counter()
        model = pick_model(model or self.model)
        question, _ = mask(normalize(question))  # 11장: 개인정보는 모델과 기록에 넘기기 전에 가린다(원문은 저장하지 않는다)
        kind = screen(question, attacks=True, names=self.names, known=self.known)  # 10·11장: 모델 전에 거른다
        if kind:
            return Answer(present(REFUSAL[kind], []), refused=True, kind="guard", seconds=time.perf_counter() - t0, asked=question)
        said, rewrite = question, {}
        if history:  # 후속 질문: 고쳐 쓴 질문도 같은 검사를 다시 거친다(앞 대화에서 끌어온 말이 있을 수 있다)
            question, rewrite = self._standalone(said, history, followup)
            question, _ = mask(normalize(question))
            kind = screen(question, attacks=True, names=self.names, known=self.known)
            if kind:
                return Answer(present(REFUSAL[kind], []), refused=True, kind="guard", seconds=time.perf_counter() - t0, asked=question)
        when, law_at, box, system, note = as_of_setup(self.conn, question, self.names, self.retr, self.laws_at)
        llm = chain(model)
        try:
            tr = run_agent(question, llm, box or Toolbox(self.law, self.names, self.retr.search), system=system,
                           seed_search=seed_search, on_step=on_step, note=note)
        except LLMError:
            return Answer(UNAVAILABLE, kind="unavailable", seconds=time.perf_counter() - t0)
        if leaked(tr.answer):  # 11장: 시스템 지시문이 답에 나오면 거절로 바꾼다
            tr.answer = REFUSAL["injection"]
        elif unverified(tr.answer, self.names, set(tr.shown)):  # 이번에 열어 본 조문을 하나도 인용하지 않은 답은 내보내지 않는다
            tr.answer = REFUSAL["uncited"]
        tr.answer, _ = strip_code(tr.answer)  # 법령 답에는 코드가 필요 없다(범위 제한)
        extra = self._offtopic(question if said == question else f"{said}\n(앞 대화를 보고 고쳐 쓴 질문: {question})", tr.answer)
        if extra:  # 답이 질문 속 범위 밖 부탁(가사, 지정 문구, 수식 등)을 따랐으면 내보내지 않는다
            tr.answer = REFUSAL_EXTRA
        law = self.law
        r = judge(tr.answer, set(tr.shown), set(law.articles) | set((law_at or law).articles), tr.context, self.names)
        tid = save(self.conn, tr, question=question, answer=tr.answer, refused=r["refused"],
                   config={"model": model, "prompt_version": prompt_version(), "fallbacks": [c.model for c in llm.clients[1:]], "llm_events": llm.events,
                           "seed_search": seed_search, "retriever": self.retriever_cfg,
                           **({"followup": {**rewrite, "said": said, "turns": len(history)}} if history else {}),
                           "as_of": str(when) if when else None}, snapshot_id=latest_snapshot(self.conn))
        note = year_notice(question, when, r["cites"], law_at or law, self.hist)
        text = present(tr.answer, r["cites"])
        if note:
            text = text.replace("\n\n※ 이 답변은", f"\n\n{note}\n\n※ 이 답변은", 1)
        used = [s for s in tr.steps if s.get("llm")]
        last = used[-1] if used else {}
        return Answer(text, tid, r["refused"], "answer", r["cites"], last.get("model") or llm.clients[0].model,
                      last.get("fallback_from", ""), time.perf_counter() - t0, question)


def ask(conn, question: str, *, retriever_cfg: dict, model: str | None = None, seed_search: bool = True) -> tuple[str, str]:
    """(사용자에게 보일 글, trace_id). CLI용: 질문 하나에 Asker 하나(모델을 매번 새로 불러온다)."""
    a = Asker(conn, retriever_cfg=retriever_cfg, model=model).answer(question, seed_search=seed_search)
    return a.text, a.trace_id
