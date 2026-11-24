"""에이전트 루프(6장): 모델이 도구를 골라 부르고, 도구 결과를 읽고, 답한다. 프레임워크 없이 직접 돈다."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from proviso.agent.resilience import LLMError, RetryPolicy, limiter_for, parse_retry_after, retryable_status, with_retry
from proviso.agent.tools import SPECS, Key, Result, Toolbox
from proviso.answer.run import CITE, REFUSE
from proviso.config import ROOT

SYSTEM = ("너는 한국 금융 법령으로만 답하는 도우미다. 법령: {laws}.\n"
          "답하기 전에 반드시 도구로 조문을 찾아 읽어라. 조문에 '대통령령으로 정하는'이 있으면 follow_reference로 시행령을 확인하고, "
          "용어의 뜻을 물으면 define_term을 써라. 필요한 조문을 다 봤으면 도구를 그만 쓰고 한국어로 간결하게 답하라.\n"
          + CITE.replace("위 자료", "도구로 본 자료") + REFUSE)
FINISH = "도구는 더 쓸 수 없다. 지금까지 도구로 본 자료만으로 처음 질문에 답하라."


@dataclass
class Reply:
    content: str
    tool_calls: list[dict]
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cost: float = 0.0           # API 모델이 알려 준 요금(달러). 로컬은 0
    provider: str = ""          # 오픈라우터가 실제로 보낸 곳
    attempts: int = 1           # 14장: 재시도까지 친 시도 횟수
    model: str = ""             # 14장: 대체 모델을 쓰면 실제로 답한 모델
    fallback_from: str = ""     # 14장: 1순위가 실패해 넘어왔으면 그 1순위 모델


class OllamaChat:
    """도구를 쓰는 대화. 같은 입력의 응답은 파일에 저장해 다시 쓴다(tool_call id는 뺀다)."""

    def __init__(self, model: str, *, num_ctx: int = 16384, num_predict: int = 500, host: str = "http://localhost:11434",
                 timeout: float = 600, cache: Path = ROOT / "data" / "cache" / "llm_chat", policy: RetryPolicy | None = None,
                 transport: httpx.BaseTransport | None = None):
        """timeout: 로컬 4B·9B는 긴 자료에서 한 번에 수십 초 걸려 API(60초)보다 길게 둔다."""
        self.model = model
        self.options = {"temperature": 0.0, "seed": 0, "num_ctx": num_ctx, "num_predict": num_predict, "presence_penalty": 0.0}
        self.policy = policy or RetryPolicy(timeout=timeout)
        self._http = httpx.Client(base_url=host, timeout=self.policy.timeout, transport=transport)
        self._cache = cache

    def chat(self, messages: list[dict], tools: list[dict] | None) -> Reply:
        body = {"model": self.model, "messages": messages, "stream": False, "think": False, "options": self.options}
        if tools:
            body["tools"] = tools
        path = self._cache / (hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest() + ".json")
        if path.exists():
            return Reply(**json.loads(path.read_text(encoding="utf-8")))
        t0 = time.perf_counter()
        d, attempts = with_retry(lambda: _post(self._http, "/api/chat", body), self.policy)
        calls = [{"function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}}
                 for c in d["message"].get("tool_calls") or []]
        rep = Reply((d["message"].get("content") or "").strip(), calls, d.get("prompt_eval_count") or 0,
                    d.get("eval_count") or 0, round(time.perf_counter() - t0, 3), attempts=attempts, model=self.model)
        self._cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep.__dict__, ensure_ascii=False), encoding="utf-8")
        return rep


class OpenAIChat:
    """OpenAI 호환 API(오픈라우터 등)로 도구를 쓰는 대화(9장). OllamaChat과 같은 메시지를 받아 바꿔 보낸다.

    메시지는 Ollama 형식(도구 호출에 id가 없고, 도구 결과에 tool_name)으로 쌓이므로, 보낼 때 호출마다 id를 붙이고
    도구 결과를 순서대로 그 id에 잇는다. 같은 입력의 응답은 파일에 저장해 다시 쓴다(다시 돌려도 요금이 들지 않는다).
    """

    def __init__(self, model: str, *, base_url: str = "https://openrouter.ai/api/v1", key_env: str = "OPENROUTER_API_KEY",
                 num_predict: int = 500, timeout: float = 300, cache: Path = ROOT / "data" / "cache" / "llm_chat",
                 reasoning: str | None = None, repeat: int = 0, policy: RetryPolicy | None = None,
                 per_minute: int = 60, transport: httpx.BaseTransport | None = None):
        """reasoning: None이면 추론을 끈다. 끌 수 없는 모델(gpt-oss, GLM 등)은 'low'처럼 가장 낮은 단계를 준다(결과에 남는다)."""
        import os

        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        key = os.environ.get(key_env, "")
        if not key:
            raise RuntimeError(f"{key_env}가 비어 있음(.env 확인)")
        self.model = model
        self.options = {"temperature": 0.0, "seed": 0, "max_tokens": num_predict,
                        "reasoning": {"effort": reasoning, "exclude": True} if reasoning else {"enabled": False}}
        if reasoning:  # 추론 토큰도 max_tokens에 들어가므로 답이 잘리지 않게 넉넉히 준다
            self.options["max_tokens"] = num_predict + 2000
        self._key = key  # 헤더로만 보낸다. 저장하는 파일(응답, 결과, 기록)에는 넣지 않는다
        self.policy = policy or RetryPolicy(timeout=60.0)  # 14장: 호출마다 60초, 429·5xx만 최대 3번 다시
        self._limit = limiter_for(base_url, per_minute)  # 14장: 평가 실행이 회사의 분당 한도에 걸리지 않게
        self._http = httpx.Client(base_url=base_url, timeout=self.policy.timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {key}"})
        self._cache = cache
        self.total_cost = 0.0
        self.repeat = repeat  # 9장: API 모델은 temperature 0이어도 답이 매번 달라서 반복한다. 0이면 처음 돌린 응답 그대로

    def redact(self, text: str) -> str:
        """오류 글에 키가 섞여 나와도 지운다."""
        return text.replace(self._key, "***") if self._key else text

    def __repr__(self) -> str:
        return f"OpenAIChat(model={self.model!r})"

    @staticmethod
    def convert(messages: list[dict]) -> list[dict]:
        out, pending, n = [], [], 0
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    n += 1
                    calls.append({"id": f"call_{n}", "type": "function", "function": {
                        "name": c["function"]["name"], "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False)}})
                pending = [c["id"] for c in calls]
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": pending.pop(0) if pending else f"call_{n}", "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages: list[dict], tools: list[dict] | None) -> Reply:
        body = {"model": self.model, "messages": self.convert(messages), **self.options, "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        key = body if not self.repeat else {**body, "_repeat": self.repeat}  # 반복 번호는 저장 이름에만 쓰고 API에는 보내지 않는다
        path = self._cache / (hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest() + ".json")
        if path.exists():
            return Reply(**json.loads(path.read_text(encoding="utf-8")))
        t0 = time.perf_counter()

        def once() -> dict:
            self._limit.acquire()
            try:
                return _post(self._http, "/chat/completions", body)
            except LLMError as e:
                raise LLMError(self.redact(str(e)), status=e.status, retryable=e.retryable, retry_after=e.retry_after) from None

        d, attempts = with_retry(once, self.policy)
        if not d.get("choices"):  # 오픈라우터는 공급자 오류를 200 안의 error로 주기도 한다
            raise LLMError(self.redact(f"API 응답에 답이 없음: {str(d.get('error'))[:200]}"), retryable=False)
        msg = d["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"function": {"name": c["function"]["name"], "arguments": args}})
        u = d.get("usage") or {}
        rep = Reply((msg.get("content") or "").strip(), calls, u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0,
                    round(time.perf_counter() - t0, 3), float(u.get("cost") or 0.0), d.get("provider") or "",
                    attempts=attempts, model=self.model)
        self.total_cost += rep.cost
        self._cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep.__dict__, ensure_ascii=False), encoding="utf-8")
        return rep


def _reason(r: httpx.Response) -> str:
    """오류 응답에서 error.message만 꺼낸다. 본문 전체에는 계정 id(user_id) 같은 값이 섞여 오므로 기록에 남기지 않는다."""
    try:
        e = r.json().get("error")
        msg = e.get("message") if isinstance(e, dict) else e
        return str(msg)[:200] if msg else "(이유 없음)"
    except (ValueError, AttributeError):
        return r.text[:120].replace("\n", " ")


def _post(http: httpx.Client, path: str, body: dict) -> dict:
    """POST 하나. 실패는 LLMError로 바꾼다(재시도할 만한지 표시)."""
    try:
        r = http.post(path, json=body)
    except httpx.TimeoutException as e:
        raise LLMError(f"시간 초과: {type(e).__name__}", retryable=True) from None
    except httpx.TransportError as e:
        raise LLMError(f"연결 오류: {type(e).__name__}: {e}", retryable=True) from None
    if r.status_code >= 400:
        raise LLMError(f"API 오류 {r.status_code}: {_reason(r)}", status=r.status_code,
                       retryable=retryable_status(r.status_code), retry_after=parse_retry_after(r.headers.get("retry-after")))
    return r.json()


def chat_client(model: str, max_tokens: int = 500, repeat: int = 0):
    """'openrouter:<모델 id>[@추론 단계]'면 오픈라우터, 아니면 로컬 Ollama. 예: openrouter:openai/gpt-oss-20b@low
    max_tokens: 답 길이 상한. 500은 5장에서 4B의 끝없는 반복을 끊으려고 정한 값이다(9장: API 모델은 답을 길게 써서 잘린다)."""
    if not model.startswith("openrouter:"):
        return OllamaChat(model, num_predict=max_tokens)
    mid, _, effort = model.split(":", 1)[1].partition("@")
    return OpenAIChat(mid, reasoning=effort or None, num_predict=max_tokens, repeat=repeat)


@dataclass
class Trace:
    answer: str = ""
    shown: list[Key] = field(default_factory=list)
    context: str = ""          # 도구가 보여 준 글 전부
    steps: list[dict] = field(default_factory=list)
    stop: str = ""             # answered | turns | tokens | repeat
    seconds: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def tool_calls(self) -> list[str]:
        return [s["tool"] for s in self.steps if "tool" in s]


def run_agent(question: str, llm, tools: Toolbox, *, system: str = SYSTEM, max_turns: int = 6, max_calls: int = 8,
              token_limit: int = 11000, seed_search: bool = False,
              seed_more: Callable[[str, list[Key]], Result | None] | None = None,
              on_step: Callable[[dict], None] | None = None) -> Trace:
    """max_turns번까지 도구를 쓰고, 넘거나 자료가 길어지면 도구 없이 답하게 한다.

    seed_search: 모델에게 묻기 전에 코드가 질문 그대로 한 번 검색해 결과를 먼저 준다.
    seed_more: 그 첫 검색 결과(보인 조문)를 받아 코드가 더 붙일 자료. 같은 도구 결과에 이어 붙인다.
    on_step: 단계(도구 호출, 모델 호출)가 끝날 때마다 그 기록을 받는다(16장: 서버가 진행 상황을 흘려보낼 때).
    """
    laws = ", ".join(sorted({a["doc_title"] for a in tools.law.articles.values()}))
    messages: list[dict] = [{"role": "system", "content": system.format(laws=laws)}, {"role": "user", "content": question}]
    tr = Trace()
    asked: set[str] = set()

    def use(name: str, args: Any, more=None) -> bool:
        """도구 하나를 실행해 대화와 기록에 남긴다. 이미 한 호출이면 False."""
        sig = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
        repeated = sig in asked
        asked.add(sig)
        t0 = time.perf_counter()
        if repeated:
            text, shown, err = "이미 같은 호출로 본 결과다. 다른 도구를 쓰거나 답하라.", [], True
        else:
            r = tools.call(name, args, question)
            text, shown, err = r.text, list(r.shown), r.error
            add = more(question, shown) if more and not err else None
            if add:
                text, shown = text + "\n\n" + add.text, shown + [k for k in add.shown if k not in shown]
        sec = round(time.perf_counter() - t0, 3)
        tr.seconds += sec
        tr.steps.append({"tool": name, "args": args, "chars": len(text), "shown": shown, "error": err, "repeat": repeated, "seconds": sec})
        if on_step:
            on_step(tr.steps[-1])
        tr.shown += [k for k in shown if k not in tr.shown]
        if not repeated:
            tr.context += text + "\n\n"
        messages.append({"role": "tool", "tool_name": name, "content": text})
        return not repeated

    def ask(specs) -> Reply:
        rep = llm.chat(messages, specs)
        tr.seconds += rep.seconds
        tr.prompt_tokens += rep.prompt_tokens
        tr.output_tokens += rep.output_tokens
        tr.cost += rep.cost
        step = {"llm": True, "prompt_tokens": rep.prompt_tokens, "output_tokens": rep.output_tokens, "seconds": rep.seconds}
        if getattr(rep, "attempts", 1) > 1:  # 14장: 재시도·대체가 있었을 때만 남긴다(전 결과 파일과 모양을 같게)
            step["attempts"] = rep.attempts
        if getattr(rep, "fallback_from", ""):
            step |= {"model": rep.model, "fallback_from": rep.fallback_from}
        tr.steps.append(step)
        if on_step:
            on_step(step)
        return rep

    if seed_search:
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search", "arguments": {"query": question}}}]})
        use("search", {"query": question}, seed_more)
    for _ in range(max_turns):
        rep = ask(SPECS)
        if not rep.tool_calls:
            tr.answer, tr.stop = rep.content, "answered"
            return tr
        messages.append({"role": "assistant", "content": rep.content, "tool_calls": rep.tool_calls})
        fresh = [use(c["function"]["name"], c["function"]["arguments"]) for c in rep.tool_calls]
        if not any(fresh):
            tr.stop = "repeat"
        elif rep.prompt_tokens + rep.output_tokens + _tokens(messages[-len(fresh):]) > token_limit:
            tr.stop = "tokens"
        elif len(tr.tool_calls) >= max_calls:
            tr.stop = "calls"
        if tr.stop:
            break
    tr.stop = tr.stop or "turns"
    messages.append({"role": "user", "content": FINISH})
    tr.answer = ask(None).content
    return tr


def _tokens(messages: list[dict]) -> int:
    """방금 받은 도구 결과의 토큰 어림(한글 1자 ≈ 0.7토큰)."""
    return int(sum(len(m["content"]) for m in messages) * 0.7)
