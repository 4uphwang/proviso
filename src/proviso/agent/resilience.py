"""모델 호출 장애 대응(14장, 계획서 §5.5).

- 재시도: 429·5xx·연결 오류·시간 초과만, 지수 백오프(1, 2, 4초 …, 최대 cap초)에 0~25% 흔들림을 더해 최대 retries번.
  서버가 Retry-After를 주면 그만큼(cap까지) 기다린다. 400·401·404처럼 다시 보내도 같은 오류는 재시도하지 않는다.
- 속도 제한: 회사(API 주소)마다 분당 호출 수를 넘지 않게 기다린다.
- 대체 모델: 1순위가 (재시도까지) 실패하면 다음 모델로 넘어가고, 응답에 fallback_from을 남긴다.
  같은 모델이 연속 trip_after번 실패하면 cooldown초 동안 그 모델을 건너뛴다(차단기).
  평가 실행은 대체를 쓰지 않는다. 다른 모델의 답이 섞이면 비교가 깨지므로 실패로 기록한다.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """모델 호출 실패. retryable이면 다시 보내 볼 만한 오류(429·5xx·연결·시간 초과)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status, self.retryable, self.retry_after = status, retryable, retry_after


def retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def parse_retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # HTTP 날짜 형식은 쓰지 않는다(지수 백오프로 대신한다)


@dataclass
class RetryPolicy:
    retries: int = 3          # 첫 시도 뒤 다시 보내는 횟수
    base: float = 1.0
    cap: float = 30.0
    timeout: float = 60.0     # 호출 하나의 시간 한도(초). 로컬 모델은 따로 길게 둔다
    jitter: float = 0.25
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(0))

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """attempt: 1부터(첫 재시도). Retry-After가 있으면 그 값을, 없으면 base·2^(attempt−1)에 흔들림을 더한다."""
        if retry_after is not None:
            return min(retry_after, self.cap)
        d = min(self.base * 2 ** (attempt - 1), self.cap)
        return d * (1 + self.jitter * self.rng.random())


NO_RETRY = RetryPolicy(retries=0)


def with_retry(fn: Callable[[], Any], policy: RetryPolicy, on_retry: Callable[[int, LLMError, float], None] | None = None):
    """(결과, 시도 횟수). 재시도할 수 없는 오류이거나 횟수를 다 쓰면 마지막 LLMError를 그대로 던진다."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except LLMError as e:
            if not e.retryable or attempt > policy.retries:
                e.attempts = attempt
                raise
            d = policy.delay(attempt, e.retry_after)
            if on_retry:
                on_retry(attempt, e, d)
            policy.sleep(d)


class RateLimiter:
    """창 60초 안의 호출이 per_minute개를 넘지 않게 기다린다. 스레드에서 같이 써도 된다."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute, self._clock, self._sleep = per_minute, clock, sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.waited = 0.0

    def acquire(self) -> float:
        """기다린 초."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    self.waited += waited
                    return waited
                d = 60 - (now - self._calls[0])
            self._sleep(d)
            waited += d


_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(key: str, per_minute: int) -> RateLimiter:
    """회사(API 주소)마다 하나. 같은 회사의 모델들이 한도를 나눠 쓴다."""
    if key not in _LIMITERS:
        _LIMITERS[key] = RateLimiter(per_minute)
    return _LIMITERS[key]


class FallbackChat:
    """여러 대화 클라이언트를 순서대로 써 본다. chat()은 첫 성공의 Reply를 돌려주고, 1순위가 아니면 fallback_from을 붙인다."""

    def __init__(self, clients: list, *, trip_after: int = 2, cooldown: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        if not clients:
            raise ValueError("클라이언트가 하나는 있어야 함")
        self.clients, self.trip_after, self.cooldown, self._clock = clients, trip_after, cooldown, clock
        self._fails = [0] * len(clients)
        self._open_until = [0.0] * len(clients)
        self.events: list[dict] = []   # 실패·건너뜀 기록(트레이스용)

    @property
    def model(self) -> str:
        return self.clients[0].model

    @property
    def options(self) -> dict:
        return self.clients[0].options

    def chat(self, messages: list[dict], tools: list[dict] | None):
        primary = self.clients[0].model
        last: LLMError | None = None
        for i, c in enumerate(self.clients):
            if self._open_until[i] > self._clock():
                self.events.append({"model": c.model, "event": "skipped"})
                continue
            try:
                rep = c.chat(messages, tools)
            except LLMError as e:
                last = e
                self._fails[i] += 1
                self.events.append({"model": c.model, "event": "failed", "status": e.status, "error": str(e)[:200]})
                if self._fails[i] >= self.trip_after:
                    self._open_until[i] = self._clock() + self.cooldown
                continue
            self._fails[i] = 0
            rep.model = c.model
            if i:
                rep.fallback_from = primary
            return rep
        raise LLMError(f"모든 모델이 실패: {last}", status=last.status if last else None)
