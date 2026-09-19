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


def publish(client, version, data: bytes, filename="client_agent.exe", headers=PUBLISH_HEADERS, platform=None):
    form = {"version": version}
    if platform is not None:
        form["platform"] = platform
    return client.post(
        "/api/client-release",
        data=form,
        files={"file": (filename, data, "application/octet-stream")},
        headers=headers,
    )


def agent_headers(platform=None):
    h = dict(AGENT_HEADERS)
    if platform is not None:
        h["X-Client-Platform"] = platform
    return h


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

    def test_platform_defaults_to_windows(self, client, db):
        # старые публикаторы (CI до этой задачи) поле platform не шлют
        assert publish(client, "1.0", b"x").status_code == 200
        assert db.execute("SELECT platform FROM client_releases").fetchone()["platform"] == "windows"

    @pytest.mark.parametrize("plat", ["windows", "linux"])
    def test_platform_is_stored(self, client, db, plat):
        r = publish(client, "1.0", b"x", platform=plat)
        assert r.status_code == 200, r.text
        assert r.json()["platform"] == plat
        assert db.execute("SELECT platform FROM client_releases").fetchone()["platform"] == plat

    def test_platform_is_stripped_and_lowercased(self, client, db):
        assert publish(client, "1.0", b"x", platform=" Linux ").status_code == 200
        assert db.execute("SELECT platform FROM client_releases").fetchone()["platform"] == "linux"

    def test_empty_platform_means_default(self, client, db):
        # FastAPI подставляет default вместо пустого значения Form-поля,
        # поэтому platform="" неотличим от отсутствующего поля
        assert publish(client, "1.0", b"x", platform="").status_code == 200
        assert db.execute("SELECT platform FROM client_releases").fetchone()["platform"] == "windows"

    @pytest.mark.parametrize("bad", ["macos", "win", "linux2", "windows linux"])
    def test_invalid_platform_gives_400(self, client, db, bad):
        r = publish(client, "1.0", b"x", platform=bad)
        assert r.status_code == 400, r.text
        assert db.execute("SELECT COUNT(*) FROM client_releases").fetchone()["count"] == 0


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

    def test_platforms_do_not_mix(self, client):
        publish(client, "1.0", b"win-bin", platform="windows")
        publish(client, "2.0", b"linux-bin", platform="linux")
        win = client.get("/api/client-release", headers=agent_headers("windows")).json()["client_release"]
        lin = client.get("/api/client-release", headers=agent_headers("linux")).json()["client_release"]
        assert win["version"] == "1.0" and win["sha256"] == hashlib.sha256(b"win-bin").hexdigest()
        assert lin["version"] == "2.0" and lin["sha256"] == hashlib.sha256(b"linux-bin").hexdigest()

    def test_agent_without_header_gets_windows(self, client):
        publish(client, "1.0", b"win-bin", platform="windows")
        publish(client, "2.0", b"linux-bin", platform="linux")
        rel = client.get("/api/client-release", headers=AGENT_HEADERS).json()["client_release"]
        assert rel["version"] == "1.0"

    @pytest.mark.parametrize("bad", ["", "macos", "LiNuX2"])
    def test_unknown_platform_header_falls_back_to_windows(self, client, bad):
        publish(client, "1.0", b"win-bin", platform="windows")
        publish(client, "2.0", b"linux-bin", platform="linux")
        rel = client.get("/api/client-release", headers=agent_headers(bad)).json()["client_release"]
        assert rel["version"] == "1.0"

    def test_platform_header_is_case_insensitive(self, client):
        publish(client, "2.0", b"linux-bin", platform="linux")
        rel = client.get("/api/client-release", headers=agent_headers("Linux")).json()["client_release"]
        assert rel["version"] == "2.0"

    def test_no_release_for_platform_gives_null(self, client):
        publish(client, "1.0", b"win-bin", platform="windows")
        r = client.get("/api/client-release", headers=agent_headers("linux"))
        assert r.status_code == 200
        assert r.json() == {"client_release": None}


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

    def test_platforms_do_not_mix(self, client):
        publish(client, "1.0", b"win-bin", filename="client_agent.exe", platform="windows")
        publish(client, "2.0", b"linux-bin", filename="client_agent", platform="linux")

        win = client.get("/api/download/client-agent", headers=agent_headers("windows"))
        assert win.content == b"win-bin"
        assert 'filename="client_agent.exe"' in win.headers["content-disposition"]

        lin = client.get("/api/download/client-agent", headers=agent_headers("linux"))
        assert lin.content == b"linux-bin"
        assert 'filename="client_agent"' in lin.headers["content-disposition"]

    def test_agent_without_header_gets_windows(self, client):
        publish(client, "1.0", b"win-bin", platform="windows")
        publish(client, "2.0", b"linux-bin", platform="linux")
        assert client.get("/api/download/client-agent", headers=AGENT_HEADERS).content == b"win-bin"

    def test_404_when_platform_has_no_release(self, client):
        publish(client, "1.0", b"win-bin", platform="windows")
        r = client.get("/api/download/client-agent", headers=agent_headers("linux"))
        assert r.status_code == 404


# ------------------------------------------------ дашборд: latest_client_version

class TestDashboardLatestClientVersion:
    def _event(self, version, machine="PC1", os_info=None):
        e = {
            "machine_name": machine,
            "process_name": "app.exe",
            "pid": 1,
            "start_time": "2026-09-17T09:00:00",
            "sample_time": "2026-09-17T09:05:00",
            "duration_seconds": 300,
            "client_version": version,
            "unique_key": f"{machine}|1|{version}",
        }
        if os_info is not None:
            e["os_info"] = os_info
        return e

    def _by_machine(self, client):
        return {m["machine_name"]: m for m in client.get("/api/latest", headers=PUBLISH_HEADERS).json()["latest"]}

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

    def test_each_machine_compares_with_its_platform_release(self, client):
        # версии по ОС разъезжаются: windows 1.1, linux 2.0
        insert_events([
            self._event("1.1", machine="PC1", os_info="Windows 10 Pro"),
            self._event("2.0", machine="ALT1", os_info="ALT Workstation 11.1 (Prometheus)"),
        ])
        publish(client, "1.1", b"win-bin", platform="windows")
        publish(client, "2.0", b"linux-bin", platform="linux")

        items = self._by_machine(client)
        assert items["PC1"]["latest_client_version"] == "1.1"
        assert items["PC1"]["client_outdated"] is False
        assert items["ALT1"]["latest_client_version"] == "2.0"
        assert items["ALT1"]["client_outdated"] is False

    def test_linux_machine_is_outdated_against_linux_release_only(self, client):
        insert_events([self._event("1.0", machine="ALT1", os_info="Astra Linux 1.7_x86-64")])
        publish(client, "9.9", b"win-bin", platform="windows")
        item = self._by_machine(client)["ALT1"]
        # под linux релизов нет — сравнивать не с чем, windows-версия не подставляется
        assert item["latest_client_version"] == ""
        assert item["client_outdated"] is False

        publish(client, "2.0", b"linux-bin", platform="linux")
        item = self._by_machine(client)["ALT1"]
        assert item["latest_client_version"] == "2.0"
        assert item["client_outdated"] is True

    def test_machine_without_os_info_falls_back_to_windows(self, client):
        insert_events([self._event("1.0", machine="OLD1")])
        publish(client, "1.1", b"win-bin", platform="windows")
        item = self._by_machine(client)["OLD1"]
        assert item["latest_client_version"] == "1.1"
        assert item["client_outdated"] is True
