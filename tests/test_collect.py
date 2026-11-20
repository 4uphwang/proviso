import json

import httpx
import pytest

from proviso.collect.law_api import LawApiAuthError, LawApiClient, LawApiError
from proviso.collect.laws import collect_current, pick_current
from proviso.collect.state import StateStore
from proviso.config import ROOT, Sources

SECRET = "secretoc123"
BODY = json.loads((ROOT / "tests" / "fixtures" / "act_efta.json").read_text(encoding="utf-8"))
SEARCH = {"LawSearch": {"law": {"법령명한글": "전자금융거래법", "현행연혁코드": "현행", "법령일련번호": "290247"}}}


def client(handler, **kw):
    return LawApiClient(SECRET, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def test_retry_then_success():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=SEARCH)

    res = client(handler).search_laws("전자금융거래법")
    assert len(calls) == 3 and res[0]["법령일련번호"] == "290247"  # dict 하나도 list로


def test_gives_up_and_redacts_oc():
    def handler(req):
        raise httpx.ConnectError(f"boom {req.url}")

    with pytest.raises(LawApiError) as e:
        client(handler, max_attempts=2).law_body("1")
    assert SECRET not in str(e.value) and "<OC>" in str(e.value)


def test_auth_rejection_stops_immediately():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text="<html>페이지 접속에 실패하였습니다.</html>")

    with pytest.raises(LawApiAuthError):
        client(handler).search_laws("x")
    assert len(calls) == 1


def test_client_error_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(404)

    with pytest.raises(LawApiError):
        client(handler).law_body("1")
    assert len(calls) == 1


def test_pick_current_requires_exact_name():
    rows = [
        {"법령명한글": "전자금융거래법 시행령", "현행연혁코드": "현행", "법령일련번호": "2"},
        {"법령명한글": "전자금융거래법", "현행연혁코드": "현행", "법령일련번호": "1"},
    ]
    assert pick_current(rows, "전자금융거래법")["법령일련번호"] == "1"
    with pytest.raises(LawApiError):
        pick_current(rows, "없는법")


def test_collect_resumes_without_refetch(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("lawSearch.do"):
            return httpx.Response(200, json=SEARCH)
        return httpx.Response(200, json=BODY)

    sources = Sources.model_validate({"laws": [{"id": "전자금융거래법", "type": "act", "query": "전자금융거래법"}]})
    state_path = tmp_path / "state.json"
    current = collect_current(sources, client(handler), tmp_path / "raw", StateStore(state_path), log=lambda m: None)
    assert current == {"전자금융거래법": "290247"} and len(calls) == 2
    assert (tmp_path / "raw" / "law" / "290247.json").exists()
    # 새 프로세스처럼 상태 파일을 다시 읽어도 네트워크를 다시 부르지 않는다
    collect_current(sources, client(handler), tmp_path / "raw", StateStore(state_path), log=lambda m: None)
    assert len(calls) == 2


def test_failure_recorded_and_retried_next_run(tmp_path):
    fail = {"on": True}

    def handler(req):
        if req.url.path.endswith("lawSearch.do"):
            return httpx.Response(200, json=SEARCH)
        return httpx.Response(500) if fail["on"] else httpx.Response(200, json=BODY)

    sources = Sources.model_validate({"laws": [{"id": "전자금융거래법", "type": "act", "query": "전자금융거래법"}]})
    state = StateStore(tmp_path / "state.json")
    collect_current(sources, client(handler, max_attempts=2), tmp_path / "raw", state, log=lambda m: None)
    assert state.status("body:290247") == "failed"
    fail["on"] = False
    collect_current(sources, client(handler), tmp_path / "raw", state, log=lambda m: None)
    assert state.status("body:290247") == "done"
