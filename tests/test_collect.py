import json

import httpx
import pytest

from proviso.collect.law_api import LawApiAuthError, LawApiClient, LawApiError
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


def _eflaw_rows():
    return [
        {"법령명한글": "전자금융거래법", "법령일련번호": "280277", "시행일자": "20261217", "공포일자": "20251216",
         "제개정구분명": "일부개정", "현행연혁코드": "시행예정"},
        {"법령명한글": "전자금융거래법", "법령일련번호": "290247", "시행일자": "20261002", "공포일자": "20260929",
         "제개정구분명": "일부개정", "현행연혁코드": "현행"},
        {"법령명한글": "전자금융거래법", "법령일련번호": "280277", "시행일자": "20251216", "공포일자": "20251216",
         "제개정구분명": "일부개정", "현행연혁코드": "연혁"},
        {"법령명한글": "전자금융거래법 시행령", "법령일련번호": "285727", "시행일자": "20260428", "공포일자": "20260428",
         "제개정구분명": "일부개정", "현행연혁코드": "현행"},
    ]


def test_version_rows_filter_and_sort():
    from proviso.collect.laws import version_rows
    vs = version_rows(_eflaw_rows(), "전자금융거래법")
    assert [(v["mst"], v["effective"]) for v in vs] == [("280277", "20251216"), ("290247", "20261002"), ("280277", "20261217")]
    old = version_rows([{**_eflaw_rows()[0], "법령명한글": "신용정보의이용및보호에관한법률"}], "신용정보의 이용 및 보호에 관한 법률")
    assert len(old) == 1  # 옛 법령명의 띄어쓰기 차이는 무시


def _handler(calls, fail_body=None):
    def handler(req):
        calls.append(req.url.path + "?" + req.url.params.get("target", "") + req.url.params.get("page", ""))
        if req.url.path.endswith("lawSearch.do"):
            page = int(req.url.params.get("page", "1"))
            rows = _eflaw_rows()[:2] if page == 1 else _eflaw_rows()[2:]  # 2페이지에 나눠 응답
            return httpx.Response(200, json={"LawSearch": {"totalCnt": "4", "law": rows}})
        if fail_body and fail_body["on"]:
            return httpx.Response(500)
        return httpx.Response(200, json=BODY)
    return handler


SRC = Sources.model_validate({"laws": [{"id": "전자금융거래법", "type": "act", "query": "전자금융거래법"}]})


def test_collect_current_scope_pages_and_resumes(tmp_path):
    from proviso.collect.laws import collect_versions
    calls = []
    state_path = tmp_path / "state.json"
    vs = collect_versions(SRC, client(_handler(calls)), tmp_path / "raw", StateStore(state_path), log=lambda m: None)
    assert len(vs["전자금융거래법"]) == 3
    bodies = sorted(p.name for p in (tmp_path / "raw" / "eflaw").iterdir())
    assert bodies == ["280277_20261217.json", "290247_20261002.json"]  # 현행 + 시행예정만
    n = len(calls)
    collect_versions(SRC, client(_handler(calls)), tmp_path / "raw", StateStore(state_path), log=lambda m: None)
    assert all("lawService" not in c for c in calls[n:])  # 다시 실행하면 본문은 다시 받지 않음(목록만 갱신)


def test_collect_all_and_retry_failed(tmp_path):
    from proviso.collect.laws import collect_versions
    fail = {"on": True}
    state = StateStore(tmp_path / "state.json")
    collect_versions(SRC, client(_handler([], fail), max_attempts=2), tmp_path / "raw", state, scope="all", log=lambda m: None)
    assert state.status("body:280277_20251216") == "failed"
    fail["on"] = False
    collect_versions(SRC, client(_handler([], fail)), tmp_path / "raw", state, scope="all", log=lambda m: None)
    assert state.status("body:280277_20251216") == "done"
    assert len(list((tmp_path / "raw" / "eflaw").iterdir())) == 3


def test_response_echoing_oc_is_redacted():
    def handler(req):
        return httpx.Response(200, json={"LawSearch": {"totalCnt": "1", "law": [
            {"법령상세링크": f"/DRF/lawService.do?OC={SECRET}&target=law&MST=1"}]}})

    _, rows = client(handler).eflaw_search("x")
    assert SECRET not in json.dumps(rows) and "OC=REDACTED" in rows[0]["법령상세링크"]
