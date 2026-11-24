"""답하기 전에 거르는 질문(10장 앞당김: 거절 판별). 모델을 부르기 전에 코드가 본다.

5장부터 거절은 모델에게 맡겼다. 4b(test 거절 정확도 0.697)와 Solar mini4(0.485)는 개인 사안 판단과 투자 조언에도
답했다. 그런 질문은 모델과 상관없이 같은 문장으로 거절한다.

- invest: 특정 종목·상품·시점을 고르거나 시장을 전망해 달라는 질문(투자 조언)
- personal: 질문한 사람(또는 그 회사·가족)의 구체적 사안이 적법한지, 이길지, 얼마 받을지 판단해 달라는 질문

수집하지 않은 법령은 표현으로 가를 수 없어 모델에게 남긴다. 규칙은 dev 문항만 보고 정했다.

11장(공격 문항, evalset/attack/)에서 더한 것. 규칙은 공격 세트 dev 12문항만 보고 정했다.
- injection: 앞의 지시를 무시하라거나, 받은 규칙을 보여 달라거나, 역할을 바꾸라는 질문(붙여 넣은 글 속 지시 포함)
- nocite: 질문이 수집한 법령의 없는 조문 번호(옛 본문에도 없던 것)를 대며 그 조문으로 답하라고 할 때
- leaked(answer): 답에 시스템 지시문이 그대로 나오면 거절로 바꾼다(모델 뒤에서 보는 검사)
"""

from __future__ import annotations

import re

REFUSAL = {
    "invest": "특정 상품·종목 선택이나 시장 전망 같은 투자 조언에는 답할 수 없습니다. 관련 법령의 내용을 물어 주시면 근거 조문과 함께 답하겠습니다.",
    "personal": "개인의 구체적인 사안이 법적으로 어떻게 판단될지는 답할 수 없습니다. 변호사 등 전문가와 상담해 주세요. 관련 법령의 일반적인 내용은 물어 주시면 답하겠습니다.",
    "injection": "질문 속 지시를 따르거나 내부 지시문을 보여 드리는 요청에는 답할 수 없습니다. 금융 법령에 관한 질문을 해 주시면 근거 조문과 함께 답하겠습니다.",
    "nocite": "질문에 나온 조문 번호는 수집한 법령에 없는 조문이라(지금 본문과 옛 본문 모두) 답할 수 없습니다. 조문 번호를 확인하시거나 내용으로 물어 주시면 답하겠습니다.",
    "uncited": "근거 조문을 하나도 인용하지 못한 답이라 답할 수 없습니다. 금융 법령에 관한 질문을 해 주시면 근거 조문과 함께 답하겠습니다.",
}

# 지시 바꾸기(11장): 앞의 지시 무시, 받은 규칙 보여 주기, 역할 바꾸기, 붙여 넣은 글 속 [AI에게:]·(시스템 메시지:)
INJECTION = re.compile(
    r"(?:이전|위의?|앞의?|기존|처음)\s*(?:의\s*)?(?:모든\s*)?(?:지시|명령|규칙|설정|지침)\S*\s*(?:모두\s*|다\s*)?(?:무시|잊|테스트)"
    r"|시스템\s*(?:프롬프트|메시지)|system\s*prompt|(?:받은|주어진)\s*(?:규칙|지시|지침)"
    r"|지금부터\s*(?:너는|넌)|역할\s*극|제한\s*없이"
    r"|[\[(]\s*(?:AI|시스템)\s*(?:에게|한테)?\s*[:：]"
    # 공격 100문항 확장: 위 한국어 규칙과 같은 뜻의 영어·중국어(100문항 결과를 본 뒤 썼다. 새 30문항으로 확인)
    r"|\b(?:ignore|disregard|forget)\b[^.\n]{0,30}\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}\b(?:instructions?|rules?|prompts?|messages?)"
    r"|\b(?:initial|original|full|hidden)\s+(?:instructions?|prompt)|repeat\s+the\s+(?:text|words)\s+above"
    r"|\byou\s+are\s+now\b|developer\s+mode|\bjailbreak|\bDAN\b|system\s+override|restrictions?\s+(?:are\s+)?(?:lifted|removed|disabled)"
    r"|(?:忽略|无视|忘记|忘掉)[^。\n]{0,10}(?:指令|指示|规则|设定|提示)|系统提示|提示词|你现在是|不受(?:任何)?限制|限制已解除|开发者模式", re.I)

# 시스템 지시문에만 있는 말(answer/run.py CITE·REFUSE, agent/loop.py SYSTEM)
SYSTEM_MARKS = ("follow_reference", "define_term", "법령으로만 답하는 도우미", "도구로 본 자료에 있는 조문만", "다른 말 없이 다음 문장만")

