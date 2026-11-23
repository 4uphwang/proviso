"""에이전트 루프(6장): 모델이 도구를 골라 부르고, 도구 결과를 읽고, 답한다. 프레임워크 없이 직접 돈다."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

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


class OllamaChat:
    """도구를 쓰는 대화. 같은 입력의 응답은 파일에 저장해 다시 쓴다(tool_call id는 뺀다)."""

    def __init__(self, model: str, *, num_ctx: int = 16384, num_predict: int = 500, host: str = "http://localhost:11434",
                 timeout: float = 600, cache: Path = ROOT / "data" / "cache" / "llm_chat"):
        self.model = model
        self.options = {"temperature": 0.0, "seed": 0, "num_ctx": num_ctx, "num_predict": num_predict, "presence_penalty": 0.0}
        self._http = httpx.Client(base_url=host, timeout=timeout)
        self._cache = cache

    def chat(self, messages: list[dict], tools: list[dict] | None) -> Reply:
        body = {"model": self.model, "messages": messages, "stream": False, "think": False, "options": self.options}
        if tools:
            body["tools"] = tools
        path = self._cache / (hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest() + ".json")
        if path.exists():
            return Reply(**json.loads(path.read_text(encoding="utf-8")))
        t0 = time.perf_counter()
        r = self._http.post("/api/chat", json=body)
        r.raise_for_status()
        d = r.json()
        calls = [{"function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}}
                 for c in d["message"].get("tool_calls") or []]
        rep = Reply((d["message"].get("content") or "").strip(), calls, d.get("prompt_eval_count") or 0,
                    d.get("eval_count") or 0, round(time.perf_counter() - t0, 3))
        self._cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep.__dict__, ensure_ascii=False), encoding="utf-8")
        return rep


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

    @property
    def tool_calls(self) -> list[str]:
        return [s["tool"] for s in self.steps if "tool" in s]


def run_agent(question: str, llm, tools: Toolbox, *, system: str = SYSTEM, max_turns: int = 6, max_calls: int = 8,
              token_limit: int = 11000, seed_search: bool = False,
              seed_more: Callable[[str, list[Key]], Result | None] | None = None) -> Trace:
    """max_turns번까지 도구를 쓰고, 넘거나 자료가 길어지면 도구 없이 답하게 한다.

    seed_search: 모델에게 묻기 전에 코드가 질문 그대로 한 번 검색해 결과를 먼저 준다.
    seed_more: 그 첫 검색 결과(보인 조문)를 받아 코드가 더 붙일 자료. 같은 도구 결과에 이어 붙인다.
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
        tr.steps.append({"llm": True, "prompt_tokens": rep.prompt_tokens, "output_tokens": rep.output_tokens, "seconds": rep.seconds})
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
