"""proviso 서버(16장). 검색·재정렬 모델과 법령을 한 번만 불러 두고 HTTP로 질문을 받는다. pip install -e '.[serve]'

proviso serve --port 8000
- POST /ask           {"question": "...", "model": 선택} → 답, 인용, trace_id, 실제로 답한 모델, 걸린 초
- GET  /ask/stream?q= 진행 상황을 SSE로 흘려보낸다(검색 → 조문 열기 → 모델 → 답). 토큰 단위가 아니라 단계 단위다
- GET  /health        모델이 올라와 있는지, DB가 붙는지
- GET  /trace/{id}    기록 하나(proviso trace와 같은 내용, JSON)

검색 모델이 맥 GPU(MPS)를 쓰고 DB 연결이 하나라서, 질문은 한 번에 하나씩 처리한다(잠금). 로컬 데모용이다.
PROVISO_OTLP_ENDPOINT를 주면 답할 때마다 기록을 OTel로 내보낸다(15장).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    model: str | None = None


class State:
    def __init__(self, make_asker):
        self.make_asker = make_asker
        self.asker = None
        self.lock = threading.Lock()
        self.started = time.time()
        self.warm_seconds: float | None = None
        self.served = 0


def _step_event(s: dict) -> dict:
    if "tool" in s:
        return {"type": "tool", "tool": s["tool"], "args": s["args"], "shown": [" ".join(k) for k in s["shown"]],
                "seconds": s["seconds"]}
    return {"type": "llm", "input_tokens": s["prompt_tokens"], "output_tokens": s["output_tokens"], "seconds": s["seconds"],
            **({"model": s["model"], "fallback_from": s["fallback_from"]} if s.get("fallback_from") else {})}


def _export(trace_id: str) -> None:
    endpoint = os.environ.get("PROVISO_OTLP_ENDPOINT")
    if not endpoint or not trace_id:
        return
    try:
        from proviso.trace.otel import export_records, load_records, otlp_exporter

        export_records(load_records([trace_id]), otlp_exporter(endpoint))
    except Exception:  # 내보내기가 실패해도 답에는 영향이 없게
        pass


def create_app(make_asker, *, warm: bool = True) -> FastAPI:
    """make_asker: Asker를 만드는 함수(테스트는 가짜를 준다). warm이면 시작할 때 검색 모델을 올린다."""
    st = State(make_asker)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.asker = st.make_asker()
        if warm and hasattr(st.asker, "warm"):
            st.warm_seconds = round(st.asker.warm(), 2)
        yield

    app = FastAPI(title="proviso", summary="금융 법령으로만 답하는 에이전트(법률 자문 아님)", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        db = True
        try:
            st.asker.conn.execute("SELECT 1")
        except Exception:
            db = False
        return {"ok": st.asker is not None and db, "db": db, "model": getattr(st.asker, "model", None),
                "warm_seconds": st.warm_seconds, "served": st.served, "uptime_seconds": round(time.time() - st.started)}

    @app.post("/ask")
    def ask(body: AskIn, background: BackgroundTasks) -> dict[str, Any]:
        with st.lock:
            a = st.asker.answer(body.question, model=body.model)
            st.served += 1
        background.add_task(_export, a.trace_id)
        return asdict(a)

    @app.get("/ask/stream")
    def ask_stream(q: str, model: str | None = None) -> StreamingResponse:
        if not q.strip():
            raise HTTPException(422, "질문이 비어 있음")
        events: queue.Queue = queue.Queue()

        def work():
            with st.lock:
                try:
                    a = st.asker.answer(q, model=model, on_step=lambda s: events.put(_step_event(s)))
                    st.served += 1
                    events.put({"type": "answer", **asdict(a)})
                    _export(a.trace_id)
                except Exception as e:  # 연결을 열어 둔 채 멈추지 않게 오류도 사건으로 보낸다
                    events.put({"type": "error", "error": str(e)[:200]})
                finally:
                    events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def gen():
            yield "event: start\ndata: {}\n\n"
            while (ev := events.get()) is not None:
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/trace/{trace_id}")
    def trace(trace_id: str) -> dict[str, Any]:
        from proviso.trace.otel import load_records

        recs = load_records([trace_id]) if len(trace_id) >= 6 else []
        if len(recs) != 1:
            raise HTTPException(404, "그런 기록이 없음" if not recs else "여러 기록이 맞음. 더 길게 적을 것")
        return recs[0]

    return app


def default_app(retriever_config: str = "config/experiments/t1-asof-rr384.yaml", model: str | None = None) -> FastAPI:
    import yaml

    from proviso.agent.ask import Asker
    from proviso.config import ROOT
    from proviso.db.connect import connect

    cfg = yaml.safe_load((ROOT / retriever_config).read_text(encoding="utf-8"))["retriever"]

    def make():
        conn = connect()
        conn.autocommit = True  # 질문마다 기록을 저장하고, 오래 열어 둔 연결이 트랜잭션에 묶이지 않게
        return Asker(conn, retriever_cfg=cfg, model=model, search_cache=None)

    return create_app(make)
