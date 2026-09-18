"""
Платформенный коллектор для Windows.

Контракт (одинаков для collectors_windows и collectors_linux, client_agent.py
выбирает модуль по os.name):
  WMI_AVAILABLE                      — есть ли фоновый источник событий процессов
  _wmi_subscribe(conn, stop_evt)     — фоновый поток: заполняет таблицу wmi_pid_state
  wmi_get_pid_times(conn, pid)       — (start_time, end_time, ppid, process_name) или (None,)*4
  os_info()                          — строка ОС для события
  is_system_process(user_name, exe_path, had_access_error, ppid=None, pid=None, uid=None)
                                     — True для процессов, которые не мониторим

WMI даёт точные start_time/ppid/process_name для короткоживущих процессов,
которых psutil.create_time() может не застать. Всё это внутреннее: сервер и
формат событий про WMI не знают.
"""

import os
import platform
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

WMI_AVAILABLE = False
try:
    if os.name == "nt":
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
        WMI_AVAILABLE = True
except Exception:
    WMI_AVAILABLE = False


def _wmi_subscribe(conn: sqlite3.Connection, stop_evt: threading.Event) -> None:
    """
    Subscribes to Win32_ProcessStartTrace/StopTrace.
    IMPORTANT: WMI is internal. We only update wmi_pid_state table.
    """
    if not WMI_AVAILABLE:
        return
    try:
        pythoncom.CoInitialize()
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc = locator.ConnectServer(".", "root\\cimv2")

        q_start = "SELECT * FROM Win32_ProcessStartTrace"
        q_stop = "SELECT * FROM Win32_ProcessStopTrace"

        start_w = svc.ExecNotificationQuery(q_start)
        stop_w = svc.ExecNotificationQuery(q_stop)

        cur = conn.cursor()

        def iso_now() -> str:
            return datetime.now(timezone.utc).isoformat(timespec="seconds")

        while not stop_evt.is_set():
            # Use short timeout-style polling
            # SWbemEventSource doesn't have native timeout; emulate by alternating try/except
            try:
                ev = start_w.NextEvent(1000)  # ms
                pid = int(getattr(ev, "ProcessID", 0) or 0)
                ppid = int(getattr(ev, "ParentProcessID", 0) or 0)
                pname = str(getattr(ev, "ProcessName", "") or "")
                ts = iso_now()
                if pid:
                    cur.execute(
                        """
                        INSERT INTO wmi_pid_state(pid, start_time, end_time, process_name, ppid, updated_at)
                        VALUES(?, ?, NULL, ?, ?, ?)
                        ON CONFLICT(pid) DO UPDATE SET
                            start_time=excluded.start_time,
                            end_time=NULL,
                            process_name=excluded.process_name,
                            ppid=excluded.ppid,
                            updated_at=excluded.updated_at
                        """,
                        (pid, ts, pname, ppid if ppid else None, ts),
                    )
                    conn.commit()
            except Exception:
                pass

            try:
                ev2 = stop_w.NextEvent(200)  # ms
                pid2 = int(getattr(ev2, "ProcessID", 0) or 0)
                pname2 = str(getattr(ev2, "ProcessName", "") or "")
                ts2 = iso_now()
                if pid2:
                    cur.execute(
                        """
                        UPDATE wmi_pid_state
                        SET end_time=?, process_name=COALESCE(?, process_name), updated_at=?
                        WHERE pid=?
                        """,
                        (ts2, pname2 if pname2 else None, ts2, pid2),
                    )
                    conn.commit()
            except Exception:
                pass

    except Exception:
        # WMI failure should never stop the agent
        return
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def wmi_get_pid_times(conn: sqlite3.Connection, pid: int) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """
    Returns (start_time, end_time, ppid, process_name) from wmi_pid_state for pid.
    """
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time, ppid, process_name FROM wmi_pid_state WHERE pid=?;", (int(pid),))
    r = cur.fetchone()
    if not r:
        return None, None, None, None
    return r["start_time"], r["end_time"], r["ppid"], r["process_name"]


def os_info() -> str:
    try:
        edition = ""
        try:
            edition = platform.win32_edition()  # type: ignore[attr-defined]
        except Exception:
            edition = ""
        rel = platform.release()
        ver = platform.version()
        base = f"Windows {rel}"
        if edition:
            base += f" {edition}"
        if ver:
            base += f" (build {ver})"
        return base
    except Exception:
        return "unknown"


# System process filtering
SYSTEM_USERS = {
    "system", "local service", "network service",
    "система", "локальная служба", "сетевая служба",
    "локальная_служба", "сетевая_служба",
}


def _norm_user(u: Optional[str]) -> str:
    u2 = (u or "").strip().lower()
    if "\\" in u2:
        u2 = u2.split("\\", 1)[1]
    return u2


def is_system_process(user_name: Optional[str], exe_path: Optional[str], had_access_error: bool,
                      ppid: Optional[int] = None, pid: Optional[int] = None,
                      uid: Optional[int] = None) -> bool:
    """Return True for processes that should be excluded from monitoring
    (ppid/pid/uid — часть общего контракта с collectors_linux, на Windows не используются)."""
    if had_access_error:
        return True
    if _norm_user(user_name) in SYSTEM_USERS:
        return True
    p = (exe_path or "").strip().lower().replace("/", "\\")
    if p.startswith("c:\\windows\\"):
        return True
    return False
