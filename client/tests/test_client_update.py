"""
Самообновление агента: то, что проверяется без Windows и без exe.

HTTP не мокается: в тестах поднимается настоящий локальный http.server с теми
же двумя эндпоинтами, что у сервера мониторинга (release_stub.FakeReleaseServer).
Файловая логика замены проверяется на временном файле (exe_path передаётся явно, перезапуск отключён).
"""

import os
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
    relaunch,
    server_base_url,
)
from release_stub import CLIENT_KEY, FakeReleaseServer

# ------------------------------------------------------------ локальный сервер

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

    @pytest.mark.skipif(os.name == "nt", reason="бит исполнения есть только на POSIX")
    def test_apply_preserves_executable_bit(self, tmp_path):
        exe = tmp_path / "a.exe"; exe.write_bytes(b"old"); exe.chmod(0o755)
        new = tmp_path / "a.exe.download"; new.write_bytes(b"new"); new.chmod(0o644)
        apply_downloaded_release(str(exe), str(new))
        assert os.access(str(exe), os.X_OK)
        assert oct(exe.stat().st_mode & 0o777) == "0o755"

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


# ------------------------------------------------------------- client/VERSION

class TestVersionFile:
    def test_version_file_is_the_source_when_running_from_source(self):
        version_file = os.path.join(os.path.dirname(client_agent.__file__), "VERSION")
        with open(version_file, encoding="utf-8") as f:
            assert CLIENT_VERSION == f.read().strip()

    def test_version_file_format(self):
        assert parse_version(CLIENT_VERSION) is not None
        assert len(parse_version(CLIENT_VERSION)) >= 2


# ------------------------------------------------------------- relaunch

class TestRelaunch:
    @pytest.mark.skipif(os.name == "nt", reason="shell-скрипт вместо exe — только POSIX")
    def test_child_gets_args_and_no_pyinstaller_env(self, tmp_path, monkeypatch):
        import time
        out = tmp_path / "child.txt"
        script = tmp_path / "fake_agent.sh"
        script.write_text(f'#!/bin/sh\necho "ARGS:$*" > "{out}"\nenv >> "{out}"\n')
        script.chmod(0o755)
        # так выглядит окружение внутри onefile-exe PyInstaller
        monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEIfake")
        monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/tmp/fake")
        monkeypatch.setenv("_MEIPASS2", "/tmp/_MEIfake")
        monkeypatch.setenv("MONITORING_CLIENT_UPDATE_KEY", "KEEP_ME")

        relaunch(str(script), ["--apply-update-now"])
        for _ in range(50):
            if out.exists() and "MONITORING_CLIENT_UPDATE_KEY" in out.read_text():
                break
            time.sleep(0.1)
        text = out.read_text()
        assert text.startswith("ARGS:--apply-update-now")
        assert "MONITORING_CLIENT_UPDATE_KEY=KEEP_ME" in text
        assert "_PYI_" not in text
        assert "_MEIPASS2" not in text
