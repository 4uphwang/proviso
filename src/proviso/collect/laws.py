"""법령 버전 수집: 시행일 법령 목록(eflaw) → 시행일 기준 본문 원본 저장.

버전 하나 = (MST, 시행일). 같은 개정(MST)이라도 부칙에 따라 조문마다 시행일이 다르면 시행일마다 따로 나온다.
본문은 시행일 기준 API(target=eflaw)로 받아, 그 날짜에 실제로 효력이 있는 조문만 담는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from proviso.collect.law_api import LawApiAuthError, LawApiClient, LawApiError
from proviso.collect.state import StateStore
from proviso.config import Sources

CURRENT_STATUSES = {"현행", "시행예정"}


def raw_body_path(raw_dir: Path, mst: str, effective: str) -> Path:
    return raw_dir / "eflaw" / f"{mst}_{effective}.json"


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name or "")


def version_rows(rows: list[dict], name: str) -> list[dict]:
    """목록에서 법령명이 같은 행만 남겨 버전 레코드로 바꾼다(공백은 무시: 옛 법령명은 띄어쓰기가 없다)."""
    out = []
    for r in rows:
        if _norm(r.get("법령명한글")) != _norm(name):
            continue
        out.append({
            "mst": str(r["법령일련번호"]),
            "effective": str(r["시행일자"]),
            "promulgated": str(r.get("공포일자") or ""),
            "revision_kind": r.get("제개정구분명") or "",
            "status": r.get("현행연혁코드") or "",
        })
    out.sort(key=lambda v: (v["effective"], v["promulgated"], v["mst"]))
    return out


def list_versions(client: LawApiClient, name: str) -> list[dict]:
    total, rows = client.eflaw_search(name, 1)
    page = 1
    while len(rows) < total:
        page += 1
        _, more = client.eflaw_search(name, page)
        if not more:
            break
        rows.extend(more)
    versions = version_rows(rows, name)
    if not versions:
        raise LawApiError(f"{name!r}의 시행일 버전이 목록에 없음")
    return versions


def select(versions: list[dict], scope: str) -> list[dict]:
    """scope='current'면 현행·시행예정만, 'all'이면 전부."""
    return versions if scope == "all" else [v for v in versions if v["status"] in CURRENT_STATUSES]


def collect_versions(
    sources: Sources,
    client: LawApiClient,
    raw_dir: Path,
    state: StateStore,
    scope: str = "current",
    log: Callable[[str], None] = print,
) -> dict[str, list[dict]]:
    """법령마다 버전 목록을 받고(state에 저장), scope 범위의 본문을 data/raw/eflaw/<MST>_<시행일>.json에 저장한다.

    목록은 매번 새로 받는다(새 개정 감지). 이미 받은 본문은 건너뛴다. 반환값: {doc_id: 버전 목록 전체}.
    """
    all_versions: dict[str, list[dict]] = dict(state.get_value("versions", {}))
    for src in sources.laws:
        list_key = f"list:{src.id}"
        try:
            versions = list_versions(client, src.query)
            all_versions[src.id] = versions
            state.set_value("versions", all_versions)
            state.mark(list_key, "done")
        except LawApiAuthError:
            raise
        except LawApiError as e:
            state.mark(list_key, "failed", str(e))
            log(f"  실패 {src.id} 목록: {e}")
            continue
        targets = select(versions, scope)
        fetched = skipped = failed = 0
        for v in targets:
            key = f"body:{v['mst']}_{v['effective']}"
            path = raw_body_path(raw_dir, v["mst"], v["effective"])
            if state.is_done(key) and path.exists():
                skipped += 1
                continue
            try:
                body = client.eflaw_body(v["mst"], v["effective"])
            except LawApiAuthError:
                raise
            except LawApiError as e:
                state.mark(key, "failed", str(e))
                failed += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            state.mark(key, "done")
            fetched += 1
        log(f"  {src.id}: 버전 {len(versions)}개 중 {len(targets)}개 대상, 받음 {fetched}, 건너뜀 {skipped}, 실패 {failed}")
    return all_versions
