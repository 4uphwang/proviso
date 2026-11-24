"""에이전트 프롬프트의 층(18장 보완). 모델에게 가는 입력을 이름 붙은 블록으로 나누고, 바뀌는 정도로 층을 정한다.

| 층 | 바뀌는 때 | 블록 | 자리 |
|---|---|---|---|
| fixed | 프로그램이 바뀔 때만 | 도구 정의(tools.SPECS), 역할, 도구 쓰는 법, 인용 형식, 거절 규칙 | tools, 시스템 메시지 |
| question | 질문마다 | 기준일 문구(7장), 질문 | 첫 사용자 메시지 |
| call | 모델 호출마다 | 도구 결과, 마무리 지시(FINISH) | 그 뒤 메시지 |

모델 입력은 fixed → question → call 순서로 쌓인다. 앞부분 캐시(18장)는 앞부분이 같을 때만 읽히므로,
fixed 층에는 질문마다 바뀌는 값을 넣지 않는다(tests/test_prompt.py가 검사한다). {laws}(수집한 법령 이름)는
데이터가 바뀔 때만 바뀌므로 fixed로 둔다.

prompt_version()은 모든 층의 틀을 해시한 값이다. 평가 결과와 기록에 남겨, 어떤 프롬프트로 잰 것인지 가린다.
이 모듈로 옮기면서 모델 입력은 한 글자도 바뀌지 않았다(옛 문자열의 해시로 테스트한다).

정해진 파이프라인(5장 answer/run.py의 BASE·CITE·REFUSE)과 판정자 프롬프트(8·13장)는 이 층 구조 밖이다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from proviso.agent.tools import SPECS
from proviso.answer.run import CITE, REFUSE

FIXED, QUESTION, CALL = "fixed", "question", "call"


@dataclass(frozen=True)
class Block:
    name: str
    layer: str
    text: str
    since: str   # 이 블록을 넣은 장


SYSTEM_BLOCKS: list[Block] = [
    Block("role", FIXED, "너는 한국 금융 법령으로만 답하는 도우미다. 법령: {laws}.\n", "6장"),
    Block("tool_use", FIXED,
          "답하기 전에 반드시 도구로 조문을 찾아 읽어라. 조문에 '대통령령으로 정하는'이 있으면 follow_reference로 시행령을 확인하고, "
          "용어의 뜻을 물으면 define_term을 써라. 필요한 조문을 다 봤으면 도구를 그만 쓰고 한국어로 간결하게 답하라.\n", "6장"),
    Block("cite", FIXED, CITE.replace("위 자료", "도구로 본 자료"), "5장"),
    Block("refuse", FIXED, REFUSE, "5장"),
    # 범위 제한: 법령 질문에 다른 일(코드, 번역, 글쓰기 …)을 끼워 넣으면 그 일까지 해 줬다(범위 문항 dev 6/6)
    Block("scope", FIXED,
          "금융 법령 질문이 아닌 요청(코드 작성, 번역, 글쓰기, 홍보 문구, 법령과 상관없는 계산이나 상식 등)은 하지 마라. "
          "법령 질문과 섞여 있으면 법령 부분만 답하고, 끝에 '법령 질문 외의 요청은 처리하지 않습니다.'라고 한 줄 덧붙여라.\n",
          "범위 제한"),
]

DATE_NOTE = Block("date_note", QUESTION,
                  "\n기준일: {when}. 질문은 이 시점의 규정을 묻는다. 도구는 그날 시행 중이던 조문(현행과 다를 수 있다)을 보여 준다. "
                  "답 첫 문장에 '{when} 시행 조문 기준'이라고 밝히고, 그 조문으로 답하라.{year_only}", "7장, 18장에서 질문 앞으로")
YEAR_ONLY = Block("year_only", QUESTION,
                  " 질문이 연도만 말해서 그해 7월 1일을 기준일로 잡았다. 그해 안에 조문이 바뀌었을 수 있다고 덧붙여라.", "7장")
FINISH = Block("finish", CALL, "도구는 더 쓸 수 없다. 지금까지 도구로 본 자료만으로 처음 질문에 답하라.", "6장")

SYSTEM = "".join(b.text for b in SYSTEM_BLOCKS)   # 틀. {laws}는 run_agent가 채운다
SYSTEM_SCOPED = SYSTEM  # 범위 제한 블록이 기본에 들어갔다(예전 실험 플래그 호환)


def system_message(laws: str) -> str:
    return SYSTEM.format(laws=laws)


def date_note(when: str, year_only: bool) -> str:
    return DATE_NOTE.text.format(when=when, year_only=YEAR_ONLY.text if year_only else "").strip()


def user_message(question: str, note: str = "") -> str:
    return f"{note}\n\n질문: {question}" if note else question


def layers() -> list[Block]:
    """모든 블록(도구 정의 제외), 쌓이는 순서대로."""
    return [*SYSTEM_BLOCKS, DATE_NOTE, YEAR_ONLY, FINISH]


def prompt_version() -> str:
    """도구 정의와 모든 블록의 틀을 합친 해시 앞 8자리. 한 글자라도 바뀌면 달라진다."""
    payload = json.dumps({"tools": SPECS, "blocks": [[b.name, b.layer, b.text] for b in layers()]},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]
