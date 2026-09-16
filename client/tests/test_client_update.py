"""
Самообновление агента: то, что проверяется без Windows и без exe.

HTTP не мокается: в тестах поднимается настоящий локальный http.server с теми
же двумя эндпоинтами, что у сервера мониторинга. Файловая логика замены
проверяется на временном файле (exe_path передаётся явно, перезапуск отключён).
"""

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import client_agent
import pytest
from client_agent import (
    CLIENT_VERSION,
    apply_downloaded_release,
    check_and_apply_update,
    cleanup_old_executable,
    is_newer_version,
    load_config,
    parse_version,
    server_base_url,
)

CLIENT_KEY = "TEST_CLIENT_KEY"


# ------------------------------------------------------------ локальный сервер

class FakeReleaseServer:
    """Минимальный сервер релизов: /api/client-release и /api/download/client-agent."""

    def __init__(self):
        self.release = None          # dict(version, sha256, size_bytes) или None
        self.data = b""              # байты, которые отдаёт download
        self.requests = []           # (path, client_key)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("X-Client-Key")))
                if self.headers.get("X-Client-Key") != CLIENT_KEY:
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path == "/api/client-release":
                    body = json.dumps({"client_release": outer.release}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/download/client-agent":
                    if outer.release is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(outer.data)
                else:
                    self.send_response(404)
                    self.end_headers()

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def set_release(self, version, data: bytes, sha256=None):
        self.data = data
        self.release = {
            "version": version,
            "sha256": sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }


@pytest.fixture
def srv():
    s = FakeReleaseServer().start()
    yield s
    s.stop()


@pytest.fixture
def cfg(srv):
    return {"server_ingest_url": f"{srv.base_url}/api/ingest", "client_update_key": CLIENT_KEY}


@pytest.fixture
def fake_exe(tmp_path):
    p = tmp_path / "client_agent.exe"
    p.write_bytes(b"OLD-BINARY")
    return str(p)


# ------------------------------------------------------------- версии

class TestParseVersion:
    @pytest.mark.parametrize("raw,expected", [
        ("1.2", (1, 2)), ("1.10", (1, 10)), ("1.4.2", (1, 4, 2)), (" 2.0 ", (2, 0)), ("3", (3,)),
    ])
    def test_valid(self, raw, expected):
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "v1.2", "1.2-beta", "1..2", "abc", "1.", "1.2 3"])
    def test_invalid(self, raw):
        assert parse_version(raw) is None


class TestIsNewer:
    def test_numeric_not_string_comparison(self):
        assert is_newer_version("1.10", "1.9") is True   # строкой "1.10" < "1.9"
        assert is_newer_version("1.9", "1.10") is False

    def test_equal_and_older(self):
        assert is_newer_version("1.2", "1.2") is False
        assert is_newer_version("1.1", "1.2") is False

    def test_more_components(self):
        assert is_newer_version("1.2.1", "1.2") is True
        assert is_newer_version("1.2", "1.2.0") is False

    def test_unparseable_gives_none(self):
        assert is_newer_version("beta", "1.2") is None
        assert is_newer_version("1.3", "dev") is None

    def test_client_version_constant_is_parseable(self):
        assert parse_version(CLIENT_VERSION) is not None


# ------------------------------------------------------------- базовый URL

class TestServerBaseUrl:
    def test_derived_from_ingest_url(self):
        assert server_base_url({"server_ingest_url": "http://10.0.0.5:8000/api/ingest"}) == "http://10.0.0.5:8000"
        assert server_base_url({"server_ingest_url": "https://mon.example.org/api/ingest/"}) == "https://mon.example.org"

    @pytest.mark.parametrize("url", ["", "http://10.0.0.5:8000", "http://10.0.0.5:8000/ingest", "/api/ingest"])
    def test_wrong_suffix_disables_updates(self, url):
        assert server_base_url({"server_ingest_url": url}) is None


# ------------------------------------------------------------- конфиг

class TestLoadConfig:
    def test_client_update_key_default_and_env_override(self, monkeypatch):
        monkeypatch.delenv("MONITORING_CLIENT_UPDATE_KEY", raising=False)
        assert load_config()["client_update_key"] == "CHANGE_ME_CLIENT_KEY"
        monkeypatch.setenv("MONITORING_CLIENT_UPDATE_KEY", "FROM_ENV")
        assert load_config()["client_update_key"] == "FROM_ENV"
        monkeypatch.setenv("MONITORING_CLIENT_UPDATE_KEY", "  ")
        assert load_config()["client_update_key"] == "CHANGE_ME_CLIENT_KEY"


# ------------------------------------------------------------- check_and_apply_update