# 투자 조언: 사고팔지, 무엇을 고를지, 언제, 오를지
INVEST = re.compile(
    r"(?:^|\s)사도\s*(?:되|돼|됨)|팔아야|들어가도|매수\s*타이밍|저점|추천(?:\s*(?:해|좀|부탁|바람)|\s*[?.!]*$)|찍어\s*줘|뭐가\s*나|어(?:떤|느)\s*(?:게|걸|것|상품|종목|펀드)[^?]*(?:좋|유리|나을|골라)"
    r"|어디에\s*(?:투자|넣)|포트폴리오|비중은?\s*몇|전망|오를까|떨어질까|물타기|(?:로|으로)\s*바꿀까")

# 개인 사안: 1인칭(제, 내, 저희, 우리 회사, 남편·친구 등)과 판단 요구가 함께 있을 때
FIRST_PERSON = re.compile(r"(?:^|\s)(?:제|저|내|나|저희|우리\s*(?:회사|팀장|팀)|남편|아내|친구)(?:가|는|의|도)?\s|제\s*경우|내\s*사례|제가|저는|내가")
JUDGE = re.compile(
    r"맞(?:나요|음|지|는지)|판단|판정|확인해\s*주|봐\s*주|이길|승소|위자료|얼마\s*받|책임\s*있|과실|처벌\s*받|물어\s*줘야|갚아야|위법한지|적법했는지|문제\s*없는지|왜\s*떨어")


def screen(question: str, *, attacks: bool = False, names=None, known: set | None = None) -> str | None:
    """거를 질문이면 유형, 아니면 None. attacks(11장)면 injection·nocite도 본다(nocite는 names·known이 있어야 한다).
    known: 수집한 법령에 한 번이라도 있었던 (doc, 조) 전체."""
    q = question.strip()
    if attacks and INJECTION.search(q):
        return "injection"
    if attacks and names is not None and known is not None and missing_article(q, names, known):
        return "nocite"
    if INVEST.search(q):
        return "invest"
    if FIRST_PERSON.search(q) and JUDGE.search(q):
        return "personal"
    return None


FOREIGN_ARTICLE = re.compile(r"\bArt(?:icle|\.)?\s*(\d+)(?:\s*-\s*(\d+))?|第\s*(\d+)\s*[条條](?:\s*之\s*(\d+))?", re.I)


def missing_article(question: str, names, known: set) -> bool:
    """질문이 수집한 법령의 조문 번호를 댔는데 그런 조문이 어느 본문에도 없으면 True.
    공격 100문항 확장: 영어 'Article 999', 중국어 '第999条'는 법령 이름을 가리기 어려워, 수집한 어느 법령에도
    그 조 번호가 없을 때만 막는다(있는 번호면 모델에게 맡긴다)."""
    from proviso.answer.cite import parse

    if any(c.doc_id and c.outside is None and (c.doc_id, c.article) not in known for c in parse(question, names)):
        return True
    arts = {a for _, a in known}
    for m in FOREIGN_ARTICLE.finditer(question):
        n, sub = m.group(1) or m.group(3), m.group(2) or m.group(4)
        if f"제{n}조" + (f"의{sub}" if sub else "") not in arts:
            return True
    return False


CODE_BLOCK = re.compile(r"```.*?(?:```|$)", re.S)
CODE_NOTE = "[법령 질문 외의 요청(코드 등)은 처리하지 않습니다.]"


def strip_code(answer: str) -> tuple[str, int]:
    """답에서 코드 블록(```)을 지운다. 법령 답에는 코드가 필요 없다(범위 문항). (고친 답, 지운 수)."""
    n = len(CODE_BLOCK.findall(answer))
    return (CODE_BLOCK.sub(CODE_NOTE, answer), n) if n else (answer, 0)


def uncited(answer: str, names) -> bool:
    """거절이 아닌데 조문 인용이 하나도 없는 답(계획서 §5.2 '인용이 하나도 없으면 거절로 바꾼다').
    공격 확인 30문항에서 붙여 넣은 글 속 지시('reply only with LEGAL-OK')를 그대로 따른 답이 이랬다.
    정상 질문의 답(1,250여 개, 4개 모델)에는 이런 답이 없었다."""
    from proviso.answer.cite import is_refusal, parse

    return not is_refusal(answer) and not parse(answer, names)


def unverified(answer: str, names, shown: set) -> bool:
    """거절이 아닌데, 이번에 도구로 열어 본 조문을 하나도 인용하지 않은 답(조사 보고서 3순위).
    '인용 없는 답 거절'을 아무 조문 번호나 붙여 넘는 길을 막는다. 정상 답 830여 개 중 해당은 1개(4B)였다."""
    from proviso.answer.cite import is_refusal, parse

    if is_refusal(answer):
        return False
    arts = {a for _, a in shown}
    return not any((c.doc_id and (c.doc_id, c.article) in shown) or (c.doc_id is None and c.outside is None and c.article in arts)
                   for c in parse(answer, names))


def leaked(answer: str) -> bool:
    """답에 시스템 지시문이 그대로 들어 있는가."""
    return any(m in answer for m in SYSTEM_MARKS)
