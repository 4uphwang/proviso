"""장애 주입 실험(14장). 모의 서버와 가상 시계로 장애를 일으켜 '대응 없음 / 재시도 / 재시도+대체'를 비교한다.

실제 API를 부르지 않고 실제로 기다리지도 않는다(지연·백오프는 가상 시계에 더한다). 같은 seed면 같은 결과다.
질문 하나 = 모델 호출 3번(9장 DeepSeek의 질문당 평균 호출 수에 가깝다). 호출이 하나라도 실패하면 그 질문은 실패다.
질문은 12초마다 들어온다. 앞 질문이 밀리면 다음 질문은 앞 질문이 끝난 뒤 시작한다(지연에 기다린 시간은 넣지 않는다).

python -m proviso.agent.chaos
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from proviso.agent.loop import OpenAIChat
from proviso.agent.resilience import FallbackChat, LLMError, RetryPolicy

OK = {"choices": [{"message": {"content": "답"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}}
CALLS_PER_QUESTION = 3
ARRIVAL = 12.0  # 질문은 12초마다 들어온다(앞 질문이 실패해도 다음 질문이 당겨 오지 않게). 100문항 = 약 20분


@dataclass
class Scenario:
    name: str
    p429: float = 0.0
    p5xx: float = 0.0
    p_slow: float = 0.0          # 90초 걸리는 응답(시간 한도 60초를 넘는다)
    outage: tuple[float, float] | None = None  # 이 가상 시각(초) 구간 동안 1순위가 모두 503


SCENARIOS = [
    Scenario("정상(5xx 1%)", p5xx=0.01),
    Scenario("속도 제한(429 30%)", p429=0.30),
    Scenario("서버 오류(5xx 20%)", p5xx=0.20),
    Scenario("느린 응답(10%가 90초)", p_slow=0.10, p5xx=0.01),
    Scenario("1순위 장애(200~600초 동안 모두 503)", p5xx=0.01, outage=(200, 600)),
]


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, d: float) -> None:
        self.t += d


def mock(sc: Scenario, clock: Clock, rng: random.Random, base: float, timeout: float, counter: list[int] | None):
    """counter가 있으면 1순위 서버(호출 번호로 장애 구간을 센다). 없으면 대체 서버(5xx 2%만)."""
    def handle(req):
        if counter is not None:
            counter[0] += 1
        r = rng.random()
        if counter is not None and sc.outage and sc.outage[0] <= clock.t < sc.outage[1]:
            clock.t += 0.2
            return httpx.Response(503, json={"error": "down"})
        if counter is None:
            if r < 0.02:
                clock.t += 0.5
                return httpx.Response(500, json={"error": "x"})
            clock.t += base * (0.7 + 0.6 * rng.random())
            return httpx.Response(200, json=OK)
        if r < sc.p429:
            clock.t += 0.1
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate"})
        if r < sc.p429 + sc.p5xx:
            clock.t += 0.5
            return httpx.Response(503, json={"error": "x"})
        if r < sc.p429 + sc.p5xx + sc.p_slow:
            clock.t += timeout  # 90초짜리 응답을 60초에 끊는다
            raise httpx.ReadTimeout("느림", request=req)
        clock.t += base * (0.7 + 0.6 * rng.random())
        return httpx.Response(200, json=OK)
    return httpx.MockTransport(handle)


def run(sc: Scenario, mode: str, *, questions: int = 100, seed: int = 0) -> dict:
    """mode: none(그대로), retry(재시도), fallback(재시도+대체)."""
    import os

    os.environ.setdefault("OPENROUTER_API_KEY", "chaos-test")  # 모의 서버라 키는 쓰이지 않는다
    clock, timeout = Clock(), 60.0
    rng = random.Random(seed)
    counter = [0]
    pol = RetryPolicy(retries=0 if mode == "none" else 3, timeout=timeout, sleep=clock.sleep, rng=random.Random(seed))
    with tempfile.TemporaryDirectory() as tmp:
        prim = OpenAIChat("chaos/primary", cache=Path(tmp) / "a", per_minute=10**9, policy=pol,
                          transport=mock(sc, clock, rng, 3.0, timeout, counter))
        clients = [prim]
        if mode == "fallback":
            clients.append(OpenAIChat("chaos/fallback", cache=Path(tmp) / "b", per_minute=10**9, policy=pol,
                                      transport=mock(sc, clock, random.Random(seed + 1), 4.0, timeout, None)))
        # 대체가 없으면 클라이언트를 그대로 쓴다(차단기는 대체 모델이 있을 때만 의미가 있다)
        llm = FallbackChat(clients, trip_after=2, cooldown=60, clock=lambda: clock.t) if mode == "fallback" else prim
        ok, lat, fb, attempts = 0, [], 0, 0
        for q in range(questions):
            clock.t = max(clock.t, q * ARRIVAL)
            t0, good = clock.t, True
            for c in range(CALLS_PER_QUESTION):
                try:
                    rep = llm.chat([{"role": "user", "content": f"{q}-{c}"}], None)
                except LLMError:
                    good = False
                    break
                fb += bool(getattr(rep, "fallback_from", ""))
                attempts += rep.attempts
            if good:
                ok += 1
                lat.append(clock.t - t0)
    lat.sort()
    return {"scenario": sc.name, "mode": mode, "success": ok / questions,
            "p50": lat[len(lat) // 2] if lat else None, "p95": lat[int(len(lat) * 0.95)] if lat else None,
            "fallback_calls": fb, "primary_requests": counter[0]}


def main() -> None:
    names = {"none": "대응 없음", "retry": "재시도", "fallback": "재시도+대체"}
    print("| 장애 | 대응 | 질문 성공률 | 성공한 질문 p50 | p95 | 대체 모델이 답한 호출 | 1순위에 보낸 요청 |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for sc in SCENARIOS:
        for mode in ("none", "retry", "fallback"):
            r = run(sc, mode)
            print(f"| {sc.name} | {names[mode]} | {r['success']:.2f} | {r['p50']:.1f}초 | {r['p95']:.1f}초 | "
                  f"{r['fallback_calls']} | {r['primary_requests']} |")


if __name__ == "__main__":
    main()
