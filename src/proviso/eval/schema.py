"""평가셋 형식과 검증. 형식 설명은 docs/plan.md §7.1."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ARTICLE_RE = re.compile(r"^(제\d+조(의\d+)?|별표 \d+(의\d+)?)$")

Category = Literal["lookup", "definition", "cross_ref", "multi", "terms", "temporal", "unanswerable"]
RefusalReason = Literal["out_of_corpus", "individual_case", "investment_advice"]


class Gold(BaseModel):
    """정답 조항. 조 단위가 기본이고 항·호는 있으면 더 엄격하게 채점한다."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    article: str  # '제2조', '제7조의2', '별표 1', '별표 1의2'
    paragraph: int | None = Field(default=None, ge=1)  # 항(①=1)
    item: str | None = None  # 호('10', '3의2')
    valid_from: date | None = None  # 시점 문항: 그 시점에 유효한 조문의 시작일

    @model_validator(mode="after")
    def _check(self) -> "Gold":
        if not ARTICLE_RE.match(self.article):
            raise ValueError(f"조 번호 형식이 아님: {self.article!r} (예: '제2조', '제7조의2', '별표 1')")
        return self


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    set: Literal["retrieval", "agent"]
    split: Literal["dev", "test"]
    question: str = Field(min_length=5)
    category: Category
    answerable: bool
    gold: list[Gold] = []
    as_of: date | None = None  # 기준일. 없으면 '오늘(현행)'
    refusal_reason: RefusalReason | None = None
    must_include: list[str] = []
    tags: list[str] = []  # '위임', '부칙' 등
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    origin: Literal["auto", "ai_verified", "human"]
    lexical_overlap: float | None = Field(default=None, ge=0, le=1)
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> "EvalItem":
        if self.answerable != bool(self.gold):
            raise ValueError("answerable=true이면 gold가 있어야 하고, false이면 gold가 비어 있어야 함")
        if (self.category == "unanswerable") != (not self.answerable):
            raise ValueError("category='unanswerable'과 answerable=false는 함께 써야 함")
        if self.category == "unanswerable" and self.refusal_reason is None:
            raise ValueError("unanswerable 문항은 refusal_reason이 필요함")
        if self.category != "unanswerable" and self.refusal_reason is not None:
            raise ValueError("refusal_reason은 unanswerable 문항에만 씀")
        if self.category == "temporal":
            if self.as_of is None:
                raise ValueError("temporal 문항은 as_of가 필요함")
            if any(g.valid_from is None for g in self.gold):
                raise ValueError("temporal 문항의 gold에는 valid_from이 필요함")
        if self.set == "retrieval" and self.category == "unanswerable":
            raise ValueError("검색 세트에는 unanswerable 문항을 넣지 않음")
        return self


@dataclass
class Report:
    items: list[EvalItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, Counter]:
        return {
            "set/split": Counter(f"{i.set}/{i.split}" for i in self.items),
            "category": Counter(i.category for i in self.items),
            "origin": Counter(i.origin for i in self.items),
        }


def validate_file(path: Path, doc_ids: set[str]) -> Report:
    """JSONL 파일 하나를 읽어 문항별 형식, 파일 수준 규칙(id 중복, doc_id 존재)을 검사한다."""
    report = Report()
    seen: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            where = f"{path.name}:{lineno}"
            try:
                item = EvalItem.model_validate(json.loads(line))
            except json.JSONDecodeError as e:
                report.errors.append(f"{where}: JSON 오류: {e.msg}")
                continue
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(x) for x in err["loc"]) or "(문항)"
                    report.errors.append(f"{where}: {loc}: {err['msg']}")
                continue
            if item.id in seen:
                report.errors.append(f"{where}: id {item.id!r}가 {seen[item.id]}번째 줄과 중복")
            seen[item.id] = lineno
            for g in item.gold:
                if g.doc_id not in doc_ids:
                    report.errors.append(f"{where}: gold.doc_id {g.doc_id!r}가 config/sources.yaml에 없음")
            report.items.append(item)
    return report
