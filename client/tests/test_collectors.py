"""
Платформенные коллекторы клиента: выбор модуля по os.name, Linux-фильтр
системных процессов (три правила по итогам прогона на ALT Workstation 11.1:
kernel thread по pid/ppid == 2, сервисная учётка по uid < UID_MIN, root-демон
по pid/ppid == 1 + системный каталог), Windows-фильтр без изменений, заглушки
WMI на Linux, os_info.
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

    @pytest.fixture(autouse=True)
    def _uid_min_1000(self, monkeypatch):
        monkeypatch.setattr(collectors_linux, "_uid_min_cache", 1000)

    # --- правило 1: kernel thread по pid/ppid == 2
    def test_kthreadd_itself_is_system(self):
        assert self.f("root", "", False, ppid=0, pid=2, uid=0) is True

    def test_kthreadd_child_is_system_regardless_of_exe_and_user(self):
        assert self.f("root", "", False, ppid=2, pid=1234, uid=0) is True
        assert self.f("alice", "/usr/bin/some-worker", False, ppid=2, pid=1234, uid=1000) is True

    def test_empty_exe_alone_is_not_system(self):
        """AccessDenied на exe (пустой путь) — отдельный случай «не смогли определить», не шум."""
        assert self.f("root", "", False, ppid=907, pid=2688, uid=0) is False       # sshd-сессия root под user
        assert self.f("root", None, False, ppid=1784, pid=2720, uid=0) is False
        assert self.f("alice", "   ", False, ppid=1, pid=3000, uid=1000) is False

    # --- правило 2: сервисная учётка по uid < UID_MIN
    @pytest.mark.parametrize("uid,user,exe", [
        (985, "nm-openconnect", "/usr/sbin/nm-openconnect-service"),
        (999, "_autoipd", "/usr/sbin/avahi-autoipd"),
        (1, "bin", "/usr/bin/x"),
        (997, "messagebus", "/usr/bin/dbus-daemon"),
    ])
    def test_service_account_is_system(self, uid, user, exe):
        assert self.f(user, exe, False, ppid=1, pid=700, uid=uid) is True
        assert self.f(user, exe, False, ppid=500, pid=700, uid=uid) is True   # независимо от ppid

    def test_root_uid_zero_is_not_service_account(self):
        assert self.f("root", "/usr/bin/bash5", False, ppid=2688, pid=2735, uid=0) is False

    def test_uid_min_from_login_defs_is_respected(self, monkeypatch):
        monkeypatch.setattr(collectors_linux, "_uid_min_cache", 500)
        assert self.f("svc", "/usr/bin/x", False, ppid=1, pid=700, uid=600) is False
        assert self.f("svc", "/usr/bin/x", False, ppid=1, pid=700, uid=499) is True

    def test_without_uid_service_rule_is_skipped(self):
        assert self.f("messagebus", "/usr/bin/dbus-daemon", False, ppid=1, pid=723) is False

    # --- правило 3: root-демон (pid == 1 или ppid == 1) из системного каталога
    def test_init_itself_is_system(self):
        assert self.f("root", "/usr/lib/systemd/systemd", False, ppid=0, pid=1, uid=0) is True

    @pytest.mark.parametrize("exe", [
        "/usr/sbin/sshd", "/usr/sbin/NetworkManager", "/usr/libexec/udisks2/udisksd",
        "/usr/lib/systemd/systemd-journald", "/usr/lib64/samba/sbin/smbd",
        "/lib/systemd/systemd-udevd", "/lib64/x", "/sbin/agetty",
    ])
    def test_root_daemon_started_by_init_is_system(self, exe):
        assert self.f("root", exe, False, ppid=1, pid=900, uid=0) is True

    def test_root_daemon_outside_system_dirs_is_not_system(self):
        assert self.f("root", "/usr/bin/guile22", False, ppid=1, pid=719, uid=0) is False     # alteratord на ALT
        assert self.f("root", "/usr/bin/udevadm", False, ppid=1, pid=608, uid=0) is False
        assert self.f("root", "/opt/app/bin/app", False, ppid=1, pid=5000, uid=0) is False

    def test_root_child_of_daemon_is_not_system(self):
        """sshd-сессия и смбд-воркеры: root, тот же бинарник, но ppid — не init."""
        assert self.f("root", "/usr/sbin/sshd", False, ppid=907, pid=2688, uid=0) is False
        assert self.f("root", "/usr/lib64/samba/sbin/smbd", False, ppid=1552, pid=1593, uid=0) is False
        assert self.f("root", "/usr/libexec/gdm-session-worker", False, ppid=902, pid=1771, uid=0) is False

    def test_non_root_from_system_dirs_started_by_init_is_not_system(self):
        assert self.f("alice", "/usr/lib/systemd/systemd", False, ppid=1, pid=1784, uid=1000) is False

    def test_root_daemon_rule_needs_pid_or_ppid(self):
        assert self.f("root", "/usr/sbin/sshd", False) is False

    # --- обычные процессы и had_access_error
    def test_root_shell_session_is_not_system(self):
        assert self.f("root", "/usr/bin/bash5", False, ppid=2688, pid=2735, uid=0) is False

    def test_regular_user_process_is_not_system(self):
        assert self.f("user", "/usr/bin/gnome-shell", False, ppid=1784, pid=1924, uid=1000) is False
        assert self.f("alice", "/usr/bin/python3", False) is False                  # без pid/ppid/uid

    def test_access_error_is_system(self):
        assert self.f("alice", "/usr/bin/python3", True, ppid=1234, pid=99, uid=1000) is True


class TestUidMin:
    def test_read_from_login_defs(self, tmp_path):
        f = tmp_path / "login.defs"
        f.write_text("# comment\nUID_MAX\t\t60000\n  UID_MIN   500  \nGID_MIN 1000\n", encoding="utf-8")
        assert collectors_linux.read_uid_min(str(f)) == 500

    def test_missing_or_unparseable_gives_default(self, tmp_path):
        assert collectors_linux.read_uid_min(str(tmp_path / "nope")) == 1000
        f = tmp_path / "login.defs"
        f.write_text("UID_MIN abc\nSYS_UID_MIN 201\n", encoding="utf-8")
        assert collectors_linux.read_uid_min(str(f)) == 1000

    def test_cached_once_per_run(self, monkeypatch, tmp_path):
        calls = []
        def fake_read(path=collectors_linux.LOGIN_DEFS_PATH):
            calls.append(path)
            return 777
        monkeypatch.setattr(collectors_linux, "_uid_min_cache", None)
        monkeypatch.setattr(collectors_linux, "read_uid_min", fake_read)
        assert collectors_linux.uid_min() == 777
        assert collectors_linux.uid_min() == 777
        assert len(calls) == 1


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

    def test_ppid_pid_uid_are_ignored_on_windows(self):
        assert self.f("alice", "C:\\Program Files\\x\\x.exe", False, ppid=2, pid=1, uid=5) is False

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
            pids = {int(r["pid"]) for r in rows}
            assert 2 not in pids                                        # kthreadd отфильтрован
            kernel = {p.pid for p in __import__("psutil").process_iter() if p.ppid() == 2}
            assert not (pids & kernel)                                  # и его потомки
