import os
import sys

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

# Делаем server/ импортируемым как пакет верхнего уровня: `import server_app`
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import server_app

# ---------------------------------------------------------------------------
# Тестовая база: отдельная БД в том же Postgres, что и docker-compose.
# URL берётся из MONITORING_TEST_DATABASE_URL; если его нет — из
# MONITORING_DATABASE_URL с именем базы + "_test". Рабочие данные не трогаются.
# ---------------------------------------------------------------------------


def _test_database_url() -> str:
    url = os.environ.get("MONITORING_TEST_DATABASE_URL", "").strip()
    if url:
        return url
    base = os.environ.get("MONITORING_DATABASE_URL", "").strip()
    if base:
        params = conninfo_to_dict(base)
        params["dbname"] = f"{params.get('dbname') or 'monitoring'}_test"
        return make_conninfo(**params)
    pytest.fail(
        "Тестам нужен Postgres: задайте MONITORING_TEST_DATABASE_URL "
        "(например postgresql://monitoring:пароль@localhost:5432/monitoring_test) "
        "или MONITORING_DATABASE_URL. Поднять базу: docker compose up -d postgres"
    )


def _ensure_test_database(url: str) -> None:
    """Создаёт тестовую БД, если её ещё нет (через служебную базу postgres)."""
    params = conninfo_to_dict(url)
    dbname = params.get("dbname") or "monitoring_test"
    if not str(dbname).endswith("_test"):
        pytest.fail(f"Имя тестовой БД должно оканчиваться на _test, получено: {dbname!r}")
    admin = dict(params, dbname="postgres")
    try:
        with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    except psycopg.OperationalError as e:
        pytest.fail(f"Не удалось подключиться к Postgres ({admin.get('host')}:{admin.get('port')}): {e}")


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = _test_database_url()
    _ensure_test_database(url)
    # Схема создаётся кодом сервера (ensure_schema идемпотентна), а не тестами
    os.environ["MONITORING_DATABASE_URL"] = url
    server_app.ensure_schema()
    return url


@pytest.fixture(autouse=True)
def _use_test_database(test_db_url, monkeypatch):
    """Каждый тест ходит только в тестовую БД (server_app читает URL из env)."""
    monkeypatch.setenv("MONITORING_DATABASE_URL", test_db_url)
    yield


def _truncate_all(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        tables = [r[0] for r in rows]
        if tables:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tables)
                )
            )


@pytest.fixture
def db(test_db_url):
    """
    Отдельное соединение для проверок из теста (r["column"], как в коде сервера).
    autocommit — чтобы соединение не держало блокировок и не мешало TRUNCATE.
    После теста все таблицы очищаются: код сервера коммитит через собственные
    соединения, поэтому изоляция через rollback одной транзакции невозможна.
    """
    conn = psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
        _truncate_all(test_db_url)
