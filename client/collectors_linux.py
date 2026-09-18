"""
Платформенный коллектор для Linux (Astra Linux, ALT Linux и другие дистрибутивы).

Контракт тот же, что у collectors_windows (см. докстринг там). Аналога WMI на
Linux нет: start_time/ppid/process_name берутся напрямую из psutil
(create_time()/ppid) без уточнения, поэтому WMI-функции — заглушки.
"""

import platform
import re
import sqlite3
import threading
from typing import Optional, Tuple

WMI_AVAILABLE = False

# Правила фильтра системных процессов подобраны по реальному прогону на
# ALT Workstation 11.1 (207 процессов, root и непривилегированный пользователь):
#  - kernel threads надёжно узнаются по pid/ppid == 2, а не по пустому exe:
#    под непривилегированным пользователем пустой exe — это AccessDenied на
#    чужие процессы (root-демоны и root-шеллы), их прятать нельзя;
#  - демоны на ALT живут в /usr/sbin, /usr/libexec, /usr/lib64 (не только
#    /usr/lib, /lib, /sbin), а запущены напрямую init'ом (ppid == 1);
#  - сервисные учётки (0 < uid < UID_MIN) — аналог Windows LOCAL/NETWORK SERVICE.
# TODO(linux): проверить те же правила на Astra Linux.
_KTHREADD_PID = 2
_INIT_PID = 1
_ROOT_DAEMON_PREFIXES = ("/usr/sbin/", "/usr/libexec/", "/usr/lib/", "/usr/lib64/", "/lib/", "/lib64/", "/sbin/")
LOGIN_DEFS_PATH = "/etc/login.defs"
UID_MIN_DEFAULT = 1000
_uid_min_cache: Optional[int] = None


def read_uid_min(path: str = LOGIN_DEFS_PATH) -> int:
    """UID_MIN из /etc/login.defs (строка «UID_MIN   1000»); при любой проблеме — 1000."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*UID_MIN\s+(\d+)\s*$", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return UID_MIN_DEFAULT


def uid_min() -> int:
    """UID_MIN, прочитанный один раз за прогон (кэш в модуле)."""
    global _uid_min_cache
    if _uid_min_cache is None:
        _uid_min_cache = read_uid_min()
    return _uid_min_cache


def _wmi_subscribe(conn: sqlite3.Connection, stop_evt: threading.Event) -> None:
    """No-op: на Linux фонового источника событий процессов нет."""
    return


def wmi_get_pid_times(conn: sqlite3.Connection, pid: int) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Нет WMI — нет уточнения: всегда (None, None, None, None)."""
    return None, None, None, None


def os_info() -> str:
    try:
        return platform.platform()
    except Exception:
        return "unknown"


def is_system_process(user_name: Optional[str], exe_path: Optional[str], had_access_error: bool,
                      ppid: Optional[int] = None, pid: Optional[int] = None,
                      uid: Optional[int] = None) -> bool:
    """
    True для процессов, которые не мониторим (любое из трёх независимых правил):
      1. kernel thread: pid == 2 (kthreadd) или ppid == 2 (его потомки);
      2. сервисная учётка: 0 < uid < UID_MIN из /etc/login.defs (root — не она);
      3. root-демон: root и (pid == 1 или ppid == 1) и exe из системных каталогов
         (/usr/sbin, /usr/libexec, /usr/lib, /usr/lib64, /lib, /lib64, /sbin).
    Пустой exe_path (AccessDenied) сам по себе НЕ признак системного процесса:
    событие сохраняется с exe_path=null как «не смогли определить».
    had_access_error (нет владельца) — по-прежнему фильтр, как на Windows.
    Обычные пользовательские процессы и root-сессии (bash по ssh и т.п.) не фильтруются.
    """
    if had_access_error:
        return True
    pid_i = int(pid) if pid is not None else None
    ppid_i = int(ppid) if ppid is not None else None
    if pid_i == _KTHREADD_PID or ppid_i == _KTHREADD_PID:
        return True
    # root (uid 0) — не сервисная учётка: для него отдельное правило 3
    if uid is not None and 0 < int(uid) < uid_min():
        return True
    p = (exe_path or "").strip()
    if ((user_name or "").strip() == "root" and (pid_i == _INIT_PID or ppid_i == _INIT_PID)
            and p.startswith(_ROOT_DAEMON_PREFIXES)):
        return True
    return False
