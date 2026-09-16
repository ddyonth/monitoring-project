import os
import sys
import json
import time
import sqlite3
import socket
import getpass
import argparse
import hashlib
import platform
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psutil
import requests
# Config

DEFAULT_CONFIG: Dict[str, Any] = {
    "server_ingest_url": "http://127.0.0.1:8000/api/ingest",
    "api_key": "CHANGE_ME_LOCAL_KEY",
    "db_path": "activity.db",
    "interval_minutes": 10,
    "batch_size": 300,
}

CONFIG_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
]

def load_config() -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    for path in CONFIG_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                cfg.update(disk)
                break
        except Exception:
            pass
    # Secret from environment takes priority over config.json
    env_api_key = os.environ.get("MONITORING_API_KEY")
    if env_api_key is not None and env_api_key.strip():
        cfg["api_key"] = env_api_key
    return cfg



# DB

def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_name TEXT NOT NULL,
            user_name TEXT,
            process_name TEXT NOT NULL,
            pid INTEGER,
            ppid INTEGER,
            exe_path TEXT,
            sha256 TEXT,

            start_time TEXT NOT NULL,
            sample_time TEXT NOT NULL,
            end_time TEXT,
            duration_seconds INTEGER NOT NULL,

            boot_time TEXT,
            os_info TEXT,
            current_user TEXT,
            client_version TEXT,

            cpu_user_time_s REAL,
            cpu_system_time_s REAL,
            rss_bytes INTEGER,
            io_read_bytes INTEGER,
            io_write_bytes INTEGER,
            io_read_count INTEGER,
            io_write_count INTEGER,
            net_active INTEGER,
            net_conn_count INTEGER,

            unique_key TEXT NOT NULL UNIQUE,
            sent INTEGER DEFAULT 0
        );
        """
    )

    # cache: exe_path -> sha256 (+mtime)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS binary_hash_cache (
            exe_path TEXT PRIMARY KEY,
            sha256 TEXT,
            file_mtime REAL
        );
        """
    )

    # internal WMI lifecycle cache (NOT sent to server)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wmi_pid_state (
            pid INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT,
            process_name TEXT,
            ppid INTEGER,
            updated_at TEXT
        );
        """
    )

    conn.commit()


# SHA-256 cache

def _file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def get_cached_sha256(conn: sqlite3.Connection, exe_path: Optional[str]) -> Optional[str]:
    if not exe_path:
        return None
    exe_path = str(exe_path)
    try:
        st = os.stat(exe_path)
        mtime = float(st.st_mtime)
    except Exception:
        mtime = None

    cur = conn.cursor()
    cur.execute("SELECT sha256, file_mtime FROM binary_hash_cache WHERE exe_path=?;", (exe_path,))
    row = cur.fetchone()
    if row and row["sha256"]:
        cached_mtime = row["file_mtime"]
        if mtime is None or cached_mtime is None or float(cached_mtime) == float(mtime):
            return str(row["sha256"])

    if mtime is None:
        # cannot read stat -> don't compute
        cur.execute(
            "INSERT OR REPLACE INTO binary_hash_cache(exe_path, sha256, file_mtime) VALUES(?, NULL, NULL);",
            (exe_path,),
        )
        conn.commit()
        return None

    sha = _file_sha256(exe_path)
    cur.execute(
        "INSERT OR REPLACE INTO binary_hash_cache(exe_path, sha256, file_mtime) VALUES(?, ?, ?);",
        (exe_path, sha, mtime),
    )
    conn.commit()
    return sha


# WMI (Windows): internal time source

_WMI_AVAILABLE = False
try:
    if os.name == "nt":
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
        _WMI_AVAILABLE = True
except Exception:
    _WMI_AVAILABLE = False


def _wmi_subscribe(conn: sqlite3.Connection, stop_evt: threading.Event) -> None:
    """
    Subscribes to Win32_ProcessStartTrace/StopTrace.
    IMPORTANT: WMI is internal. We only update wmi_pid_state table.
    """
    if not _WMI_AVAILABLE:
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


# Collection

def get_machine_name() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def os_info() -> str:
    try:
        if os.name == "nt":
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
        return platform.platform()
    except Exception:
        return "unknown"


def iso_from_ts(ts: float) -> str:
    # Always store UTC timestamps (timezone-aware) to keep duration math consistent
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# System process filtering
SYSTEM_USERS = {
    "system", "local service", "network service",
    "система", "локальная служба", "сетевая служба",
    "локальная_служба", "сетевая_служба",
}

def normalize_username_display(u: Optional[str]) -> str:
    """Normalize username for storage/display: strip host/domain prefix like DESKTOP-XXX\\user."""
    s = (u or "").strip()
    if "\\" in s:
        s = s.split("\\", 1)[1]
    return s


def _norm_user(u: Optional[str]) -> str:
    u2 = (u or "").strip().lower()
    if "\\" in u2:
        u2 = u2.split("\\", 1)[1]
    return u2

def is_system_process(user_name: Optional[str], exe_path: Optional[str], had_access_error: bool) -> bool:
    """Return True for processes that should be excluded from monitoring."""
    if had_access_error:
        return True
    if _norm_user(user_name) in SYSTEM_USERS:
        return True
    p = (exe_path or "").strip().lower().replace("/", "\\")
    if p.startswith("c:\\windows\\"):
        return True
    return False


def collect_slice(conn: sqlite3.Connection, client_version: str) -> None:
    machine = get_machine_name()
    user = getpass.getuser()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # best-effort boot time
    boot = None
    try:
        boot = iso_from_ts(psutil.boot_time())
    except Exception:
        boot = None

    for p in psutil.process_iter(attrs=["pid", "ppid", "name", "exe", "username", "create_time"]):
        try:
            had_err = False
            try:
                info = p.info
                pid = int(info.get("pid") or 0)
                if pid <= 0:
                    continue
                ppid = info.get("ppid")
                pname = str(info.get("name") or "unknown")
                exe = info.get("exe") or ""
                uname_raw = info.get("username")  # может быть None/"" если владелец недоступен
                uname = normalize_username_display(uname_raw or "")
                if not uname_raw:
                    had_err = True  # считаем как "не смогли получить владельца" → фильтр системных сработает

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                had_err = True
                continue

            if not pname:
                continue
            if is_system_process(uname, exe, had_err):
                continue

            # base start_time from psutil
            st = None
            ct = info.get("create_time")
            if isinstance(ct, (int, float)):
                st = iso_from_ts(float(ct))
            else:
                st = now

            # override with WMI start_time if present
            wmi_st, wmi_end, wmi_ppid, wmi_pname = wmi_get_pid_times(conn, pid)
            if wmi_st:
                st = wmi_st
            if wmi_ppid:
                ppid = wmi_ppid
            if wmi_pname:
                pname = wmi_pname

            # duration for running sample = now - start_time
            st_dt = _safe_parse_iso(st)
            if st_dt:
                dur = int((datetime.now(timezone.utc) - st_dt).total_seconds())
                if dur < 0:
                    dur = 0
            else:
                dur = 0

            sha = get_cached_sha256(conn, exe)

            # metrics (best-effort)
            cpu_user = cpu_sys = None
            rss = None
            io_rb = io_wb = io_rc = io_wc = None
            net_active = None
            net_count = None
            try:
                t = p.cpu_times()
                cpu_user = float(getattr(t, "user", 0.0) or 0.0)
                cpu_sys = float(getattr(t, "system", 0.0) or 0.0)
            except Exception:
                pass
            try:
                rss = int(p.memory_info().rss)
            except Exception:
                pass
            try:
                io = p.io_counters()
                io_rb = int(getattr(io, "read_bytes", 0) or 0)
                io_wb = int(getattr(io, "write_bytes", 0) or 0)
                io_rc = int(getattr(io, "read_count", 0) or 0)
                io_wc = int(getattr(io, "write_count", 0) or 0)
            except Exception:
                pass

            # net activity is best-effort (not exact per-process without ETW). Keep previous semantics: if process has any conns.
            try:
                conns = p.net_connections(kind="inet")
                net_count = len(conns)
                net_active = 1 if net_count > 0 else 0

            except Exception:
                net_count = None
                net_active = None

            # unique per sample row to keep append-only: (machine,pid,start_time,sample_time)
            unique_key = f"{machine}|{pid}|{st}|{now}"

            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO activity_log(
                    machine_name, user_name, process_name, pid, ppid, exe_path, sha256,
                    start_time, sample_time, end_time, duration_seconds,
                    boot_time, os_info, current_user, client_version,
                    cpu_user_time_s, cpu_system_time_s, rss_bytes,
                    io_read_bytes, io_write_bytes, io_read_count, io_write_count,
                    net_active, net_conn_count,
                    unique_key, sent
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    machine,
                    uname,
                    pname,
                    pid,
                    ppid,
                    exe,
                    sha,
                    st,
                    now,
                    dur,
                    boot,
                    os_info(),
                    user,
                    client_version,
                    cpu_user,
                    cpu_sys,
                    rss,
                    io_rb,
                    io_wb,
                    io_rc,
                    io_wc,
                    net_active,
                    net_count,
                    unique_key,
                ),
            )
            conn.commit()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    # after slice, materialize finished processes if we have WMI end_time
    materialize_finished(conn, now, client_version, boot, user)

def _safe_parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def materialize_finished(conn: sqlite3.Connection, sample_now: str, client_version: str, boot_time: Optional[str], current_user: str) -> None:
    """
    If WMI has end_time for pid, create a FINAL process record (append-only) with:
      sample_time=end_time, end_time=end_time, duration_seconds=final.
    This keeps WMI transparent for server/UI, and preserves append-only architecture.
    """
    cur = conn.cursor()
    cur.execute("SELECT pid, start_time, end_time, process_name, ppid FROM wmi_pid_state WHERE end_time IS NOT NULL;")
    rows = cur.fetchall()
    if not rows:
        return

    machine = get_machine_name()
    os_s = os_info()

    for r in rows:
        pid = int(r["pid"])
        st = r["start_time"]
        et = r["end_time"]
        pname = r["process_name"] or "unknown"
        ppid = r["ppid"]

        if not st or not et:
            continue

        st_dt = _safe_parse_iso(st)
        et_dt = _safe_parse_iso(et)
        if not (st_dt and et_dt):
            continue
        dur = int((et_dt - st_dt).total_seconds())
        if dur < 0:
            dur = 0

        # If we already created a final record for this session, skip
        final_key = f"{machine}|{pid}|{st}|{et}|FINAL"
        cur.execute("SELECT 1 FROM activity_log WHERE unique_key=? LIMIT 1;", (final_key,))
        if cur.fetchone():
            # cleanup pid_state row so it won't repeat
            cur.execute("DELETE FROM wmi_pid_state WHERE pid=?;", (pid,))
            conn.commit()
            continue

        # Try to reuse last known attributes from activity_log for this pid/start_time
        cur.execute(
            """
            SELECT user_name, exe_path, sha256
            FROM activity_log
            WHERE machine_name=? AND pid=? AND start_time=?
            ORDER BY sample_time DESC
            LIMIT 1
            """,
            (machine, pid, st),
        )
        last = cur.fetchone()
        user_name = normalize_username_display(last["user_name"]) if (last and last["user_name"]) else None
        exe_path = last["exe_path"] if last else None
        sha = last["sha256"] if last else None

        # create final record (metrics may be null)
        cur.execute(
            """
            INSERT OR IGNORE INTO activity_log(
                machine_name, user_name, process_name, pid, ppid, exe_path, sha256,
                start_time, sample_time, end_time, duration_seconds,
                boot_time, os_info, current_user, client_version,
                cpu_user_time_s, cpu_system_time_s, rss_bytes,
                io_read_bytes, io_write_bytes, io_read_count, io_write_count,
                net_active, net_conn_count,
                unique_key, sent
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, 0)
            """,
            (
                machine,
                user_name,
                pname,
                pid,
                ppid,
                exe_path,
                sha,
                st,
                et,
                et,
                dur,
                boot_time,
                os_s,
                current_user,
                client_version,
                final_key,
            ),
        )

        # cleanup pid_state row (PID might get reused)
        cur.execute("DELETE FROM wmi_pid_state WHERE pid=?;", (pid,))
        conn.commit()


# Send

def fetch_unsent(conn: sqlite3.Connection, batch_size: int) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM activity_log
        WHERE sent=0
        ORDER BY id
        LIMIT ?
        """,
        (int(batch_size),),
    )
    return cur.fetchall()


