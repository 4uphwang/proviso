"""설정 파일(config/*.yaml) 로딩."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = ROOT / "config" / "sources.yaml"


class LawSource(BaseModel):
    id: str
    type: Literal["act", "decree"]
    query: str
    parent: str | None = None
    aliases: list[str] = []


class TermsSource(BaseModel):
    id: str
    url: str
    title: str


class Sources(BaseModel):
    laws: list[LawSource]
    terms: list[TermsSource] = []

    @model_validator(mode="after")
    def _check(self) -> "Sources":
        ids = [s.id for s in self.laws] + [t.id for t in self.terms]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"중복된 id: {sorted(dup)}")
        law_ids = {s.id for s in self.laws}
        for s in self.laws:
            if s.type == "decree" and s.parent not in law_ids:
                raise ValueError(f"시행령 {s.id!r}의 parent {s.parent!r}가 laws에 없음")
            if s.type == "act" and s.parent is not None:
                raise ValueError(f"법률 {s.id!r}에는 parent를 둘 수 없음")
        aliases = [a for s in self.laws for a in s.aliases]
        clash = {a for a in aliases if aliases.count(a) > 1 or a in law_ids}
        if clash:
            raise ValueError(f"약칭 충돌: {sorted(clash)}")
        return self

    @property
    def doc_ids(self) -> set[str]:
        return {s.id for s in self.laws} | {t.id for t in self.terms}


def load_sources(path: Path = SOURCES_PATH) -> Sources:
    with open(path, encoding="utf-8") as f:
        return Sources.model_validate(yaml.safe_load(f))
