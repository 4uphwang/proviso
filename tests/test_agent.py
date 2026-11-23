"""에이전트(6장): 도구와 루프의 규칙. 모델과 검색기는 가짜를 쓴다."""

from proviso.agent.loop import FINISH, Reply, run_agent
from proviso.agent.tools import CUT, Law, Toolbox, focus
from proviso.answer.cite import LawNames

NAMES = LawNames([("가상금융법", "가상법"), ("가상법", "가상법"), ("가상금융법 시행령", "가상법 시행령")], {"가상법 시행령": "가상법"})


def art(doc, title, text):
    return {"doc_title": doc, "title": title, "valid_from": "2024-01-01", "text": text}


ARTS = {
    ("가상법", "제2조"): art("가상금융법", "정의", '제2조(정의) 이 법에서 사용하는 용어의 뜻은 다음과 같다.\n1. "전자지갑"이란 다음 각 목의 것을 말한다.\n가. 앱\n나. 카드\n2. "이용자"란 쓰는 사람을 말한다.'),
    ("가상법", "제5조"): art("가상금융법", "등록", "제5조(등록) ① 대통령령으로 정하는 자기자본을 갖추어야 한다.\n② 제2조의 전자지갑에 적용한다."),
    ("가상법 시행령", "제3조"): art("가상금융법 시행령", "자기자본", '제3조(자기자본) 법 제5조제1항에서 "대통령령으로 정하는 자기자본"이란 5억원을 말한다.'),
}
LAW = Law(ARTS, refs={("가상법", "제5조"): [("가상법", "제2조")]}, down={("가상법", "제5조"): [("가상법 시행령", "제3조")]},
          up={("가상법 시행령", "제3조"): [("가상법", "제5조")]})


def box():
    return Toolbox(LAW, NAMES, lambda q: [("가상법", "제5조")])


def test_focus_keeps_relevant_lines_in_order():
    text = "제1조(업무)\n" + "\n".join(f"{i}. 무관한 업무 {'가' * 80}" for i in range(1, 30)) + "\n30. 수탁증권의 보관\n31. 끝"
    out = focus(text, "수탁증권은 누가 보관하나", 400)
    assert "30. 수탁증권의 보관" in out and out.startswith("제1조(업무)") and CUT in out and len(out) < 500
    assert focus("짧은 글", "질문", 100) == "짧은 글"


def test_tools_resolve_names_and_links():
    r = box().call("get_article", {"law": "가상금융법", "article": "5"}, "q")
    assert r.shown == [("가상법", "제5조")] and "위임받은 시행령: 가상금융법 시행령 제3조" in r.text
    f = box().call("follow_reference", {"law": "가상법", "article": "제5조"}, "q")
    assert f.shown == [("가상법 시행령", "제3조"), ("가상법", "제2조")] and "5억원" in f.text
    back = box().call("follow_reference", {"law": "가상금융법 시행령", "article": "제3조"}, "q")
    assert back.shown == [("가상법", "제5조")]


def test_search_link_hints():
    r = Toolbox(LAW, NAMES, lambda q: [("가상법", "제5조")], link_hints=True).search("x")
    assert "가상금융법 시행령 제3조(자기자본)(위임받은 시행령)" in r.text and r.shown == [("가상법", "제5조")]  # 이름만 보인 조문은 근거가 아니다
    assert "이어진 조문" not in box().search("x").text


def test_define_term_shows_children_only():
    r = box().call("define_term", {"term": "전자 지갑"}, "q")
    assert r.shown == [("가상법", "제2조")] and "나. 카드" in r.text and "이용자" not in r.text


def test_tool_errors_are_text_not_exceptions():
    for name, args in [("get_article", {"law": "없는법", "article": "제1조"}), ("get_article", {"law": "가상법", "article": "제999조"}),
                       ("get_article", {"law": "가상법"}), ("nope", {}), ("search", "문자열"), ("search", {"query": "x", "extra": "y"})]:
        assert box().call(name, args, "q").error, (name, args)


class Script:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, tools):
        self.seen.append((list(messages), tools))
        return self.replies.pop(0)


def call(name, **args):
    return Reply("", [{"function": {"name": name, "arguments": args}}], 100, 10)


