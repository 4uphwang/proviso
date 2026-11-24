"""답변 충실도(13장, 계획서 §7.2). 답의 법령 주장이 에이전트가 도구로 본 조문에 실제로 있는지 모델에게 묻는다.

핵심어 포함률(5~8장)은 정답과 겹치는지만 본다. 충실도는 답이 '본 자료'를 벗어나 지어냈는지 본다(RAGAS faithfulness 계열).
- 자료: 에이전트가 도구로 본 조문(steps의 shown)의 본문. 기준일 질문은 그날 본문이다. 머리에 도구와 같은
  "[법령 조(제목), 시행 날짜]"를 붙인다. 에이전트가 실제로 본 것은 질문과 겹치는 줄을 고른 1,000~4,000자이지만,
  저장해 두지 않아서 여기서는 조문 본문 전체를 준다(조금 너그럽다). 처음에는 3,000자에서 잘랐다가 정의 조항
  (제2조)의 뒷부분이 빠져, 12,000자로 늘렸다(이보다 긴 조문은 3개).
- 판정자는 답을 법령 주장 단위로 나누고 주장마다 근거 있음 / 근거 없음 / 어긋남을 고른다.
- 답 하나의 충실도 = 근거 있는 주장 / 주장 수. '근거 없는 주장이 있는 답'은 근거 없음·어긋남이 하나라도 있는 답이다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

VERDICTS = ("supported", "unsupported", "contradicted")
SOURCE_CHARS = 12000  # 조문 하나당. 별표처럼 아주 긴 조문만 잘린다

PROMPT = """너는 법령 답변의 근거를 확인하는 채점자다. 아래 [자료]는 답한 사람이 본 법령 조문이다.
[답]을 법령에 관한 주장 단위로 나누고, 주장마다 [자료]로 뒷받침되는지 판정하라.

- 주장: 법령이 무엇을 정하는지에 관한 한 문장 크기의 내용(요건, 기한, 금액, 주체, 허용·금지, 예외, 조문 번호와 그 내용).
  인사말, 질문 되풀이, "법률 자문이 아닙니다" 같은 고지, "답할 수 없습니다" 같은 거절은 주장이 아니다.
- supported: [자료]에 같은 내용이 있다. 표현이 달라도 뜻이 같으면 된다. 자료를 바르게 요약·종합한 결론도 된다.
- contradicted: [자료]와 어긋난다(숫자, 기간, 주체, 허용·금지, 조문 번호가 다름, 뜻이 뒤집힘).
- unsupported: [자료]에 그런 내용이 없다. 일반 상식이나 다른 법에 맞는 말이라도 [자료]에 없으면 unsupported다.
법령이 실제로 무엇인지 네 지식으로 판단하지 말고 [자료]만 보라.

질문: {question}

[답]
{answer}

[자료]
{sources}

JSON 하나만 써라. 다른 말은 쓰지 마라.
{{"claims": [{{"claim": "주장 요약", "verdict": "supported|unsupported|contradicted", "source": 자료 번호 또는 null}}]}}"""


@dataclass
class Faith:
    claims: list[dict]
    parsed: bool = True

    @property
    def n(self) -> int:
        return len(self.claims)

    @property
    def score(self) -> float | None:
        return sum(c["verdict"] == "supported" for c in self.claims) / self.n if self.n else None

    @property
    def unfaithful(self) -> bool:
        return any(c["verdict"] != "supported" for c in self.claims)


def shown_keys(steps: list[dict]) -> list[tuple[str, str]]:
    """에이전트가 도구로 본 조문(본 순서, 중복 없이)."""
    out: dict[tuple[str, str], None] = {}
    for s in steps:
        for d, a in s.get("shown") or []:
            out[(d, a)] = None
    return list(out)


def format_sources(keys: list[tuple[str, str]], articles: dict, limit: int = SOURCE_CHARS) -> str:
    parts = []
    for i, k in enumerate((k for k in keys if k in articles), 1):
        a = articles[k]
        text = a["text"] if len(a["text"]) <= limit else a["text"][:limit] + " …(이하 생략)"
        parts.append(f"({i}) [{a['doc_title']} {k[1]}({a.get('title') or ''}), 시행 {a['valid_from']}]\n{text.strip()}")
    return "\n\n".join(parts) or "(본 자료 없음)"


def build_prompt(question: str, answer: str, sources: str) -> str:
    return PROMPT.format(question=question, answer=answer.strip(), sources=sources)


def parse(text: str) -> Faith:
    """판정자 응답의 JSON. 코드 블록이나 앞뒤 말이 붙어도 첫 { … 마지막 }을 읽는다. 못 읽으면 parsed=False."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return Faith([], parsed=False)
    try:
        d = json.loads(m.group())
    except json.JSONDecodeError:
        return Faith([], parsed=False)
    claims = []
    for c in d.get("claims") or []:
        v = str(c.get("verdict", "")).strip().lower()
        if v not in VERDICTS:
            return Faith([], parsed=False)
        claims.append({"claim": str(c.get("claim", "")), "verdict": v, "source": c.get("source")})
    return Faith(claims)


def judge(llm, question: str, answer: str, sources: str) -> tuple[Faith, float]:
    """(판정, 요금). llm은 agent.loop.chat_client(…)."""
    rep = llm.chat([{"role": "user", "content": build_prompt(question, answer, sources)}], None)
    return parse(rep.content), rep.cost
