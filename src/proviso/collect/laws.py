"""현행 법령 수집: 법령명 검색 → 현행 MST → 본문 원본 저장."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from proviso.collect.law_api import LawApiAuthError, LawApiClient, LawApiError
from proviso.collect.state import StateStore
from proviso.config import Sources


def raw_body_path(raw_dir: Path, mst: str) -> Path:
    return raw_dir / "law" / f"{mst}.json"


def pick_current(results: list[dict], name: str) -> dict:
    """검색 결과에서 법령명이 정확히 같은 현행 법령 하나를 고른다."""
    exact = [r for r in results if r.get("법령명한글") == name and r.get("현행연혁코드") == "현행"]
    if len(exact) != 1:
        found = [(r.get("법령명한글"), r.get("현행연혁코드")) for r in results]
        raise LawApiError(f"{name!r}의 현행 법령이 {len(exact)}개(검색 결과: {found[:5]})")
    return exact[0]


def collect_current(
    sources: Sources,
    client: LawApiClient,
    raw_dir: Path,
    state: StateStore,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """sources의 법령마다 현행 본문을 받아 data/raw/law/<MST>.json에 저장한다.

    다시 실행하면 이미 받은 본문은 건너뛴다. 반환값: {doc_id: 현행 MST}.
    """
    current: dict[str, str] = dict(state.get_value("current_mst", {}))
    for src in sources.laws:
        search_key = f"search:{src.id}"
        try:
            if not state.is_done(search_key) or src.id not in current:
                hit = pick_current(client.search_laws(src.query), src.query)
                current[src.id] = hit["법령일련번호"]
                state.set_value("current_mst", current)
                state.mark(search_key, "done")
            mst = current[src.id]
            body_key = f"body:{mst}"
            path = raw_body_path(raw_dir, mst)
            if state.is_done(body_key) and path.exists():
                log(f"  건너뜀 {src.id} (MST {mst}, 이미 받음)")
                continue
            body = client.law_body(mst)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            state.mark(body_key, "done")
            n = len(body["법령"].get("조문", {}).get("조문단위", []))
            log(f"  받음 {src.id} (MST {mst}, 조문단위 {n}개)")
        except LawApiAuthError:
            raise
        except LawApiError as e:
            state.mark(search_key if src.id not in current else f"body:{current[src.id]}", "failed", str(e))
            log(f"  실패 {src.id}: {e}")
    return current
