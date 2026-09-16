"""
Релизы клиентского агента: POST /api/client-release (публикация, api_key),
GET /api/client-release и GET /api/download/client-agent (client_update_key).
Хранилище — таблица client_releases в тестовом Postgres (conftest.py).
"""

import hashlib

import pytest
import server_app
from fastapi.testclient import TestClient
from server_app import insert_events

API_KEY = "TEST_API_KEY"
CLIENT_KEY = "TEST_CLIENT_KEY"
PUBLISH_HEADERS = {"X-API-Key": API_KEY}
AGENT_HEADERS = {"X-Client-Key": CLIENT_KEY}


@pytest.fixture
def client(db, monkeypatch):
    # Ключи задаём явно через env (приоритет над config.json), чтобы тест
    # не зависел от значений в репозиторном config.json и окружении разработчика
    monkeypatch.setenv("MONITORING_API_KEY", API_KEY)
    monkeypatch.setenv("MONITORING_CLIENT_UPDATE_KEY", CLIENT_KEY)
    return TestClient(server_app.app)


def publish(client, version, data: bytes, filename="client_agent.exe", headers=PUBLISH_HEADERS):
    return client.post(
        "/api/client-release",
        data={"version": version},
        files={"file": (filename, data, "application/octet-stream")},
        headers=headers,
    )


# ------------------------------------------------------------ POST (publish)

class TestPublishClientRelease:
    def test_publish_ok(self, client, db):
        payload = b"MZ" + bytes(range(256)) * 10
        r = publish(client, "1.4.2", payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == "1.4.2"
        assert body["sha256"] == hashlib.sha256(payload).hexdigest()
        assert body["size_bytes"] == len(payload)

        row = db.execute("SELECT * FROM client_releases").fetchone()
        assert row["version"] == "1.4.2"
        assert row["filename"] == "client_agent.exe"
        assert row["size_bytes"] == len(payload)
        assert bytes(row["data"]) == payload
        assert row["uploaded_at"]

    def test_version_is_stripped(self, client):
        r = publish(client, "  2.0 ", b"x")
        assert r.status_code == 200
        assert r.json()["version"] == "2.0"

    @pytest.mark.parametrize("bad", ["", "abc", "v1.2", "1", "1.", ".1", "1..2", "1.2-beta", "1.2 3"])
    def test_invalid_version_gives_400(self, client, db, bad):
        r = publish(client, bad, b"x")
        assert r.status_code == 400, r.text
        assert db.execute("SELECT COUNT(*) FROM client_releases").fetchone()["count"] == 0

    def test_empty_file_gives_400(self, client):
        r = publish(client, "1.0", b"")
        assert r.status_code == 400

    def test_missing_api_key_gives_401(self, client, db):
        r = publish(client, "1.0", b"x", headers={})
        assert r.status_code == 401
        assert db.execute("SELECT COUNT(*) FROM client_releases").fetchone()["count"] == 0

    def test_wrong_api_key_gives_401(self, client):
        assert publish(client, "1.0", b"x", headers={"X-API-Key": "nope"}).status_code == 401

    def test_client_key_is_not_enough_to_publish(self, client):
        # публикация — привилегированное действие, ключ агента не подходит
        assert publish(client, "1.0", b"x", headers=AGENT_HEADERS).status_code == 401

    def test_filename_is_basename_only(self, client, db):
        r = publish(client, "1.0", b"x", filename="..\\evil\\client_agent.exe")
        assert r.status_code == 200
        row = db.execute("SELECT filename FROM client_releases").fetchone()
        assert "\\" not in row["filename"] and "/" not in row["filename"]


# ------------------------------------------------------- GET /api/client-release

class TestGetClientRelease:
    def test_empty_table_gives_null(self, client):
        r = client.get("/api/client-release", headers=AGENT_HEADERS)
        assert r.status_code == 200
        assert r.json() == {"client_release": None}

    def test_returns_latest_metadata_without_data(self, client):
        publish(client, "1.0", b"old")
        new = b"new-binary"
        publish(client, "1.1", new)
        r = client.get("/api/client-release", headers=AGENT_HEADERS)
        assert r.status_code == 200
        assert r.json() == {
            "client_release": {
                "version": "1.1",
                "sha256": hashlib.sha256(new).hexdigest(),
                "size_bytes": len(new),
            }
        }

    def test_requires_client_key(self, client):
        publish(client, "1.0", b"x")
        assert client.get("/api/client-release").status_code == 401
        assert client.get("/api/client-release", headers={"X-Client-Key": "nope"}).status_code == 401
        # api_key не является ключом агента
        assert client.get("/api/client-release", headers=PUBLISH_HEADERS).status_code == 401


# ------------------------------------------------- GET /api/download/client-agent

class TestDownloadClientAgent:
    def test_404_without_releases(self, client):
        r = client.get("/api/download/client-agent", headers=AGENT_HEADERS)
        assert r.status_code == 404

    def test_download_returns_latest_bytes(self, client):
        publish(client, "1.0", b"old")
        payload = bytes(range(256)) * 100
        publish(client, "1.1", payload, filename="client_agent.exe")
        r = client.get("/api/download/client-agent", headers=AGENT_HEADERS)
        assert r.status_code == 200
        assert r.content == payload
        assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(payload).hexdigest()
        assert r.headers["content-type"] == "application/octet-stream"
        assert 'filename="client_agent.exe"' in r.headers["content-disposition"]

        meta = client.get("/api/client-release", headers=AGENT_HEADERS).json()["client_release"]
        assert meta["sha256"] == hashlib.sha256(r.content).hexdigest()
        assert meta["size_bytes"] == len(r.content)

    def test_requires_client_key(self, client):
        publish(client, "1.0", b"x")
        assert client.get("/api/download/client-agent").status_code == 401


# ------------------------------------------------ дашборд: latest_client_version

class TestDashboardLatestClientVersion:
    def _event(self, version):
        return {
            "machine_name": "PC1",
            "process_name": "app.exe",
            "pid": 1,
            "start_time": "2026-09-17T09:00:00",
            "sample_time": "2026-09-17T09:05:00",
            "duration_seconds": 300,
            "client_version": version,
            "unique_key": f"PC1|1|{version}",
        }

    def test_no_release_means_not_outdated(self, client):
        insert_events([self._event("1.0")])
        item = client.get("/api/latest", headers=PUBLISH_HEADERS).json()["latest"][0]
        assert item["latest_client_version"] == ""
        assert item["client_outdated"] is False

    def test_outdated_flag_uses_table(self, client):
        insert_events([self._event("1.0")])
        publish(client, "1.1", b"x")
        item = client.get("/api/latest", headers=PUBLISH_HEADERS).json()["latest"][0]
        assert item["latest_client_version"] == "1.1"
        assert item["client_version"] == "1.0"
        assert item["client_outdated"] is True

    def test_same_version_not_outdated(self, client):
        insert_events([self._event("1.1")])
        publish(client, "1.1", b"x")
        item = client.get("/api/latest", headers=PUBLISH_HEADERS).json()["latest"][0]
        assert item["client_outdated"] is False
