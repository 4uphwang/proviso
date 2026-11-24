"""난독화 변형 문항: evalset/attack/attacks_obf.jsonl. 사람이 고른 공격 42개(확인 30 + 범위 섞인 요청 test 12)를
세 가지로 바꾼다. 정규식 규칙이 문자 모양에 기대는지 보려는 것이다(조사 보고서: 보이지 않는 문자로 분류기 70~100% 우회).

- zw: 글자 사이마다 보이지 않는 문자(U+200B 제로폭 공백)를 넣는다
- fw: 영문·숫자를 전각(ＡＢＣ１２３)으로 바꾼다
- hg: 영문 일부를 모양이 같은 키릴 문자(а, е, о, р, с, х, і)로 바꾼다
판정은 원래 문항 그대로다. 개인정보 문항은 바뀐 값도 원문으로 본다(가려지지 않고 모델에 가면 통함).
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
HG = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "i": "і",
                    "A": "А", "E": "Е", "O": "О", "P": "Р", "C": "С", "X": "Х"})


def zw(s: str) -> str:
    return "​".join(s)


def fw(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if ("!" <= c <= "~" and c.isalnum()) else c for c in s)


def hg(s: str) -> str:
    return s.translate(HG)


base = [json.loads(line) for line in (HERE / "attacks_holdout.jsonl").read_text(encoding="utf-8").splitlines()]
base += [d for d in map(json.loads, (HERE / "scope.jsonl").read_text(encoding="utf-8").splitlines())
         if d["split"] == "test" and d["kind"] == "mixed"]
out = []
for d in base:
    for name, f in (("zw", zw), ("fw", fw), ("hg", hg)):
        v = {**d, "id": f"{d['id']}-{name}", "question": f(d["question"]), "obf": name,
             "notes": d["notes"] + f" 난독화 변형({name})."}
        if d.get("pii"):
            v["pii"] = sorted({*d["pii"], *(f(p) for p in d["pii"])})
        out.append(v)
p = HERE / "attacks_obf.jsonl"
p.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in out), encoding="utf-8")
print(p, len(base), "→", len(out))
