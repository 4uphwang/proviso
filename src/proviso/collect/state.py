"""수집 상태 저장. 중간에 멈춰도 다시 실행하면 끝난 작업은 건너뛴다."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    """작업 키별 상태(pending / done / failed)와 임의의 값을 JSON 파일 하나에 보관한다.

    쓰기는 임시 파일에 쓴 뒤 rename해서, 쓰는 도중에 죽어도 파일이 깨지지 않는다.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"tasks": {}, "values": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def status(self, key: str) -> str:
        return self.data["tasks"].get(key, {}).get("status", "pending")

    def is_done(self, key: str) -> bool:
        return self.status(key) == "done"

    def mark(self, key: str, status: str, error: str | None = None) -> None:
        task = self.data["tasks"].setdefault(key, {"attempts": 0})
        task["status"] = status
        task["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if status == "failed":
            task["attempts"] = task.get("attempts", 0) + 1
            task["error"] = error
        else:
            task.pop("error", None)
        self._save()

    def set_value(self, key: str, value: Any) -> None:
        self.data["values"][key] = value
        self._save()

    def get_value(self, key: str, default: Any = None) -> Any:
        return self.data["values"].get(key, default)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.data["tasks"].values():
            out[t["status"]] = out.get(t["status"], 0) + 1
        return out
