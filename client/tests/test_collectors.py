"""
Платформенные коллекторы клиента: выбор модуля по os.name, Linux-фильтр
системных процессов (kernel threads, потомки kthreadd, root из /usr/lib|/lib|/sbin),
Windows-фильтр без изменений, заглушки WMI на Linux, os_info.
Гоняется на любой ОС: модули импортируются напрямую, os.name мокается.
"""

import os
import platform
import sqlite3

import client_agent
import collectors_linux
import collectors_windows
import pytest


# ------------------------------------------------------------ выбор модуля

class TestSelectCollectors:
    def test_nt_gives_windows(self):
        assert client_agent.select_collectors("nt") is collectors_windows

    @pytest.mark.parametrize("os_name", ["posix", "java", "anything-else"])
    def test_non_nt_gives_linux(self, os_name):
        assert client_agent.select_collectors(os_name) is collectors_linux

    def test_default_follows_os_name(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        assert client_agent.select_collectors() is collectors_windows
        monkeypatch.setattr(os, "name", "posix")
        assert client_agent.select_collectors() is collectors_linux

    def test_module_level_choice_matches_current_os(self):
        expected = collectors_windows if os.name == "nt" else collectors_linux
        assert client_agent.collectors is expected

    @pytest.mark.parametrize("mod", [collectors_windows, collectors_linux])
    def test_contract(self, mod):
        for name in ("WMI_AVAILABLE", "_wmi_subscribe", "wmi_get_pid_times", "os_info", "is_system_process"):
            assert hasattr(mod, name), name
        assert isinstance(mod.WMI_AVAILABLE, bool)


# ------------------------------------------------------------ Linux-фильтр

class TestLinuxIsSystemProcess:
    f = staticmethod(collectors_linux.is_system_process)

    def test_kernel_thread_without_exe_is_system(self):
        assert self.f("root", "", False, ppid=2) is True
        assert self.f("root", None, False, ppid=1) is True
        assert self.f("alice", "   ", False) is True

    def test_kthreadd_child_is_system(self):
        assert self.f("root", "/usr/bin/some-worker", False, ppid=2) is True
        assert self.f("alice", "/usr/bin/some-worker", False, ppid=2) is True

    @pytest.mark.parametrize("path", ["/usr/lib/systemd/systemd-journald", "/lib/systemd/systemd-udevd", "/sbin/agetty"])
    def test_root_process_from_system_dirs_is_system(self, path):
        assert self.f("root", path, False, ppid=1) is True

    @pytest.mark.parametrize("path", ["/usr/lib/firefox/firefox", "/lib/foo", "/sbin/bar"])
    def test_non_root_from_system_dirs_is_not_system(self, path):
        assert self.f("alice", path, False, ppid=1) is False

    @pytest.mark.parametrize("path", ["/usr/bin/bash", "/usr/sbin/sshd", "/opt/app/bin/app", "/home/alice/tool"])
    def test_root_outside_system_dirs_is_not_system(self, path):
        assert self.f("root", path, False, ppid=1) is False

    def test_regular_user_process_is_not_system(self):
        assert self.f("alice", "/usr/bin/python3", False, ppid=1234) is False
        assert self.f("alice", "/usr/bin/python3", False) is False       # ppid не передан

    def test_access_error_is_system(self):
        assert self.f("alice", "/usr/bin/python3", True, ppid=1234) is True


# ------------------------------------------------------------ Windows-фильтр

class TestWindowsIsSystemProcess:
    f = staticmethod(collectors_windows.is_system_process)

    @pytest.mark.parametrize("user", ["SYSTEM", "NT AUTHORITY\\SYSTEM", "Local Service", "система",
                                      "NT AUTHORITY\\сетевая служба"])
    def test_system_users(self, user):
        assert self.f(user, "C:\\Program Files\\x\\x.exe", False) is True

    @pytest.mark.parametrize("path", ["C:\\Windows\\System32\\svchost.exe", "c:/windows/explorer.exe"])
    def test_windows_dir(self, path):
        assert self.f("alice", path, False) is True

    def test_regular_process(self):
        assert self.f("DESKTOP-1\\alice", "C:\\Program Files\\x\\x.exe", False) is False

    def test_ppid_is_ignored_on_windows(self):
        assert self.f("alice", "C:\\Program Files\\x\\x.exe", False, ppid=2) is False

    def test_access_error(self):
        assert self.f("alice", "C:\\Program Files\\x\\x.exe", True) is True


# ------------------------------------------------------------ WMI / os_info

class TestWmiStubs:
    def test_linux_has_no_wmi(self):
        assert collectors_linux.WMI_AVAILABLE is False

    def test_linux_wmi_get_pid_times_is_always_none(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        client_agent.ensure_schema(conn)
        conn.execute(
            "INSERT INTO wmi_pid_state(pid, start_time, end_time, process_name, ppid, updated_at) "
            "VALUES(1, '2026-09-18T10:00:00+00:00', NULL, 'x', 5, '2026-09-18T10:00:00+00:00')"
        )
        assert collectors_linux.wmi_get_pid_times(conn, 1) == (None, None, None, None)
        assert collectors_linux.wmi_get_pid_times(conn, 999) == (None, None, None, None)

    def test_linux_subscribe_is_noop(self):
        import threading
        conn = sqlite3.connect(":memory:")
        collectors_linux._wmi_subscribe(conn, threading.Event())   # возвращается сразу, ничего не пишет

    def test_windows_wmi_get_pid_times_reads_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        client_agent.ensure_schema(conn)
        conn.execute(
            "INSERT INTO wmi_pid_state(pid, start_time, end_time, process_name, ppid, updated_at) "
            "VALUES(1, '2026-09-18T10:00:00+00:00', NULL, 'x.exe', 5, '2026-09-18T10:00:00+00:00')"
        )
        assert collectors_windows.wmi_get_pid_times(conn, 1) == ("2026-09-18T10:00:00+00:00", None, 5, "x.exe")
        assert collectors_windows.wmi_get_pid_times(conn, 2) == (None, None, None, None)

    def test_windows_wmi_flag_matches_platform(self):
        if os.name != "nt":
            assert collectors_windows.WMI_AVAILABLE is False   # pythoncom не импортируется вне Windows


class TestOsInfo:
    def test_linux_os_info_is_platform_string(self):
        assert collectors_linux.os_info() == platform.platform()

    def test_windows_os_info_format(self, monkeypatch):
        monkeypatch.setattr(platform, "release", lambda: "10")
        monkeypatch.setattr(platform, "version", lambda: "10.0.19045")
        monkeypatch.setattr(platform, "win32_edition", lambda: "Professional", raising=False)
        assert collectors_windows.os_info() == "Windows 10 Professional (build 10.0.19045)"

    def test_windows_os_info_without_edition(self, monkeypatch):
        monkeypatch.setattr(platform, "release", lambda: "11")
        monkeypatch.setattr(platform, "version", lambda: "10.0.22631")
        monkeypatch.delattr(platform, "win32_edition", raising=False)
        assert collectors_windows.os_info() == "Windows 11 (build 10.0.22631)"


# ------------------------------------------------------------ collect_slice на текущей ОС

class TestCollectSliceSmoke:
    def test_collect_slice_writes_rows_via_selected_collector(self, tmp_path):
        """Срез реальных процессов текущей ОС: хотя бы собственный процесс pytest
        должен попасть в activity_log с os_info выбранного коллектора."""
        conn = client_agent.db_connect(str(tmp_path / "activity.db"))
        client_agent.ensure_schema(conn)
        client_agent.collect_slice(conn, "0.0")
        rows = conn.execute("SELECT pid, exe_path, os_info FROM activity_log").fetchall()
        assert rows
        assert any(int(r["pid"]) == os.getpid() for r in rows)
        assert all(r["os_info"] == client_agent.collectors.os_info() for r in rows)
        if os.name != "nt":
            assert all((r["exe_path"] or "").strip() for r in rows)   # kernel threads отфильтрованы
