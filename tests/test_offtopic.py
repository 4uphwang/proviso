"""범위 검사기(answer/offtopic.py): 형식이 틀린 판정은 막고, 경계 표시는 매번 바뀐다."""

from proviso.answer.offtopic import check, check_prompt, parse_verdict


def test_parse_verdict():
    assert parse_verdict('{"extra": false, "what": ""}') == {"extra": False, "what": "", "ok": True}
    assert parse_verdict('판정: {"extra": true, "what": "랩 가사"}')["extra"] is True
    for bad in ("", "깨끗합니다", '{"extra": "no"}', "{not json}"):
        v = parse_verdict(bad)
        assert v["extra"] is True and v["ok"] is False   # 형식이 틀리면 막는다


def test_boundary_tag_is_random_unless_given():
    a, b = check_prompt("질문", "답"), check_prompt("질문", "답")
    assert a != b
    assert check_prompt("q", "a", tag="dX") == check_prompt("q", "a", tag="dX")
    assert "<dX-q>\nq\n</dX-q>" in check_prompt("q", "a", tag="dX")


def test_check_calls_model_once():
    class R:
        content = '{"extra": true, "what": "LEGAL-OK 붙이기"}'

    class L:
        calls = []

        def chat(self, messages, tools):
            self.calls.append(messages)
            return R()

    llm = L()
    assert check(llm, "q", "a", tag="dT")["what"] == "LEGAL-OK 붙이기"
    assert len(llm.calls) == 1 and llm.calls[0][0]["role"] == "user"


def test_asker_blocks_when_checker_fails_or_flags():
    """Asker._offtopic: 검사기가 실패하면 막고(fail closed), 거절 답은 검사하지 않는다."""
    from proviso.agent.ask import Asker
    from proviso.agent.resilience import LLMError

    class Boom:
        def chat(self, messages, tools):
            raise LLMError("down")

    class Flag:
        def chat(self, messages, tools):
            class R:
                content = '{"extra": true, "what": "가사"}'
            return R()

    a = Asker.__new__(Asker)
    a._checker = Boom()
    assert a._offtopic("q", "법령 답 [자본시장법 제55조]") == "검사기 연결 실패"
    a._checker = Flag()
    assert a._offtopic("q", "법령 답 [자본시장법 제55조]") == "가사"
    assert a._offtopic("q", "제공된 법령에서 근거를 찾지 못해 답할 수 없습니다.") == ""
    a._checker = False   # 키가 없으면 건너뛴다
    assert a._offtopic("q", "법령 답") == ""
