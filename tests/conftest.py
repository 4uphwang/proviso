import pytest

from proviso.db.connect import connect


@pytest.fixture
def db():
    """Postgres 연결. 연결이 안 되면 건너뛴다(로컬: docker compose up -d, CI: 서비스 컨테이너)."""
    try:
        conn = connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"DB 연결 안 됨: {e}")
    try:
        yield conn
    finally:
        conn.close()
