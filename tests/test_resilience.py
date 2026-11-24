"""14장 장애 주입: 모의 서버가 429·500·시간 초과·400을 내게 해서 재시도·대체·차단기·속도 제한을 확인한다. 실제 API는 부르지 않는다."""

import httpx
import pytest

from proviso.agent.loop import OpenAIChat
from proviso.agent.resilience import FallbackChat, LLMError, RateLimiter, RetryPolicy, with_retry

OK = {"choices": [{"message": {"content": "답"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.0}}
MSG = [{"role": "user", "content": "질문"}]


def server(script):
    """script: 호출마다 낼 것(int 상태 코드, 'timeout', dict 응답). 다 쓰면 마지막 것을 되풀이한다."""
    seen = []

    def handle(req):
        x = script[min(len(seen), len(script) - 1)]
        seen.append(req)
        if x == "timeout":
            raise httpx.ReadTimeout("느림", request=req)
        if isinstance(x, tuple):
            return httpx.Response(x[0], headers=x[1], json={"error": "x"})
        if isinstance(x, int):
            return httpx.Response(x, json={"error": "x"})
        return httpx.Response(200, json=x)
    return httpx.MockTransport(handle), seen


def client(tmp_path, monkeypatch, script, model="m/a", retries=3):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    waits = []
    t, seen = server(script)
    c = OpenAIChat(model, cache=tmp_path / model.replace("/", "_"), transport=t, per_minute=10_000,
                   policy=RetryPolicy(retries=retries, sleep=waits.append))
    return c, seen, waits


def test_429_then_ok_retries_and_honors_retry_after(tmp_path, monkeypatch):
    c, seen, waits = client(tmp_path, monkeypatch, [(429, {"retry-after": "7"}), 500, OK])
    rep = c.chat(MSG, None)
    assert rep.content == "답" and rep.attempts == 3 and len(seen) == 3
    assert waits[0] == 7 and 2 <= waits[1] <= 2.5  # Retry-After, 그다음 지수 백오프(2초 + 흔들림)


def test_gives_up_after_three_retries(tmp_path, monkeypatch):
    c, seen, _ = client(tmp_path, monkeypatch, [503])
    with pytest.raises(LLMError) as e:
        c.chat(MSG, None)
    assert e.value.status == 503 and len(seen) == 4


def test_timeout_is_retried_but_400_is_not(tmp_path, monkeypatch):
    c, seen, _ = client(tmp_path, monkeypatch, ["timeout", OK])
    assert c.chat(MSG, None).attempts == 2
    c2, seen2, _ = client(tmp_path, monkeypatch, [400, OK], model="m/b")
    with pytest.raises(LLMError) as e:
        c2.chat(MSG, None)
    assert e.value.status == 400 and not e.value.retryable and len(seen2) == 1


def test_key_never_in_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    t = httpx.MockTransport(lambda req: httpx.Response(401, text="bad key sk-secret"))
    c = OpenAIChat("m/c", cache=tmp_path, transport=t, policy=RetryPolicy(retries=0))
    with pytest.raises(LLMError) as e:
        c.chat(MSG, None)
    assert "sk-secret" not in str(e.value)


def test_fallback_marks_reply_and_breaker_skips_primary(tmp_path, monkeypatch):
    a, seen_a, _ = client(tmp_path, monkeypatch, [500], model="m/a", retries=1)
    b, seen_b, _ = client(tmp_path, monkeypatch, [OK], model="m/b")
    now = [0.0]
    fb = FallbackChat([a, b], trip_after=2, cooldown=60, clock=lambda: now[0])
    for i in range(3):
        rep = fb.chat(MSG + [{"role": "user", "content": str(i)}], None)  # 내용이 달라야 저장된 응답을 다시 쓰지 않는다
        assert rep.model == "m/b" and rep.fallback_from == "m/a"
    assert len(seen_a) == 4  # 두 번 실패(재시도 포함 2회씩)한 뒤 세 번째는 건너뜀
    assert [e["event"] for e in fb.events] == ["failed", "failed", "skipped"]
    now[0] = 61  # 차단 시간이 지나면 1순위를 다시 써 본다
    fb.chat(MSG + [{"role": "user", "content": "x"}], None)
    assert len(seen_a) == 6


def test_all_fail_raises(tmp_path, monkeypatch):
    a, _, _ = client(tmp_path, monkeypatch, [500], model="m/a", retries=0)
    b, _, _ = client(tmp_path, monkeypatch, [404], model="m/b")
    with pytest.raises(LLMError):
        FallbackChat([a, b]).chat(MSG, None)


def test_primary_ok_has_no_fallback(tmp_path, monkeypatch):
    a, _, _ = client(tmp_path, monkeypatch, [OK], model="m/a")
    rep = FallbackChat([a]).chat(MSG, None)
    assert rep.fallback_from == "" and rep.model == "m/a"


def test_rate_limiter_waits_for_window():
    now, slept = [0.0], []

    def sleep(d):
        slept.append(d)
        now[0] += d
    lim = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
    assert lim.acquire() == 0 and lim.acquire() == 0
    now[0] = 10
    assert lim.acquire() == 50 and slept == [50]


def test_with_retry_counts_attempts():
    calls = []

    def f():
        calls.append(1)
        if len(calls) < 2:
            raise LLMError("x", retryable=True)
        return "ok"
    assert with_retry(f, RetryPolicy(sleep=lambda d: None)) == ("ok", 2)


def test_error_keeps_only_message_not_account_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    body = {"error": {"message": "x is not a valid model ID", "code": 400}, "user_id": "user_ABC"}
    c = OpenAIChat("m/d", cache=tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(400, json=body)),
                   policy=RetryPolicy(retries=0))
    with pytest.raises(LLMError) as e:
        c.chat(MSG, None)
    assert "not a valid model ID" in str(e.value) and "user_ABC" not in str(e.value)


def test_failed_eval_item_is_not_scored_as_refusal():
    from proviso.answer.score import item_scores
    assert item_scores(None, {}, {"answer": "", "refused": False, "cites": [], "error": "API 오류 503"}) == {}


def test_mark_cache_on_system_and_last_message():
    from proviso.agent.loop import chat_client, mark_cache
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "tool_call_id": "c1", "content": "R"}]
    out = mark_cache(msgs)
    assert out[0]["content"][0] == {"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}
    assert out[1]["content"] == "Q" and out[3]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[0]["content"] == "S"  # 원본은 그대로
    sys_only = mark_cache(msgs, "sys")
    assert sys_only[0]["content"][0]["cache_control"] and sys_only[3]["content"] == "R"


def test_cache_suffix_in_model_spec(monkeypatch):
    from proviso.agent.loop import chat_client
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    c = chat_client("openrouter:anthropic/claude-haiku-5.5+cache")
    assert c.model == "anthropic/claude-haiku-5.5" and c.prompt_cache == "all"
    assert chat_client("openrouter:anthropic/claude-haiku-5.5+cache-sys").prompt_cache == "sys"
    assert not chat_client("openrouter:z-ai/glm-5.3-flash@low").prompt_cache
