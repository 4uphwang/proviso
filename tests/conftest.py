import psycopg
import pytest

from proviso.db.connect import database_url

TEST_DB = "proviso_test"


def _test_url() -> str:
    """개발 DB와 같은 서버의 별도 데이터베이스(proviso_test). 실제 수집 데이터와 섞이지 않게 한다."""
    base = database_url()
    return base.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="session")
def test_db_url():
    try:
        with psycopg.connect(database_url(), autocommit=True) as admin:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
            if exists:
                admin.execute(f"DROP DATABASE {TEST_DB} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {TEST_DB}")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"DB 연결 안 됨: {e}")
    return _test_url()


@pytest.fixture
def db(test_db_url):
    """테스트 전용 데이터베이스 연결(로컬: docker compose up -d, CI: 서비스 컨테이너)."""
    conn = psycopg.connect(test_db_url)
    try:
        yield conn
    finally:
        conn.close()
