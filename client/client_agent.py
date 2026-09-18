import os
import sys
import time
import sqlite3
import socket
import stat
import getpass
import argparse
import hashlib
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psutil
import requests


def select_collectors(os_name: Optional[str] = None):
    """
    Платформенный коллектор: "nt" -> collectors_windows (WMI, Windows-фильтр
    системных процессов), иначе -> collectors_linux (без WMI, Linux-фильтр).
    Оба модуля реализуют один контракт (см. докстринг collectors_windows).
    """
    name = os_name if os_name is not None else os.name
    if name == "nt":
        import collectors_windows as mod
    else:
        import collectors_linux as mod
    return mod


collectors = select_collectors()
# Config

def _load_client_version() -> str:
    """
    Версия этого экземпляра агента — единственная база для сравнения с релизом
    на сервере.

    Источник — файл client/VERSION. В упакованный exe версия попадает на этапе
    сборки: client_agent.spec генерирует модуль _version.py, который PyInstaller
    вшивает в бинарник, поэтому в рантайме с диска ничего не читается
    (в onefile __file__ указывает во временный _MEIPASS, а не в каталог exe).
    При запуске из исходников читается сам файл VERSION рядом с этим .py.
    """
    if getattr(sys, "frozen", False):
        try:
            from _version import CLIENT_VERSION as built  # сгенерирован spec-ом при сборке
            return str(built).strip()
        except Exception:
            # exe собран без spec-а: версии нет, считаем её нулевой, чтобы
            # первый же опубликованный релиз её заменил
            return "0.0"
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0"


CLIENT_VERSION = _load_client_version()

INGEST_SUFFIX = "/api/ingest"

DEFAULT_CONFIG: Dict[str, Any] = {
    "server_ingest_url": "http://127.0.0.1:8000/api/ingest",
    "api_key": "CHANGE_ME_LOCAL_KEY",
    "client_update_key": "CHANGE_ME_CLIENT_KEY",
    "db_path": "activity.db",
    "interval_minutes": 10,
    "batch_size": 300,
}

# Настройка агента — только встроенные значения по умолчанию плюс переменные
# окружения. Файл конфигурации не читается: в упакованном onefile-exe __file__
# указывает во временный каталог PyInstaller, а не в каталог exe, поэтому
# config.json рядом с exe в реальной поставке никогда не работал.
CONFIG_ENV_OVERRIDES = {
    "server_ingest_url": "MONITORING_SERVER_URL",
    "api_key": "MONITORING_API_KEY",
    "client_update_key": "MONITORING_CLIENT_UPDATE_KEY",
}


def load_config() -> Dict[str, Any]:
    """DEFAULT_CONFIG, поверх — непустые переменные окружения из CONFIG_ENV_OVERRIDES."""
    cfg = DEFAULT_CONFIG.copy()
    for key, env_name in CONFIG_ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            cfg[key] = value.strip()
    return cfg


def server_base_url(cfg: Dict[str, Any]) -> Optional[str]:
    """
    Базовый URL сервера выводится из server_ingest_url: единственный адрес
    сервера в конфиге, отдельное поле могло бы разойтись с ним. Требуем
    точный суффикс /api/ingest; иначе обновления отключены (None).
    """
    url = str(cfg.get("server_ingest_url") or "").strip().rstrip("/")
    if not url.endswith(INGEST_SUFFIX):
        return None
    base = url[: -len(INGEST_SUFFIX)]
    return base or None



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


# WMI-подписка, wmi_get_pid_times, os_info и фильтр системных процессов —
# в платформенных модулях collectors_windows / collectors_linux (см. select_collectors).


# Collection

def get_machine_name() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"



def iso_from_ts(ts: float) -> str:
    # Always store UTC timestamps (timezone-aware) to keep duration math consistent
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def normalize_username_display(u: Optional[str]) -> str:
    """Normalize username for storage/display: strip host/domain prefix like DESKTOP-XXX\\user."""
    s = (u or "").strip()
    if "\\" in s:
        s = s.split("\\", 1)[1]
    return s



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
            if collectors.is_system_process(uname, exe, had_err, ppid=ppid):
                continue

            # base start_time from psutil
            st = None
            ct = info.get("create_time")
            if isinstance(ct, (int, float)):
                st = iso_from_ts(float(ct))
            else:
                st = now

            # override with WMI start_time if present (Linux: всегда None — нет уточнения)
            wmi_st, wmi_end, wmi_ppid, wmi_pname = collectors.wmi_get_pid_times(conn, pid)
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
                    collectors.os_info(),
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
    os_s = collectors.os_info()

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



