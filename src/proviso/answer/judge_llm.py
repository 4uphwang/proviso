"""핵심어 채점을 모델로(8장). 글자 맞추기는 같은 뜻을 다르게 쓴 답("인가 취소" ↔ "인가를 취소")을 틀렸다고 센다.

핵심어마다 답에 같은 뜻이 있는지(있음), 없는지(없음), 다르게 썼는지(틀림)를 로컬 모델에게 묻는다.
- plain: 질문, 답, 핵심어만 준다
- context: 핵심어가 나오는 정답 조문의 문장을 함께 준다("다음 각 목의 정보" 같은 형식 문구가 무엇을 가리키는지 알게)
사람 라벨(data/labels/)과 먼저 맞춰 보고 쓴다.
"""

from __future__ import annotations

import re

VERDICTS = {"있음": "yes", "없음": "no", "틀림": "wrong"}

PROMPT = """너는 법령 답변 채점자다. 아래 답에 각 핵심 내용이 들어 있는지 판단하라.
- 있음: 답에 그 내용이 같은 뜻으로 있다. 글자·띄어쓰기·조사·어순이 달라도 뜻이 같으면 있음이다(예: "인가 취소"와 "인가를 취소", "투자매매업ㆍ투자중개업"과 "투자매매업 및 투자중개업").
- 틀림: 그 내용을 다루지만 숫자, 기간, 주체, 허용·금지가 다르게 쓰였다.
- 없음: 답에 그 내용이 없다.
답이 맞는지 법령을 따로 판단하지 말고, 핵심 내용과 답만 비교하라.

질문: {question}

답:
{answer}

핵심 내용:
{keywords}

핵심 내용 번호마다 한 줄씩 "번호: 있음" 또는 "번호: 없음" 또는 "번호: 틀림"으로만 써라. 다른 말은 쓰지 마라."""


def keyword_context(keyword: str, texts: list[str], width: int = 160) -> str | None:
    """정답 조문에서 핵심어가 나오는 문장(앞뒤 width자). 띄어쓰기를 무시하고 찾는다."""
    pat = re.compile(r"\s*".join(map(re.escape, re.sub(r"\s", "", keyword))))
    for t in texts:
        m = pat.search(t)
        if m:
            a, b = max(0, m.start() - width), min(len(t), m.end() + width)
            return re.sub(r"\s+", " ", t[a:b]).strip()
    return None


def build_prompt(question: str, answer: str, keywords: list[str], contexts: list[str | None] | None = None) -> str:
    lines = []
    for i, k in enumerate(keywords, 1):
        lines.append(f"{i}. {k}")
        if contexts and contexts[i - 1]:
            lines.append(f"   (조문 속 문장: …{contexts[i - 1]}…)")
    return PROMPT.format(question=question, answer=answer.strip(), keywords="\n".join(lines))


def parse(text: str, n: int) -> list[str | None]:
    """'1: 있음' 같은 줄들 → ['yes', ...]. 못 읽은 번호는 None.
    4B 모델은 '1 번: 있음', '1. 청문: 없음', '1. 번호: 틀림'처럼 쓰기도 한다. 줄 첫 숫자를 번호로, 줄의 마지막 판정어를 판정으로 읽고,
    번호가 없는 줄은 순서대로 채운다."""
    out: list[str | None] = [None] * n
    nxt = 0
    for line in text.splitlines():
        words = re.findall(r"있음|없음|틀림", line)
        if not words:
            continue
        m = re.match(r"\s*\**\s*(\d+)", line)
        i = int(m.group(1)) - 1 if m else nxt
        if 0 <= i < n and out[i] is None:
            out[i] = VERDICTS[words[-1]]
        nxt = i + 1
    return out


def judge_keywords(llm, question: str, answer: str, keywords: list[str], gold_texts: list[str] | None = None,
                   mode: str = "plain") -> list[str | None]:
    ctx = [keyword_context(k, gold_texts or []) for k in keywords] if mode == "context" else None
    return parse(llm.generate(build_prompt(question, answer, keywords, ctx)), len(keywords))
