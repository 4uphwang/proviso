from proviso.answer.cite import is_refusal
from proviso.answer.guard import REFUSAL, screen


def test_invest_and_personal_are_screened():
    assert screen("지금 삼성전자 사도 됨?") == "invest"
    assert screen("수익률 좋은 채권 추천") == "invest"
    assert screen("내년 코스피 전망") == "invest"
    assert screen("제가 어제 보이스피싱으로 500만원을 잃었는데 제 경우 은행이 전액 물어줘야 하나요?") == "personal"
    assert screen("우리 팀장 문자 내용 미공개정보 맞지?") == "personal"


def test_legal_questions_pass():
    assert screen("유튜브나 방송에서 불특정 다수에게 종목 추천을 하는 것도 투자자문업에 해당하나요?") is None
    assert screen("거래소 이사장 후보추천위원 구성") is None
    assert screen("펀드평가회사 등록요건 머임? 증권사 계열사도 됨?") is None
    assert screen("온라인소액투자중개업자 자기자본 얼마 필요해?") is None


def test_guard_refusals_count_as_refusals():
    assert all(is_refusal(v) for v in REFUSAL.values())
