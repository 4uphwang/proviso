"""16장 서버: 가짜 Asker로 엔드포인트만 본다(모델·DB 없이)."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from proviso.agent.ask import Answer  # noqa: E402
from proviso.serve import create_app  # noqa: E402


class FakeConn:
    def execute(self, sql):
        return None


class FakeAsker:
    model = "fake/model"
    conn = FakeConn()

    def __init__(self):
        self.made = 0
        self.histories = []

    def warm(self):
        return 1.5

    def answer(self, question, *, model=None, on_step=None, history=None):
        self.histories.append([t.question for t in history or []])
        if on_step:
            on_step({"tool": "search", "args": {"query": question}, "shown": [("법", "제1조")], "seconds": 0.1,
                     "chars": 10, "error": False, "repeat": False})
            on_step({"llm": True, "prompt_tokens": 5, "output_tokens": 2, "seconds": 0.2})
        return Answer(f"답: {question}", "tid-1", False, "answer", [{"doc": "법", "article": "제1조", "status": "shown"}],
                      model or self.model, "", 0.3, asked=question)


@pytest.fixture
def client():
    with TestClient(create_app(FakeAsker)) as c:
        yield c


def test_health_reports_warmup(client):
    h = client.get("/health").json()
    assert h["ok"] and h["warm_seconds"] == 1.5 and h["served"] == 0


def test_ask_returns_answer_and_counts(client):
    r = client.post("/ask", json={"question": "질문"}).json()
    assert r["text"] == "답: 질문" and r["trace_id"] == "tid-1" and r["model"] == "fake/model"
    assert client.get("/health").json()["served"] == 1


def test_ask_rejects_empty(client):
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_stream_sends_steps_then_answer(client):
    with client.stream("GET", "/ask/stream", params={"q": "질문"}) as r:
        body = "".join(r.iter_text())
    kinds = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert kinds == ["start", "tool", "llm", "answer"]
    last = json.loads([line for line in body.splitlines() if line.startswith("data: ")][-1][6:])
    assert last["text"] == "답: 질문"


def test_trace_not_found(client):
    assert client.get("/trace/zzzzzzzz").status_code == 404


def test_conversation_id_carries_history(client):
    a = client.post("/ask", json={"question": "첫 질문"}).json()
    b = client.post("/ask", json={"question": "그럼 과태료는?", "conversation_id": a["conversation_id"]}).json()
    assert b["conversation_id"] == a["conversation_id"]
    other = client.post("/ask", json={"question": "새 질문"}).json()
    assert other["conversation_id"] != a["conversation_id"]


def test_unknown_conversation_id_starts_new():
    asker = FakeAsker()
    with TestClient(create_app(lambda: asker)) as c:
        a = c.post("/ask", json={"question": "첫 질문"}).json()
        c.post("/ask", json={"question": "그럼?", "conversation_id": a["conversation_id"]})
        c.post("/ask", json={"question": "다른 거", "conversation_id": "made-up"})
    assert asker.histories == [[], ["첫 질문"], []]
