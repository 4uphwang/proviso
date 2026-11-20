"""DB 연결과 스키마 적용."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from proviso.config import ROOT

SCHEMA_PATH = ROOT / "db" / "schema.sql"
DEFAULT_URL = "postgresql://proviso:proviso@localhost:5433/proviso"


def database_url() -> str:
    load_dotenv(ROOT / ".env")
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def apply_schema(conn: psycopg.Connection, path: Path = SCHEMA_PATH) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def table_names(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {r[0] for r in rows}