# Self-update
#
# Контроль целостности релиза — только sha256, пересчитанный локально и
# сверенный с метаданными /api/client-release, плюс ключ авторизации и (когда
# сервер за HTTPS) TLS. Криптографической подписи релиза нет — осознанное решение.

DOWNLOAD_SUFFIX = ".download"
OLD_SUFFIX = ".old"


def _log_update(msg: str) -> None:
    print(f"[update] {msg}")


def parse_version(v: Any) -> Optional[Tuple[int, ...]]:
    """'1.10' -> (1, 10); что-то другое -> None. Сравниваем кортежи, не строки."""
    s = str(v or "").strip()
    if not s:
        return None
    parts = s.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer_version(server_version: Any, current_version: Any) -> Optional[bool]:
    """True/False, или None если хотя бы одна из версий не разбирается."""
    sv = parse_version(server_version)
    cv = parse_version(current_version)
    if sv is None or cv is None:
        return None
    return sv > cv


def current_executable_path() -> Optional[str]:
    """Путь к запущенному exe. Только для упакованного PyInstaller-агента:
    при запуске из .py заменять sys.executable (python.exe) нельзя."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def cleanup_old_executable(exe_path: Optional[str]) -> None:
    """Удаляет <exe>.old от прошлого обновления (Windows не даёт удалить
    работающий exe в момент замены, поэтому чистим при следующем старте)."""
    if not exe_path:
        return
    old = exe_path + OLD_SUFFIX
    try:
        if os.path.exists(old):
            os.remove(old)
    except Exception as e:
        _log_update(f"cannot remove {old}: {e}")


def fetch_release_info(base_url: str, client_key: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Метаданные последнего релиза или None (нет релизов / ошибка сети / не 200)."""
    url = f"{base_url}/api/client-release"
    try:
        resp = requests.get(url, headers={"X-Client-Key": client_key}, timeout=timeout)
    except Exception as e:
        _log_update(f"GET {url} failed: {e}")
        return None
    if resp.status_code != 200:
        _log_update(f"GET {url} -> {resp.status_code}")
        return None
    try:
        rel = resp.json().get("client_release")
    except Exception as e:
        _log_update(f"bad JSON from {url}: {e}")
        return None
    if not isinstance(rel, dict):
        return None
    return rel


def download_release(base_url: str, client_key: str, dest_path: str, timeout: int = 300) -> Optional[str]:
    """Скачивает бинарник во временный файл dest_path; возвращает sha256 байт или None."""
    url = f"{base_url}/api/download/client-agent"
    h = hashlib.sha256()
    try:
        with requests.get(url, headers={"X-Client-Key": client_key}, timeout=timeout, stream=True) as resp:
            if resp.status_code != 200:
                _log_update(f"GET {url} -> {resp.status_code}")
                return None
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
    except Exception as e:
        _log_update(f"download failed: {e}")
        _remove_quietly(dest_path)
        return None
    return h.hexdigest()


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def apply_downloaded_release(exe_path: str, downloaded_path: str) -> None:
    """Текущий exe -> exe.old, скачанный -> на место текущего (атомарно через os.replace)."""
    # Скачанный файл создан с правами по умолчанию (без бита исполнения на
    # Linux) — переносим права текущего exe, иначе новую версию нельзя запустить.
    try:
        os.chmod(downloaded_path, stat.S_IMODE(os.stat(exe_path).st_mode))
    except Exception as e:
        _log_update(f"cannot copy file mode to {downloaded_path}: {e}")
    old_path = exe_path + OLD_SUFFIX
    _remove_quietly(old_path)
    os.replace(exe_path, old_path)
    os.replace(downloaded_path, exe_path)


