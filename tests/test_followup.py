"""후속 질문 다시 쓰기(agent/followup.py): 모델 없이 형식·전략·실패 처리만 본다."""

from proviso.agent.followup import Turn, history_text, parse_rewrite, rewrite_prompt, standalone
from proviso.agent.resilience import LLMError


class Rep:
    def __init__(self, content):
        self.content = content


class Fake:
    def __init__(self, content):
        self.content, self.calls = content, []

    def chat(self, messages, tools):
        self.calls.append(messages)
        return Rep(self.content)


class Boom:
    def chat(self, messages, tools):
        raise LLMError("down")


H = [Turn("전자금융업자 이용자 손실 책임은?", "전자금융거래법 제9조에 따라 …", ["전자금융거래법 제9조"])]


def test_no_history_or_none_keeps_question():
    assert standalone("그럼 과태료는?", [], "rewrite", Fake("{}"))[0] == "그럼 과태료는?"
    assert standalone("그럼 과태료는?", H, "none")[0] == "그럼 과태료는?"


def test_concat_prepends_previous_question():
    assert standalone("그럼 과태료는?", H, "concat")[0] == "전자금융업자 이용자 손실 책임은? 그럼 과태료는?"


def test_rewrite_uses_model_answer():
    llm = Fake('{"question": "전자금융거래법상 손실 책임 위반 시 과태료는?"}')
    q, info = standalone("그럼 과태료는?", H, "rewrite", llm)
    assert q == "전자금융거래법상 손실 책임 위반 시 과태료는?" and info["ok"] and len(llm.calls) == 1


def test_rewrite_falls_back_on_bad_format_or_error():
    assert standalone("그럼 과태료는?", H, "rewrite", Fake("모르겠음"))[0] == "그럼 과태료는?"
    assert standalone("그럼 과태료는?", H, "rewrite", Boom()) == ("그럼 과태료는?", {"strategy": "rewrite", "ok": False, "error": "연결 실패"})
    assert parse_rewrite('{"question": ""}', "q") == ("q", False)
    assert parse_rewrite('{"question": "' + "가" * 1000 + '"}', "q") == ("q", False)


def test_prompt_wraps_history_in_random_tag():
    p1, p2 = rewrite_prompt("q", H), rewrite_prompt("q", H)
    assert p1 != p2 and "전자금융거래법 제9조" in p1
    assert rewrite_prompt("q", H, tag="hx").count("<hx>") == 2  # 지시문의 언급과 실제 경계


def test_history_keeps_recent_turns_and_cuts_answers():
    turns = [Turn(f"q{i}", "가" * 1000) for i in range(5)]
    t = history_text(turns)
    assert "q1" not in t and "q4" in t and "…" in t


def test_hit_needs_right_version():
    from datetime import date

    from proviso.eval.followup_run import hit

    class Spans:
        def at(self, doc, art, d):
            return (date(2010, 1, 1), date(2017, 1, 1)) if d < date(2017, 1, 1) else (date(2017, 1, 1), None)

    keys = [("법", "제2조"), ("법", "제1조")]
    old = {"doc_id": "법", "article": "제1조", "valid_from": "2010-01-01"}
    now = {"doc_id": "법", "article": "제1조"}
    assert hit(keys, date(2015, 7, 1), old, Spans()) == 2
    assert hit(keys, None, old, Spans()) is None          # 연도를 잃으면 현행 버전이라 틀린다
    assert hit(keys, None, now, Spans()) == 2
    assert hit(keys, date(2015, 7, 1), now, Spans()) is None  # 엉뚱한 연도를 붙여도 틀린다
    assert hit([("법", "제3조")], None, now, Spans()) is None
