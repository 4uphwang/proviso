"""18장 보완: 프롬프트 층. 블록으로 나눈 뒤에도 모델 입력이 예전과 한 글자도 같고, 고정 층에 바뀌는 값이 없다."""

import hashlib
import json
import re

from proviso.agent import prompt as P
from proviso.agent.tools import SPECS

h = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731

# 블록으로 나누기 전(커밋 4a36229)의 문자열 해시(시스템 지시는 범위 제한을 넣은 뒤 값). 프롬프트를 일부러 바꾸면 여기와 prompt_version이 함께 바뀐다.
OLD = {
    # 범위 제한 블록을 넣으며 일부러 바꿨다(나누기 직후 값 10b38749…). 정상 답 회귀는 test 171문항으로 확인
    "system": "048545b95eb39a4fab3ce0c66b05fa8f72dfb46ac882a973d1e445ab8766308b",
    "finish": "03ed486f56804fa48dda8b80bbea8627c098905acc6a89be745f3e77e0972e7b",
    "date_note": "31f4b4cd5eaa72e07ef42095c61f9373553b0e08941502cb5114eeb40e019d56",
    "year_only": "5402d27f0ccb18ea9934efa0b6b6b63662f66cc861076171d8c3ed62e58c7985",
    "tools": "eff149f367fdc2d6704fa9ebe5b8b3a9f3e4352b248e635c0ab33c3b433deb00",
}


def test_model_input_is_byte_identical_to_before():
    assert h(P.SYSTEM) == OLD["system"]
    assert h(P.FINISH.text) == OLD["finish"]
    assert h(P.DATE_NOTE.text) == OLD["date_note"]
    assert h(P.YEAR_ONLY.text) == OLD["year_only"]
    assert h(json.dumps(SPECS, ensure_ascii=False, sort_keys=True)) == OLD["tools"]


def test_fixed_layer_has_no_per_question_values():
    """고정 층(시스템 지시)이 질문마다 바뀌면 앞부분 캐시가 깨진다(18장: 기준일 문구가 그랬다)."""
    for b in P.SYSTEM_BLOCKS:
        assert b.layer == P.FIXED
        assert set(re.findall(r"\{(\w+)\}", b.text)) <= {"laws"}, b.name   # 법령 목록 말고는 채울 칸이 없다
        assert not re.search(r"\d{4}\s*년|\d{4}-\d{2}-\d{2}|기준일", b.text), b.name


def test_layers_stack_fixed_then_question_then_call():
    order = {P.FIXED: 0, P.QUESTION: 1, P.CALL: 2}
    ranks = [order[b.layer] for b in P.layers()]
    assert ranks == sorted(ranks)


def test_messages_assembled_from_layers():
    note = P.date_note("2015년 7월 1일", year_only=True)
    assert note.startswith("기준일: 2015년 7월 1일.") and "그해 7월 1일을 기준일로" in note
    assert P.user_message("질문?", note) == f"{note}\n\n질문: 질문?"
    assert P.user_message("질문?") == "질문?"
    assert P.system_message("A법, B법").startswith("너는 한국 금융 법령으로만 답하는 도우미다. 법령: A법, B법.")


def test_prompt_version_changes_with_any_block(monkeypatch):
    v = P.prompt_version()
    assert len(v) == 8
    monkeypatch.setattr(P, "FINISH", P.Block("finish", P.CALL, P.FINISH.text + " ", "test"))
    assert P.prompt_version() != v
