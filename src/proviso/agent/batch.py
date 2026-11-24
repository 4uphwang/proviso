"""오픈라우터 배치 API(19장, 계획서 E7). 기다려도 되는 요청을 한꺼번에 맡기고 나중에 결과를 받는다.

POST /api/v1/batches({endpoint, model, requests: [{custom_id, body}]}) → GET /api/v1/batches/{id}를 되풀이해
status가 completed(또는 failed·expired·cancelled)가 되면 results를 읽는다. 결과 순서는 맡긴 순서와 다르므로
custom_id로 맞춘다. 가격은 모델마다 다르다(배치 변형 ':batch'의 가격, 보통 절반).

요청 본문은 보통 호출과 같다(OpenAIChat.request_body). 단, 본문의 model은 빼고 배치에 한 번만 적는다.
여러 번 대화가 오가는 에이전트에는 맞지 않는다(모델 답을 받아야 다음 도구를 고른다). 한 번 부르면 끝나는 판정에 쓴다.
키는 헤더로만 보내고, 오류 글에서는 지운다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

TERMINAL = {"completed", "failed", "expired", "cancelled"}


@dataclass
class BatchResult:
    batch_id: str
    status: str
    bodies: dict[str, dict]                 # custom_id → 응답 본문(choices, usage …)
    errors: dict[str, str] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)  # 배치 전체 토큰·비용(cost)
    seconds: float = 0.0                       # 맡긴 때부터 끝날 때까지(벽시계)
    polls: int = 0


class BatchClient:
    def __init__(self, *, base_url: str = "https://openrouter.ai/api/v1", key_env: str = "OPENROUTER_API_KEY",
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        import os

        from dotenv import load_dotenv

        from proviso.config import ROOT

        load_dotenv(ROOT / ".env")
        key = os.environ.get(key_env, "")
        if not key:
            raise RuntimeError(f"{key_env}가 비어 있음(.env 확인)")
        self._key = key
        self._http = httpx.Client(base_url=base_url, timeout=120, transport=transport,
                                  headers={"Authorization": f"Bearer {key}"})
        self._sleep, self._clock = sleep, clock

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***")

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            try:
                e = r.json().get("error")
                msg = e.get("message") if isinstance(e, dict) else e
            except ValueError:
                msg = r.text[:200]
            raise RuntimeError(self._redact(f"배치 API 오류 {r.status_code}: {str(msg)[:300]}"))
        return r.json()

    def submit(self, model: str, requests: list[tuple[str, dict]], endpoint: str = "/v1/chat/completions") -> str:
        """requests: [(custom_id, 요청 본문)]. 본문의 model은 뺀다. 반환: 배치 id."""
        ids = [c for c, _ in requests]
        if len(set(ids)) != len(ids):
            raise ValueError("custom_id가 겹침")
        # 문서: endpoint·model을 requests보다 앞에 둬야 한다(dict 순서대로 직렬화된다)
        payload = {"endpoint": endpoint, "model": model,
                   "requests": [{"custom_id": c, "body": {k: v for k, v in b.items() if k != "model"}} for c, b in requests]}
        return self._check(self._http.post("/batches", content=json.dumps(payload, ensure_ascii=False),
                                           headers={"Content-Type": "application/json"}))["id"]

    def get(self, batch_id: str) -> dict:
        return self._check(self._http.get(f"/batches/{batch_id}"))

    def wait(self, batch_id: str, *, every: float = 30.0, limit: float = 24 * 3600,
             progress: Callable[[str], None] = print) -> BatchResult:
        """끝날 때까지 every초마다 상태를 본다. 맡긴 직후부터 잰다."""
        t0, polls = self._clock(), 0
        while True:
            try:
                d = self.get(batch_id)
            except RuntimeError as e:  # 맡긴 직후에는 404가 나기도 한다(실제로 봤다). 처음 몇 번은 기다린다
                if "404" in str(e) and polls < 5:
                    polls += 1
                    self._sleep(min(every, 10))
                    continue
                raise
            polls += 1
            st = d.get("status", "")
            rc = d.get("request_counts") or {}
            progress(f"{st} {rc.get('completed', 0)}/{rc.get('total', '?')} (실패 {rc.get('failed', 0)})")
            if st in TERMINAL:
                break
            if self._clock() - t0 > limit:
                raise TimeoutError(f"배치 {batch_id}가 {limit}초 안에 끝나지 않음")
            self._sleep(every)
        bodies, errors = {}, {}
        for r in d.get("results") or []:
            if r.get("error"):
                errors[r["custom_id"]] = str(r["error"].get("message") or r["error"])[:300]
            else:
                bodies[r["custom_id"]] = (r.get("response") or {}).get("body") or {}
        if st != "completed" and d.get("error"):
            errors["_batch"] = str(d["error"].get("message") or d["error"])[:300]
        return BatchResult(batch_id, st, bodies, errors, d.get("usage") or {}, round(self._clock() - t0, 1), polls)


def content_of(body: dict) -> str:
    try:
        return (body["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError):
        return ""