def test_loop_collects_shown_articles():
    llm = Script(call("search", query="자기자본"), call("follow_reference", law="가상법", article="제5조"), Reply("5억원 [가상금융법 시행령 제3조]", []))
    t = run_agent("자기자본 얼마?", llm, box())
    assert t.stop == "answered" and t.tool_calls == ["search", "follow_reference"]
    assert t.shown == [("가상법", "제5조"), ("가상법 시행령", "제3조"), ("가상법", "제2조")] and "5억원" in t.context


def test_loop_stops_on_repeat_and_answers_without_tools():
    llm = Script(call("search", query="x"), call("search", query="x"), Reply("끝", []))
    t = run_agent("q", llm, box())
    assert (t.stop, t.answer) == ("repeat", "끝")
    last, tools = llm.seen[-1]
    assert tools is None and last[-1]["content"] == FINISH


def test_loop_limits():
    t = run_agent("q", Script(*[call("search", query=str(i)) for i in range(3)], Reply("끝", [])), box(), max_turns=3)
    assert t.stop == "turns"
    t = run_agent("q", Script(call("search", query="1"), Reply("끝", [])), box(), token_limit=50)
    assert t.stop == "tokens"


def test_seed_search_runs_before_model():
    llm = Script(Reply("답", []))
    t = run_agent("q", llm, box(), seed_search=True)
    assert t.tool_calls == ["search"] and llm.seen[0][0][-1]["role"] == "tool"


def test_seed_more_appends_to_first_search():
    llm = Script(Reply("답", []))
    b = box()
    t = run_agent("자기자본", llm, b, seed_search=True, seed_more=lambda q, shown: b.attach([("가상법 시행령", "제3조")], q))
    assert t.tool_calls == ["search"] and t.shown == [("가상법", "제5조"), ("가상법 시행령", "제3조")]
    assert "5억원" in llm.seen[0][0][-1]["content"] and "이어진 조문" in t.context
    assert b.attach([("없는법", "제1조")], "q") is None


def test_version_flag_needs_same_text_period():
    from datetime import date as D
    from types import SimpleNamespace as NS

    from proviso.agent.run import version_flag
    from proviso.agent.tools import Law

    it = NS(as_of=D(2014, 4, 9), answerable=True, gold=[NS(doc_id="법", article="제1조", valid_from=D(2013, 8, 29))])
    law = Law({("법", "제1조"): {"valid_from": D(2014, 1, 1)}}, {}, {})
    period = {("법", "제1조", D(2013, 8, 29)): D(2013, 8, 29), ("법", "제1조", D(2014, 1, 1)): D(2013, 8, 29)}
    assert version_flag(it, {("법", "제1조")}, law, period) is True
    period[("법", "제1조", D(2014, 1, 1))] = D(2014, 1, 1)
    assert version_flag(it, {("법", "제1조")}, law, period) is False
    assert version_flag(it, set(), law, period) is False
    assert version_flag(NS(as_of=None, answerable=True, gold=[]), set(), law, period) is None


def test_year_notice_only_for_year_only_and_changed_cites():
    from datetime import date as D

    from proviso.agent.run import year_notice
    from proviso.agent.tools import Law

    class H:
        def changes(self, doc, art, start, end):
            return [D(2014, 9, 1)] if art == "제1조" else []

    law = Law({("법", "제1조"): {"doc_title": "어느 법", "title": "목적"}, ("법", "제2조"): {"doc_title": "어느 법", "title": "정의"}}, {}, {})
    cite = lambda art, st="shown": {"doc": "법", "article": art, "status": st}
    note = year_notice("2014년 당시 규정은?", D(2014, 7, 1), [cite("제1조")], law, H())
    assert note and "어느 법 제1조(목적)(9월 1일 시행)" in note
    assert year_notice("2014년 당시 규정은?", D(2014, 7, 1), [cite("제2조")], law, H()) is None
    assert year_notice("2014년 3월 당시 규정은?", D(2014, 3, 15), [cite("제1조")], law, H()) is None
    assert year_notice("2014년 당시 규정은?", D(2014, 7, 1), [cite("제1조", "unseen")], law, H()) is None
    assert year_notice("규정은?", None, [cite("제1조")], law, H()) is None
