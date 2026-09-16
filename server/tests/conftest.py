import os
import sqlite3
import sys

import pytest

# Делаем server/ импортируемым как пакет верхнего уровня: `import server_app`
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import server_app


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """
    Временная SQLite-база на каждый тест.

    db_connect() в server_app читает модульную переменную DB_PATH при каждом
    вызове, поэтому достаточно подменить её через monkeypatch и вызвать
    ensure_schema(), которая создаст реальные таблицы в файле.
    """
    path = tmp_path / "test_server.db"
    monkeypatch.setattr(server_app, "DB_PATH", str(path))
    server_app.ensure_schema()
    yield str(path)
    # Соединений, которые держит server_app, после вызовов не остаётся
    # (каждая функция открывает и закрывает своё), поэтому просто удаляем файл.
    if path.exists():
        path.unlink()


@pytest.fixture
def db(db_path):
    """Отдельное соединение для проверок из теста (чтение/запись напрямую)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
