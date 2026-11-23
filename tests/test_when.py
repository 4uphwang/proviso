from datetime import date

from proviso.retrieve.when import question_date

T = date(2026, 11, 23)


def test_year_only_is_mid_year():
    assert question_date("2014년 당시 신용정보회사가 내 정보를", T) == date(2014, 7, 1)
    assert question_date("2022 금소법 연대보증 금지 리스도 적용", T) == date(2022, 7, 1)


def test_full_and_month():
    assert question_date("2019년 3월 2일 기준 사원 수 한도", T) == date(2019, 3, 2)
    assert question_date("2019.3.2. 기준", T) == date(2019, 3, 2)
    assert question_date("2019년 3월 무렵", T) == date(2019, 3, 15)


def test_amounts_are_not_years():
    assert question_date("자기자본 2000만원 이상", T) is None
    assert question_date("1000개 이상이면", T) is None


def test_current_year_is_current():
    assert question_date("2026년 현재 기준", T) is None
    assert question_date("자본금은 얼마인가요?", T) is None