def mark_sent(conn: sqlite3.Connection, ids: List[int]) -> None:
    if not ids:
        return
    cur = conn.cursor()
    cur.execute(f"UPDATE activity_log SET sent=1 WHERE id IN ({','.join(['?']*len(ids))});", ids)
    conn.commit()


def send_batch(server_url: str, api_key: str, rows: List[sqlite3.Row]) -> bool:
    payload = []
    for r in rows:
        payload.append({
            "machine_name": r["machine_name"],
            "user_name": r["user_name"],
            "process_name": r["process_name"],
            "pid": r["pid"],
            "ppid": r["ppid"],
            "exe_path": r["exe_path"],
            "sha256": r["sha256"],
            "start_time": r["start_time"],
            "sample_time": r["sample_time"],
            "end_time": r["end_time"],
            "duration_seconds": r["duration_seconds"],
            "boot_time": r["boot_time"],
            "os_info": r["os_info"],
            "current_user": r["current_user"],
            "client_version": r["client_version"],
            "cpu_user_time_s": r["cpu_user_time_s"],
            "cpu_system_time_s": r["cpu_system_time_s"],
            "rss_bytes": r["rss_bytes"],
            "io_read_bytes": r["io_read_bytes"],
            "io_write_bytes": r["io_write_bytes"],
            "io_read_count": r["io_read_count"],
            "io_write_count": r["io_write_count"],
            "net_active": r["net_active"],
            "net_conn_count": r["net_conn_count"],
            "unique_key": r["unique_key"],
        })

    try:
        resp = requests.post(server_url, json=payload, headers={"X-API-Key": api_key}, timeout=60)
        print(f"[send_batch] POST {server_url} -> {resp.status_code}, items={len(payload)}")
        if resp.status_code == 401:
            return False
        return resp.ok
    except Exception as e:
        print(f"[send_batch] ERROR: {e}")
        return False



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Do one cycle and exit")
    args = ap.parse_args()

    cfg = load_config()
    db_path = str(cfg.get("db_path") or "activity.db")
    server_url = str(cfg.get("server_ingest_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    interval_min = int(cfg.get("interval_minutes") or 10)
    batch_size = int(cfg.get("batch_size") or 300)

    if not server_url:
        print("server_ingest_url is empty")
        return 2

    conn = db_connect(db_path)
    ensure_schema(conn)

    # Start WMI watcher (Windows, optional)
    stop_evt = threading.Event()
    if _WMI_AVAILABLE:
        t = threading.Thread(target=_wmi_subscribe, args=(conn, stop_evt), daemon=True)
        t.start()

    client_version = str(cfg.get("client_version") or "").strip() or "1.2"

    try:
        while True:
            collect_slice(conn, client_version)

            rows = fetch_unsent(conn, batch_size)
            if rows:
                ok = send_batch(server_url, api_key, rows)
                if ok:
                    mark_sent(conn, [int(r["id"]) for r in rows])

            if args.once:
                break
            time.sleep(max(5, interval_min * 60))
    finally:
        stop_evt.set()
        try:
            conn.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
