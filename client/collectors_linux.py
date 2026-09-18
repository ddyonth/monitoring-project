"""
Платформенный коллектор для Linux (Astra Linux, ALT Linux и другие дистрибутивы).

Контракт тот же, что у collectors_windows (см. докстринг там). Аналога WMI на
Linux нет: start_time/ppid/process_name берутся напрямую из psutil
(create_time()/ppid) без уточнения, поэтому WMI-функции — заглушки.
"""

import platform
import sqlite3
import threading
from typing import Optional, Tuple

WMI_AVAILABLE = False

# TODO(linux): список системных процессов ниже — первая, заведомо грубая
# итерация. Пересмотреть по факту наблюдений на реальных Astra/ALT-станциях
# (как для Windows-порога rare_thr на сервере): какие root-процессы из
# /usr/lib, /lib, /sbin на самом деле шумят, нужны ли исключения для
# пользовательских сервисов systemd --user, snap/flatpak и т.п.
_ROOT_SYSTEM_PREFIXES = ("/usr/lib/", "/lib/", "/sbin/")
_KTHREADD_PID = 2


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
                      ppid: Optional[int] = None) -> bool:
    """
    True для процессов, которые не мониторим:
      - владелец недоступен (had_access_error);
      - пустой exe_path — типично для kernel threads;
      - ppid == 2 — потомки kthreadd;
      - root-процесс с бинарником из /usr/lib/, /lib/ или /sbin/.
    Обычные пользовательские процессы (в т.ч. root в /usr/bin) не фильтруются.
    """
    if had_access_error:
        return True
    p = (exe_path or "").strip()
    if not p:
        return True
    if ppid is not None and int(ppid) == _KTHREADD_PID:
        return True
    if (user_name or "").strip() == "root" and p.startswith(_ROOT_SYSTEM_PREFIXES):
        return True
    return False
