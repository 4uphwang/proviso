"""질의 가공(4장): 로컬 LLM(Ollama)으로 질문을 바꿔 검색한다. API 비용은 없다.

Q1 질의 재작성: 일반인 말을 법령에 나올 법한 용어로 바꿔 원래 질문 뒤에 붙인다(원래 단어는 그대로 둔다).
Q2 HyDE: 질문에 답하는 '가상의 조문'을 쓰게 하고 그 글로 검색한다. 지어낸 내용이 섞일 수 있어서
   검색어로만 쓰고 답변에는 쓰지 않는다.

같은 (모델, 프롬프트, 옵션)의 응답은 data/cache/llm/에 저장해 다시 돌릴 때 같은 결과를 쓴다
(온도 0이어도 GPU 연산 순서에 따라 출력이 조금 달라질 수 있어서). 지연은 캐시가 없을 때만 의미가 있다.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from typing import Any

import httpx

from proviso.config import ROOT
from proviso.eval.metrics import Hit

CACHE = ROOT / "data" / "cache" / "llm"
OLLAMA = "http://localhost:11434"
# 같은 말을 끝없이 되풀이해 시간 초과가 난 응답만 이 길이에서 끊어 다시 받는다. 정상으로 끝난 응답은 모두 이보다 짧아 바뀌지 않는다
RUNAWAY_CAP = 1000

PROMPTS = {
    "rewrite": (
        "당신은 한국 금융 법령 검색을 돕는다. 아래 질문은 일반인이 쓴 말이다.\n"
        "이 질문의 답이 들어 있을 법령 조문을 찾기 위한 검색어를 법령에서 실제로 쓰는 용어로 써라.\n"
        "- 질문에 답하지 마라.\n- 조 번호를 지어내지 마라.\n- 용어 3~8개를 쉼표로 나열하고, 다른 말은 쓰지 마라.\n\n"
        "질문: {q}"),
    "hyde": (
        "아래 질문에 답하는 한국 금융 법령(법률 또는 시행령)의 조문 본문을 법령 문체로 2~4문장 써라.\n"
        "조 번호와 법령 이름은 쓰지 말고, 본문만 써라.\n\n질문: {q}"),
}


class OllamaClient:
    def __init__(self, model: str, temperature: float = 0.0, seed: int = 0, num_ctx: int = 4096,
                 host: str = OLLAMA, timeout: float = 120.0, num_predict: int | None = None,
                 presence_penalty: float | None = None):
        self.model = model
        self.options = {"temperature": temperature, "seed": seed, "num_ctx": num_ctx}
        if num_predict is not None:  # 4장 캐시 키가 바뀌지 않게, 줄 때만 넣는다
            self.options["num_predict"] = num_predict
        if presence_penalty is not None:  # 안 주면 모델 기본값을 쓴다. qwen3.5는 1.5라서 온도 0에서도 같은 말을 피한다(5장)
            self.options["presence_penalty"] = presence_penalty
        self._http = httpx.Client(base_url=host, timeout=timeout)
        self.last_cached = False
        self.last_seconds = 0.0

    def generate(self, prompt: str) -> str:
        key = hashlib.sha256(json.dumps([self.model, prompt, self.options], ensure_ascii=False).encode()).hexdigest()
        path = CACHE / f"{key}.json"
        if path.exists():
            self.last_cached = True
            d = json.loads(path.read_text(encoding="utf-8"))
            self.last_seconds = d["seconds"]
            return d["text"]
        t0 = time.perf_counter()
        truncated = False
        try:
            text = self._chat(prompt, self.options)
        except httpx.ReadTimeout:
            if "num_predict" in self.options:
                raise
            truncated = True
            text = self._chat(prompt, {**self.options, "num_predict": RUNAWAY_CAP})
        self.last_cached = False
        self.last_seconds = round(time.perf_counter() - t0, 3)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "prompt": prompt, "options": self.options, "text": text,
                                    "seconds": self.last_seconds, **({"truncated": RUNAWAY_CAP} if truncated else {})},
                                   ensure_ascii=False), encoding="utf-8")
        return text

    def _chat(self, prompt: str, options: dict) -> str:
        r = self._http.post("/api/chat", json={"model": self.model, "stream": False, "think": False, "options": options,
                                               "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


class LLMQueryTransform:
    """안쪽 검색기에 넘기기 전에 질문을 바꾼다. mode: rewrite(원래 질문 + 용어) | hyde(원래 질문 + 가상 조문)."""

    name = "llm_query"

    def __init__(self, inner, *, mode: str, model: str = "qwen3.5:4b", client: OllamaClient | None = None,
                 presence_penalty: float | None = None):
        if mode not in PROMPTS:
            raise ValueError(f"모르는 질의 가공: {mode!r}")
        self._inner = inner
        self._mode = mode
        self._client = client or OllamaClient(model, presence_penalty=presence_penalty)
        self.llm_seconds: list[float] = []

    def describe(self) -> dict[str, Any]:
        return {**self._inner.describe(),
                "query_llm": {"mode": self._mode, "model": self._client.model, **self._client.options,
                              "prompt": PROMPTS[self._mode]}}

    def transform(self, question: str) -> str:
        out = self._client.generate(PROMPTS[self._mode].format(q=question))
        self.llm_seconds.append(self._client.last_seconds)
        return f"{question}\n{out}"

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        return self._inner.search(self.transform(question), k=k, as_of=as_of)