def _child_environment() -> Dict[str, str]:
    """
    Окружение для перезапускаемого exe без служебных переменных PyInstaller
    (_PYI_ARCHIVE_FILE, _PYI_APPLICATION_HOME_DIR, _PYI_PARENT_PROCESS_LEVEL,
    _MEIPASS2 и т.п.). Унаследовав их, новый onefile-exe использует временный
    каталог родителя, который удаляется при выходе старого процесса, и гибнет.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}


def relaunch(exe_path: str, argv: List[str]) -> None:
    """Запускает новый exe с теми же аргументами независимо от текущего процесса."""
    kwargs: Dict[str, Any] = {
        "cwd": os.path.dirname(exe_path) or None,
        "env": _child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe_path] + list(argv), **kwargs)


def check_and_apply_update(
    cfg: Dict[str, Any],
    current_version: str,
    exe_path: Optional[str] = None,
    do_relaunch: bool = True,
    argv: Optional[List[str]] = None,
    timeout: int = 30,
) -> str:
    """
    Один цикл проверки/применения обновления. Возвращает:
      "up_to_date"  — обновлять нечего (или сервер недоступен / нет релизов);
      "updated"     — новый exe установлен (и, если do_relaunch, запущен);
                      вызывающий код должен корректно завершиться;
      "error"       — обновление есть, но применить не удалось (см. лог).
    exe_path по умолчанию — путь запущенного упакованного exe; при запуске
    из .py обновление не применяется.
    """
    base = server_base_url(cfg)
    if not base:
        _log_update("server_ingest_url does not end with /api/ingest, updates disabled")
        return "up_to_date"
    client_key = str(cfg.get("client_update_key") or "").strip()

    rel = fetch_release_info(base, client_key, timeout=timeout)
    if not rel:
        return "up_to_date"

    server_version = str(rel.get("version") or "").strip()
    expected_sha = str(rel.get("sha256") or "").strip().lower()
    newer = is_newer_version(server_version, current_version)
    if newer is None:
        _log_update(f"cannot compare versions: server={server_version!r}, current={current_version!r}, skip")
        return "up_to_date"
    if not newer:
        _log_update(f"current {current_version} is up to date (server {server_version})")
        return "up_to_date"
    if not expected_sha:
        _log_update(f"release {server_version} has no sha256, skip")
        return "up_to_date"

    exe = exe_path or current_executable_path()
    if not exe:
        _log_update(f"release {server_version} is newer than {current_version}, "
                    "but self-update works only for the packaged exe (not running frozen)")
        return "error"

    tmp = exe + DOWNLOAD_SUFFIX
    _log_update(f"downloading {server_version} to {tmp}")
    actual_sha = download_release(base, client_key, tmp)
    if not actual_sha:
        _remove_quietly(tmp)
        return "error"
    if actual_sha != expected_sha:
        _log_update(f"sha256 mismatch: expected {expected_sha}, got {actual_sha}; update rejected")
        _remove_quietly(tmp)
        return "error"

    try:
        apply_downloaded_release(exe, tmp)
    except Exception as e:
        _log_update(f"cannot replace executable: {e}")
        _remove_quietly(tmp)
        return "error"
    _log_update(f"installed {server_version} to {exe}")

    if do_relaunch:
        args = list(sys.argv[1:] if argv is None else argv)
        try:
            relaunch(exe, args)
            _log_update(f"relaunched {exe} {' '.join(args)}")
        except Exception as e:
            _log_update(f"relaunch failed: {e} (the new version will start on next launch)")
    return "updated"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Do one cycle and exit (no update check)")
    ap.add_argument(
        "--apply-update-now",
        action="store_true",
        help="Check the server for a newer agent release, apply it and exit (no collection)",
    )
    args = ap.parse_args()

    cfg = load_config()
    client_version = CLIENT_VERSION

    cleanup_old_executable(current_executable_path())

    if args.apply_update_now:
        result = check_and_apply_update(cfg, CLIENT_VERSION)
        _log_update(f"result: {result}")
        return 0 if result in ("up_to_date", "updated") else 1

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

    # Start WMI watcher (Windows only; на Linux collectors.WMI_AVAILABLE == False)
    stop_evt = threading.Event()
    if collectors.WMI_AVAILABLE:
        t = threading.Thread(target=collectors._wmi_subscribe, args=(conn, stop_evt), daemon=True)
        t.start()

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

            # Проверка обновления раз в цикл; в режиме --once не выполняется.
            if check_and_apply_update(cfg, CLIENT_VERSION) == "updated":
                # новый exe уже запущен; завершаемся корректно (finally ниже)
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
