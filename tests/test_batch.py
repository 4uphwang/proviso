"""19장 배치 API: 모의 서버로 맡기기 → 상태 보기 → 결과를 custom_id로 맞추기. 실제 API는 부르지 않는다."""

import json

import httpx
import pytest

from proviso.agent.batch import BatchClient, content_of


def fake_server(fail_id=None):
    state = {"polls": 0, "submitted": None}

    def handle(req):
        if req.method == "POST" and req.url.path.endswith("/batches"):
            body = json.loads(req.content)
            assert list(body)[:2] == ["endpoint", "model"]  # 문서: endpoint·model이 requests보다 앞
            assert all("model" not in r["body"] for r in body["requests"])
            state["submitted"] = body
            return httpx.Response(202, json={"id": "batch_1", "status": "validating"})
        if req.method == "GET" and req.url.path.endswith("/batches/batch_1"):
            state["polls"] += 1
            if state["polls"] < 3:
                return httpx.Response(200, json={"id": "batch_1", "status": "in_progress",
                                                 "request_counts": {"total": 2, "completed": 0, "failed": 0}})
            results = []
            for r in reversed(state["submitted"]["requests"]):  # 결과 순서는 맡긴 순서와 다르다
                if r["custom_id"] == fail_id:
                    results.append({"custom_id": r["custom_id"], "response": None,
                                    "error": {"type": "error", "message": "bad row"}})
                else:
                    text = r["body"]["messages"][0]["content"].upper()
                    results.append({"custom_id": r["custom_id"], "error": None,
                                    "response": {"status_code": 200, "body": {"choices": [{"message": {"content": text}}]}}})
            return httpx.Response(200, json={"id": "batch_1", "status": "completed", "results": results,
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.0001},
                                             "request_counts": {"total": 2, "completed": 2, "failed": 0}})
        return httpx.Response(404, json={"error": {"message": "no"}})
    return httpx.MockTransport(handle), state


@pytest.fixture
def client(monkeypatch):
    def make(fail_id=None):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
        t, state = fake_server(fail_id)
        now = [0.0]
        return BatchClient(transport=t, sleep=lambda d: now.__setitem__(0, now[0] + d), clock=lambda: now[0]), state
    return make


def test_submit_wait_and_match_by_custom_id(client):
    c, state = client()
    bid = c.submit("z-ai/glm-5.3-flash", [("a", {"model": "x", "messages": [{"role": "user", "content": "one"}]}),
                                          ("b", {"messages": [{"role": "user", "content": "two"}]})])
    res = c.wait(bid, every=30, progress=lambda m: None)
    assert res.status == "completed" and res.polls == 3 and res.seconds == 60
    assert content_of(res.bodies["a"]) == "ONE" and content_of(res.bodies["b"]) == "TWO"
    assert res.usage["cost"] == 0.0001


def test_failed_row_is_kept_as_error(client):
    c, _ = client(fail_id="b")
    bid = c.submit("m", [("a", {"messages": [{"role": "user", "content": "x"}]}),
                         ("b", {"messages": [{"role": "user", "content": "y"}]})])
    res = c.wait(bid, every=1, progress=lambda m: None)
    assert set(res.bodies) == {"a"} and res.errors["b"] == "bad row"


def test_duplicate_ids_rejected_and_key_hidden(client, monkeypatch):
    c, _ = client()
    with pytest.raises(ValueError):
        c.submit("m", [("a", {}), ("a", {})])
    with pytest.raises(RuntimeError) as e:
        c.get("nope")
    assert "sk-test-secret" not in str(e.value)