class TestCheckAndApplyUpdate:
    def test_server_unreachable_is_quiet(self, fake_exe):
        cfg = {"server_ingest_url": "http://127.0.0.1:9/api/ingest", "client_update_key": CLIENT_KEY}
        # короткий timeout: в WSL2 соединение на закрытый порт localhost висит до таймаута
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False, timeout=2) == "up_to_date"
        assert Path(fake_exe).read_bytes() == b"OLD-BINARY"

    def test_no_releases(self, srv, cfg, fake_exe):
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False) == "up_to_date"
        assert srv.requests == [("/api/client-release", CLIENT_KEY)]  # download не запрашивался

    def test_same_or_older_version_no_download(self, srv, cfg, fake_exe):
        srv.set_release("1.2", b"NEW")
        assert check_and_apply_update(cfg, "1.2", exe_path=fake_exe, do_relaunch=False) == "up_to_date"
        assert check_and_apply_update(cfg, "1.3", exe_path=fake_exe, do_relaunch=False) == "up_to_date"
        assert all(path == "/api/client-release" for path, _ in srv.requests)
        assert Path(fake_exe).read_bytes() == b"OLD-BINARY"

    def test_unparseable_server_version_is_skipped(self, srv, cfg, fake_exe):
        srv.set_release("1.3-rc1", b"NEW")
        assert check_and_apply_update(cfg, "1.2", exe_path=fake_exe, do_relaunch=False) == "up_to_date"
        assert Path(fake_exe).read_bytes() == b"OLD-BINARY"

    def test_wrong_client_key_is_quiet(self, srv, fake_exe):
        srv.set_release("9.9", b"NEW")
        cfg = {"server_ingest_url": f"{srv.base_url}/api/ingest", "client_update_key": "WRONG"}
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False) == "up_to_date"

    def test_not_frozen_refuses_to_replace(self, srv, cfg, tmp_path, monkeypatch):
        srv.set_release("9.9", b"NEW")
        monkeypatch.setattr(client_agent.sys, "frozen", False, raising=False)
        # exe_path не задан и процесс не упакован -> отказ без скачивания
        assert check_and_apply_update(cfg, "1.0", do_relaunch=False) == "error"
        assert all(path == "/api/client-release" for path, _ in srv.requests)

    def test_sha_mismatch_rejects_and_cleans_temp(self, srv, cfg, fake_exe):
        srv.set_release("2.0", b"NEW-BINARY", sha256="00" * 32)
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False) == "error"
        assert Path(fake_exe).read_bytes() == b"OLD-BINARY"
        assert not os.path.exists(fake_exe + ".download")
        assert not os.path.exists(fake_exe + ".old")

    def test_newer_release_is_applied(self, srv, cfg, fake_exe):
        srv.set_release("2.0", b"NEW-BINARY")
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False) == "updated"
        assert Path(fake_exe).read_bytes() == b"NEW-BINARY"
        assert Path(fake_exe + ".old").read_bytes() == b"OLD-BINARY"
        assert not os.path.exists(fake_exe + ".download")
        assert [p for p, _ in srv.requests] == ["/api/client-release", "/api/download/client-agent"]

    def test_download_404_is_error_without_touching_exe(self, srv, cfg, fake_exe):
        # метаданные есть, но download отдаёт 404 (release=None только для download)
        srv.set_release("2.0", b"NEW")
        real = srv.release
        original_do_get = srv.httpd.RequestHandlerClass.do_GET

        def flaky(self):
            if self.path.startswith("/api/download"):
                srv.release = None
            original_do_get(self)
            srv.release = real
        srv.httpd.RequestHandlerClass.do_GET = flaky
        assert check_and_apply_update(cfg, "1.0", exe_path=fake_exe, do_relaunch=False) == "error"
        assert Path(fake_exe).read_bytes() == b"OLD-BINARY"
        assert not os.path.exists(fake_exe + ".download")


# ------------------------------------------------------------- файловые операции

class TestFileOps:
    def test_apply_replaces_and_keeps_old(self, tmp_path):
        exe = tmp_path / "a.exe"; exe.write_bytes(b"old")
        new = tmp_path / "a.exe.download"; new.write_bytes(b"new")
        apply_downloaded_release(str(exe), str(new))
        assert exe.read_bytes() == b"new"
        assert (tmp_path / "a.exe.old").read_bytes() == b"old"
        assert not new.exists()

    def test_apply_overwrites_stale_old(self, tmp_path):
        exe = tmp_path / "a.exe"; exe.write_bytes(b"old")
        (tmp_path / "a.exe.old").write_bytes(b"stale")
        new = tmp_path / "a.exe.download"; new.write_bytes(b"new")
        apply_downloaded_release(str(exe), str(new))
        assert (tmp_path / "a.exe.old").read_bytes() == b"old"

    def test_cleanup_old_executable(self, tmp_path):
        exe = tmp_path / "a.exe"; exe.write_bytes(b"x")
        (tmp_path / "a.exe.old").write_bytes(b"stale")
        cleanup_old_executable(str(exe))
        assert not (tmp_path / "a.exe.old").exists()
        cleanup_old_executable(str(exe))   # повторно — без ошибок
        cleanup_old_executable(None)
