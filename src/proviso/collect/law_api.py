"""국가법령정보센터 Open API 클라이언트.

- 공식 Open API(https://www.law.go.kr/DRF)만 쓴다. 출처: 법제처 국가법령정보센터.
- 인증값(OC)은 요청 변수로만 쓰고, 오류 메시지·로그에는 '<OC>'로 가린다.
- 네트워크 오류·5xx·429는 지수 백오프로 재시도하고, 인증·IP 거절은 바로 멈춘다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

BASE_URL = "https://www.law.go.kr/DRF"


class LawApiError(RuntimeError):
    pass


class LawApiAuthError(LawApiError):
    """인증값이나 등록 IP 문제로 거절됨. 재시도해도 소용없다."""


class LawApiClient:
    def __init__(
        self,
        oc: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 5,
        backoff: float = 1.0,
        min_interval: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not oc:
            raise LawApiAuthError("LAW_OC가 비어 있음(.env 확인)")
        self._oc = oc
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._min_interval = min_interval
        self._sleep = sleep
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._oc, "<OC>")

    def _get(self, path: str, params: dict[str, Any]) -> str:
        params = {"OC": self._oc, **params}
        last_err = ""
        for attempt in range(1, self._max_attempts + 1):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                last_err = f"네트워크 오류: {type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    if "접속에 실패" in resp.text[:3000]:
                        raise LawApiAuthError(
                            "API가 접속을 거절함. .env의 LAW_OC와 open.law.go.kr 마이페이지의 등록 IP를 확인"
                        )
                    return resp.text
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            if attempt < self._max_attempts:
                self._sleep(self._backoff * 2 ** (attempt - 1))
        raise LawApiError(self._redact(f"{path} 요청 실패({self._max_attempts}회 시도): {last_err}"))

    def _get_json(self, path: str, params: dict[str, Any]) -> dict:
        text = self._get(path, {**params, "type": "JSON"})
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LawApiError(f"{path}: JSON이 아닌 응답({text[:80]!r}...)") from e

    # --- 엔드포인트 ---

    def search_laws(self, query: str) -> list[dict]:
        """현행 법령 검색. 결과가 하나면 dict로 오므로 항상 list로 맞춘다."""
        data = self._get_json("/lawSearch.do", {"target": "law", "query": query, "display": 100})
        laws = data.get("LawSearch", {}).get("law", [])
        return laws if isinstance(laws, list) else [laws]

    def law_body(self, mst: str) -> dict:
        """법령 본문. 현행·과거 버전 모두 MST로 받는다."""
        data = self._get_json("/lawService.do", {"target": "law", "MST": mst})
        if "법령" not in data:
            raise LawApiError(f"MST {mst}: 본문에 '법령' 키가 없음")
        return data

    def history_html(self, query: str) -> str:
        """법령 연혁 목록. JSON을 요청해도 HTML로만 온다(1b 단계에서 사용)."""
        return self._get("/lawSearch.do", {"target": "lsHistory", "type": "HTML", "query": query, "display": 100})
