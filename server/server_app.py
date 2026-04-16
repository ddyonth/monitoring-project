import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Config / Paths

def resource_dir() -> str:
    """Directory containing this script (works for PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


RES_DIR = resource_dir()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# API keys (can be overridden in config.json)
API_KEY_DEFAULT = "CHANGE_ME_LOCAL_KEY"
CLIENT_UPDATE_KEY_DEFAULT = "CHANGE_ME_CLIENT_KEY"

# analytics defaults
PROFILE_WINDOW_DAYS_DEFAULT = 14
RARE_COUNT_THRESHOLD_DEFAULT = 3
RARE_MACHINE_THRESHOLD_DEFAULT = 2
TIME_HIST_MIN_SAMPLES_DEFAULT = 20
ONLINE_THRESHOLD_MINUTES_DEFAULT = 20


# Alerts (MVP) defaults

ALERT_BASELINE_WINDOW_DAYS_DEFAULT = 14
ALERT_BASELINE_MIN_POINTS_DEFAULT = 20

# ratio K + absolute thresholds
ALERT_RULES = {
    "cpu_delta": {"K": 5.0, "abs": 1.0, "very_high_abs": 5.0},                      # seconds per interval
    "io_delta": {"K": 10.0, "abs": float(50 * 1024 * 1024), "very_high_abs": float(200 * 1024 * 1024)},  # bytes
    "rss": {"K": 3.0, "abs": float(500 * 1024 * 1024), "very_high_abs": float(2 * 1024 * 1024 * 1024)},  # bytes
    "net_conn_count": {"K": 5.0, "abs": 20.0, "very_high_abs": 100.0},              # connections
}


def _read_config_from_disk() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f) if f.readable() else {}
    except Exception:
        return {}


def _get_cfg() -> Dict[str, Any]:
    cfg = _read_config_from_disk()
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _cfg_get(key: str, default: Any) -> Any:
    cfg = _get_cfg()
    v = cfg.get(key, default)
    return v


def _safe_parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = str(s).strip()
        if s2.endswith("Z"):
            s2 = s2[:-1]
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _local_hour_from_iso(s: Optional[str]) -> Optional[int]:
    dt = _safe_parse_iso(s)
    if not dt:
        return None
    try:
        return int(dt.astimezone().hour)
    except Exception:
        return int(dt.hour)

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema() -> None:
    conn = db_connect()
    cur = conn.cursor()

    # --- main append-only events table (one row per sample)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_name TEXT NOT NULL,
            user_name TEXT,
            process_name TEXT NOT NULL,
            pid INTEGER,
            ppid INTEGER,
            exe_path TEXT,

            start_time TEXT NOT NULL,
            sample_time TEXT NOT NULL,
            end_time TEXT,
            duration_seconds INTEGER NOT NULL,

            cpu_user_time_s REAL,
            cpu_system_time_s REAL,
            rss_bytes INTEGER,
            io_read_bytes INTEGER,
            io_write_bytes INTEGER,
            io_read_count INTEGER,
            io_write_count INTEGER,
            net_active INTEGER,
            net_conn_count INTEGER,

            boot_time TEXT,
            os_info TEXT,
            current_user TEXT,
            client_version TEXT,

            unique_key TEXT NOT NULL UNIQUE,
            received_at TEXT NOT NULL
        );
        """
    )

    # --- MIGRATIONS for existing DBs (backward-compatible)
    cur.execute("PRAGMA table_info(events);")
    cols = {row[1] for row in cur.fetchall()}

    if "sha256" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN sha256 TEXT;")
    if "end_time" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN end_time TEXT;")
    if "ppid" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN ppid INTEGER;")
    if "net_active" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN net_active INTEGER;")
    if "net_conn_count" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN net_conn_count INTEGER;")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(machine_name, pid, start_time, sample_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_sha ON events(sha256);")

    # --- machine inventory/state
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_state (
            machine_name TEXT PRIMARY KEY,
            boot_time TEXT,
            os_info TEXT,
            current_user TEXT,
            client_version TEXT,
            last_seen TEXT,
            updated_at TEXT
        );
        """
    )

    cur.execute("PRAGMA table_info(machine_state);")
    ms_cols = {row[1] for row in cur.fetchall()}

    if "updated_at" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN updated_at TEXT;")
    if "last_seen" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN last_seen TEXT;")
    if "client_version" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN client_version TEXT;")
    if "current_user" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN current_user TEXT;")
    if "os_info" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN os_info TEXT;")
    if "boot_time" not in ms_cols:
        cur.execute("ALTER TABLE machine_state ADD COLUMN boot_time TEXT;")

    # --- aliases
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_aliases (
            machine_name TEXT PRIMARY KEY,
            alias TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    # --- admin-managed process catalog
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS process_catalog (
            process_name TEXT PRIMARY KEY,
            description TEXT,
            process_type TEXT
        );
        """
    )

    # --- chain catalog
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chain_catalog (
            chain_key TEXT PRIMARY KEY,
            chain_name TEXT NOT NULL,
            chain_type TEXT,
            description TEXT,
            updated_at TEXT
        );
        """
    )

    cur.execute("PRAGMA table_info(chain_catalog);")
    cc_cols = {row[1] for row in cur.fetchall()}
    if "updated_at" not in cc_cols:
        cur.execute("ALTER TABLE chain_catalog ADD COLUMN updated_at TEXT;")


    # --- roles (minimal)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS roles (
            role_id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_name TEXT NOT NULL UNIQUE,
            description TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_roles (
            machine_name TEXT PRIMARY KEY,
            role_id INTEGER,
            updated_at TEXT,
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS role_profiles (
            role_id INTEGER PRIMARY KEY,
            allowed_hashes_json TEXT,
            updated_at TEXT,
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS role_allowed_process_types (
            role_id INTEGER NOT NULL,
            process_type TEXT NOT NULL,
            updated_at TEXT,
            PRIMARY KEY(role_id, process_type),
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )

    # Alerts (MVP): server-side detections stored in DB
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            created_at TEXT NOT NULL,
            sample_time TEXT NOT NULL,

            machine_name TEXT NOT NULL,
            user_name TEXT NOT NULL,

            entity_type TEXT NOT NULL,          -- process_session | process_chain

            process_name TEXT NOT NULL,
            pid INTEGER,
            start_time TEXT,

            sha256 TEXT,
            exe_path TEXT,

            parent_process_name TEXT,
            parent_sha256 TEXT,
            parent_exe_path TEXT,
            chain_key TEXT,

            metric TEXT NOT NULL,               -- cpu_delta | io_delta | rss | net_conn_count | rarity | chain_rarity | time_anomaly
            value REAL,
            baseline REAL,
            score REAL,
            severity TEXT NOT NULL,
            reason TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'new',
            ack_by TEXT,
            ack_at TEXT,
            closed_at TEXT,

            bucket_hour TEXT NOT NULL,
            dedup_key TEXT NOT NULL
        );
        """
    )
    cur.execute("PRAGMA table_info(alerts);")
    alert_cols = {row[1] for row in cur.fetchall()}
    if "entity_type" not in alert_cols:
        cur.execute("ALTER TABLE alerts ADD COLUMN entity_type TEXT;")
    if "parent_process_name" not in alert_cols:
        cur.execute("ALTER TABLE alerts ADD COLUMN parent_process_name TEXT;")
    if "parent_sha256" not in alert_cols:
        cur.execute("ALTER TABLE alerts ADD COLUMN parent_sha256 TEXT;")
    if "parent_exe_path" not in alert_cols:
        cur.execute("ALTER TABLE alerts ADD COLUMN parent_exe_path TEXT;")
    if "chain_key" not in alert_cols:
        cur.execute("ALTER TABLE alerts ADD COLUMN chain_key TEXT;")

    # indexes for speed
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_sample_time ON alerts(sample_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status_sev_time ON alerts(status, severity, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_machine_time ON alerts(machine_name, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts(user_name, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_metric_time ON alerts(metric, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_sha_time ON alerts(sha256, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_entity_metric_time ON alerts(entity_type, metric, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_chain_time ON alerts(chain_key, created_at);")

    # strict hourly dedup
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedup_key ON alerts(dedup_key);")


    conn.commit()
    conn.close()


def require_api_key(x_api_key: Optional[str]) -> None:
    api_key = str(_cfg_get("api_key", API_KEY_DEFAULT)).strip()
    if not x_api_key or x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_client_key(x_client_key: Optional[str]) -> None:
    client_key = str(_cfg_get("client_update_key", CLIENT_UPDATE_KEY_DEFAULT)).strip()
    if not x_client_key or x_client_key != client_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def upsert_machine_state(machine: str, info: Dict[str, Optional[str]], now: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO machine_state(machine_name, boot_time, os_info, current_user, client_version, last_seen, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(machine_name) DO UPDATE SET
            boot_time=COALESCE(excluded.boot_time, machine_state.boot_time),
            os_info=COALESCE(excluded.os_info, machine_state.os_info),
            current_user=COALESCE(excluded.current_user, machine_state.current_user),
            client_version=COALESCE(excluded.client_version, machine_state.client_version),
            last_seen=MAX(COALESCE(excluded.last_seen, machine_state.last_seen), machine_state.last_seen),
            updated_at=excluded.updated_at
        """,
        (
            machine,
            info.get("boot_time"),
            info.get("os_info"),
            info.get("current_user"),
            info.get("client_version"),
            info.get("last_seen"),
            now,
        ),
    )
    conn.commit()
    conn.close()


def validate_event(e: Dict[str, Any]) -> None:
    required = ["machine_name", "process_name", "start_time", "sample_time", "duration_seconds", "unique_key"]
    for k in required:
        if k not in e:
            raise HTTPException(status_code=400, detail=f"Missing field: {k}")


def insert_events(events: List[Dict[str, Any]]) -> Dict[str, int]:
    inserted = 0
    deduped = 0
    now = datetime.now().isoformat(timespec="seconds")

    machines_seen: Dict[str, Dict[str, Optional[str]]] = {}

    conn = db_connect()
    cur = conn.cursor()

    for e in events:
        machine = e["machine_name"]
        sample_time = e.get("sample_time")
        machines_seen[machine] = {
            "boot_time": e.get("boot_time"),
            "os_info": e.get("os_info"),
            "current_user": e.get("current_user"),
            "client_version": e.get("client_version"),
            "last_seen": sample_time,
        }

        try:
            cur.execute(
                """
                INSERT INTO events(
                    machine_name, user_name, process_name, pid, ppid, exe_path, sha256,
                    start_time, sample_time, end_time, duration_seconds,
                    cpu_user_time_s, cpu_system_time_s, rss_bytes,
                    io_read_bytes, io_write_bytes, io_read_count, io_write_count,
                    net_active, net_conn_count,
                    boot_time, os_info, current_user, client_version,
                    unique_key, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    e["machine_name"],
                    e.get("user_name"),
                    e["process_name"],
                    e.get("pid"),
                    e.get("ppid"),
                    e.get("exe_path"),
                    e.get("sha256"),
                    e["start_time"],
                    e["sample_time"],
                    e.get("end_time"),
                    int(e["duration_seconds"]),
                    e.get("cpu_user_time_s"),
                    e.get("cpu_system_time_s"),
                    e.get("rss_bytes"),
                    e.get("io_read_bytes"),
                    e.get("io_write_bytes"),
                    e.get("io_read_count"),
                    e.get("io_write_count"),
                    e.get("net_active"),
                    e.get("net_conn_count"),
                    e.get("boot_time"),
                    e.get("os_info"),
                    e.get("current_user"),
                    e.get("client_version"),
                    e["unique_key"],
                    now,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            deduped += 1
        except Exception:
            deduped += 1

    conn.commit()
    conn.close()

    for machine, info in machines_seen.items():
        upsert_machine_state(machine, info, now)

    return {"inserted": inserted, "deduped": deduped}


# FastAPI

app = FastAPI(title="Monitoring Server (MVP)")

static_dir = os.path.join(RES_DIR, "static")
templates_dir = os.path.join(RES_DIR, "templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)


@app.on_event("startup")
def _startup() -> None:
    ensure_schema()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    refresh_seconds = int(_cfg_get("refresh_seconds", _cfg_get("web_refresh_seconds", 120)))
    build_id = str(int(time.time()))
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "refresh_seconds": refresh_seconds, "build_id": build_id},
    )


@app.post("/api/ingest")
def ingest(payload: List[Dict[str, Any]] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="Payload must be a JSON array")

    events: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        validate_event(item)
        events.append(item)

    if not events:
        return {"ok": True, "inserted": 0, "deduped": 0}

    res = insert_events(events)

    # Alerts detector (MVP): server-side, consistent across devices
    try:
        alerts_inserted = detect_alerts_for_ingested_events(events)
    except Exception as e:
        print(f"[ingest] alerts detector error: {type(e).__name__}: {e}", file=sys.stderr)
        alerts_inserted = 0

    return {"ok": True, **res, "alerts_inserted": int(alerts_inserted)}

def _latest_samples_by_session(cur: sqlite3.Cursor, machine: str, since_iso: str) -> List[sqlite3.Row]:
    cur.execute(
        """
        WITH latest AS (SELECT pid, start_time, MAX(sample_time) AS max_sample
                        FROM events
                        WHERE machine_name = ?
                          AND sample_time >= ?
                        GROUP BY pid, start_time),
             prev AS (SELECT e.pid, e.start_time, MAX(e.sample_time) AS prev_sample
                      FROM events e
                               JOIN latest l
                                    ON e.pid IS l.pid AND e.start_time = l.start_time
                      WHERE e.machine_name = ?
                        AND e.sample_time < l.max_sample
                      GROUP BY e.pid, e.start_time)
        SELECT e.*,
               p.prev_sample                          AS prev_sample_time,
               (SELECT cpu_user_time_s
                FROM events e2
                WHERE e2.machine_name = e.machine_name
                  AND e2.pid IS e.pid
                  AND e2.start_time = e.start_time
                  AND e2.sample_time = p.prev_sample) AS prev_cpu_user_time_s,
               (SELECT cpu_system_time_s
                FROM events e2
                WHERE e2.machine_name = e.machine_name
                  AND e2.pid IS e.pid
                  AND e2.start_time = e.start_time
                  AND e2.sample_time = p.prev_sample) AS prev_cpu_system_time_s,
               CASE
                   WHEN p.prev_sample IS NULL OR e.cpu_user_time_s IS NULL THEN NULL
                   ELSE MAX(0, e.cpu_user_time_s - (SELECT cpu_user_time_s
                                                    FROM events e2
                                                    WHERE e2.machine_name = e.machine_name
                                                      AND e2.pid IS e.pid
                                                      AND e2.start_time = e.start_time
                                                      AND e2.sample_time = p.prev_sample))
                   END                                AS cpu_delta_user_s,
               CASE
                   WHEN p.prev_sample IS NULL OR e.cpu_system_time_s IS NULL THEN NULL
                   ELSE MAX(0, e.cpu_system_time_s - (SELECT cpu_system_time_s
                                                      FROM events e2
                                                      WHERE e2.machine_name = e.machine_name
                                                        AND e2.pid IS e.pid
                                                        AND e2.start_time = e.start_time
                                                        AND e2.sample_time = p.prev_sample))
                   END                                AS cpu_delta_system_s
        FROM events e
                 JOIN latest l
                      ON e.machine_name = ? AND e.pid IS l.pid AND e.start_time = l.start_time AND
                         e.sample_time = l.max_sample
                 LEFT JOIN prev p
                           ON p.pid IS e.pid AND p.start_time = e.start_time
        ORDER BY e.process_name COLLATE NOCASE
        """,
        (machine, since_iso, machine, machine),
    )

    return cur.fetchall()


def _group_stopped(rows: List[sqlite3.Row], limit_per_group: int = 80) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        name = r["process_name"] or "unknown"
        g = groups.get(name)
        if not g:
            g = {"process_name": name, "count": 0, "total_duration_seconds": 0, "items": []}
            groups[name] = g
        g["count"] += 1
        g["total_duration_seconds"] += int(r["duration_seconds"] or 0)
        if len(g["items"]) < limit_per_group:
            g["items"].append(dict(r))
    return list(groups.values())


@app.get("/api/latest")
def latest(x_api_key: Optional[str] = Header(default=None), limit_machines: int = 50):
    require_api_key(x_api_key)

    now_dt = datetime.now(timezone.utc)
    online_thr = int(_cfg_get("online_threshold_minutes", ONLINE_THRESHOLD_MINUTES_DEFAULT))
    online_delta = timedelta(minutes=online_thr)

    cfg = _get_cfg()
    rel = cfg.get("client_release") or {}
    latest_client_version = str(rel.get("version") or "").strip()

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT ms.machine_name, ms.boot_time, ms.os_info, ms.current_user, ms.client_version, ms.last_seen,
               COALESCE(ma.alias,'') AS alias
        FROM machine_state ms
        LEFT JOIN machine_aliases ma ON ma.machine_name = ms.machine_name
        ORDER BY ms.machine_name
        LIMIT ?
        """,
        (int(limit_machines),),
    )
    machine_rows = cur.fetchall()

    window_hours = int(_cfg_get("latest_window_hours", 48))
    since_iso = (now_dt - timedelta(hours=window_hours)).isoformat(timespec="seconds")

    result: List[Dict[str, Any]] = []
    for mr in machine_rows:
        machine = mr["machine_name"]
        last_seen = mr["last_seen"]
        last_seen_dt = _safe_parse_iso(last_seen)
        online = bool(last_seen_dt and (now_dt - last_seen_dt) <= online_delta)

        offline_since = None
        if (not online) and last_seen_dt:
            offline_since = (last_seen_dt + online_delta).isoformat(timespec="seconds")

        rows = _latest_samples_by_session(cur, machine, since_iso)

        # Determine "running" strictly from the most recent slice.
        # Anything not present in the latest slice is considered stopped (even if we never received an explicit end_time).
        latest_slice = last_seen  # machine_state.last_seen is updated from ingested sample_time
        running_rows = [r for r in rows if (not r["end_time"]) and (r["sample_time"] == latest_slice)]
        stopped_rows = [r for r in rows if r["end_time"] or (r["sample_time"] != latest_slice)]

        # If a process disappeared between slices, synthesize an end_time = last observed sample_time.
        stopped_synth: List[Dict[str, Any]] = []
        for r in stopped_rows:
            d = dict(r)
            if (not d.get("end_time")) and d.get("sample_time"):
                d["end_time"] = d["sample_time"]
            stopped_synth.append(d)

        def _choose_main(group: List[sqlite3.Row]) -> Dict[str, Any]:
            g = [dict(x) for x in group]
            pids = {int(x.get("pid") or 0) for x in g if x.get("pid") is not None}
            ppids = {int(x.get("ppid") or 0) for x in g if x.get("ppid") is not None}
            parent_candidates = [x for x in g if int(x.get("pid") or 0) in ppids]
            candidates = parent_candidates or g

            def _key(x: Dict[str, Any]):
                st = _safe_parse_iso(str(x.get("start_time") or ""))
                st_ts = st.timestamp() if st else float("inf")
                dur = int(x.get("duration_seconds") or 0)
                # earliest start_time first; then longest duration; then stable by pid
                return (st_ts, -dur, int(x.get("pid") or 0))

            candidates.sort(key=_key)
            return candidates[0] if candidates else {}

        # Dedup within the slice by process_name -> pick "main" row by rules.
        running_by_name: Dict[str, List[sqlite3.Row]] = {}
        for r in running_rows:
            running_by_name.setdefault(r["process_name"], []).append(r)

        running_main: List[Dict[str, Any]] = []
        for name in sorted(running_by_name.keys(), key=lambda s: (s or "").lower()):
            main = _choose_main(running_by_name[name])
            if main:
                running_main.append(main)

        # Dedup stopped within the last-observed slice by process_name -> keep only the "main" (parent) process.
        # This prevents child processes (tabs, helper processes) from inflating stopped counts and total time.
        stopped_by_name: Dict[str, List[Dict[str, Any]]] = {}
        for d in stopped_synth:
            stopped_by_name.setdefault(str(d.get("process_name") or "unknown"), []).append(d)

        stopped_main: List[Dict[str, Any]] = []
        for name in sorted(stopped_by_name.keys(), key=lambda s: (s or "").lower()):
            g = stopped_by_name[name]
            pids = {int(x.get("pid") or 0) for x in g if x.get("pid") is not None}
            ppids = {int(x.get("ppid") or 0) for x in g if x.get("ppid") is not None}
            parent_candidates = [x for x in g if int(x.get("pid") or 0) in ppids]
            candidates = parent_candidates or g

            def _key2(x: Dict[str, Any]):
                st2 = _safe_parse_iso(str(x.get("start_time") or ""))
                st_ts2 = st2.timestamp() if st2 else float("inf")
                dur2 = int(x.get("duration_seconds") or 0)
                return (st_ts2, -dur2, int(x.get("pid") or 0))

            candidates = sorted(candidates, key=_key2)
            if candidates:
                stopped_main.append(candidates[0])

        stopped_groups = _group_stopped(stopped_main)


        client_version = mr["client_version"] or ""
        client_outdated = bool(latest_client_version and client_version and (client_version != latest_client_version))

        result.append(
            {
                "machine_name": machine,
                "online": online,
                "offline_since": offline_since,
                "sample_time": last_seen or "",
                "alias": ((mr["alias"] if ("alias" in mr.keys()) else "") or ""),
                "boot_time": mr["boot_time"],
                "os_info": mr["os_info"],
                "current_user": mr["current_user"],
                "client_version": client_version,
                "client_outdated": client_outdated,
                "latest_client_version": latest_client_version,
                "running_main": running_main,
                "stopped_groups": stopped_groups,
            }
        )

    conn.close()
    return {"latest": result}



# Catalog + Aliases (legacy endpoints)

@app.get("/api/process-catalog")
def get_process_catalog(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT process_name, description, process_type AS type, process_type FROM process_catalog ORDER BY process_name;")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": rows}

@app.get("/api/chain-catalog")
def get_chain_catalog(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT chain_key, chain_name, chain_type, description, updated_at
        FROM chain_catalog
        ORDER BY chain_name COLLATE NOCASE, chain_key COLLATE NOCASE
        """
    )
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items}


@app.post("/api/chain-catalog-item")
def upsert_chain_catalog_item(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    chain_key = str(payload.get("chain_key") or "").strip()
    chain_name = str(payload.get("chain_name") or "").strip()
    chain_type = str(payload.get("chain_type") or "").strip()
    description = str(payload.get("description") or "").strip()

    if not chain_key:
        raise HTTPException(status_code=400, detail="chain_key required")
    if not chain_name:
        raise HTTPException(status_code=400, detail="chain_name required")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chain_catalog(chain_key, chain_name, chain_type, description, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(chain_key) DO UPDATE SET
            chain_name=excluded.chain_name,
            chain_type=excluded.chain_type,
            description=excluded.description,
            updated_at=excluded.updated_at
        """,
        (chain_key, chain_name, chain_type or None, description or None, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/process-catalog-item")
def upsert_process_catalog_item(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    process_name = str(payload.get("process_name", "")).strip()
    process_type = str(payload.get("process_type", "") or "").strip()
    description = str(payload.get("description", "") or "").strip()
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO process_catalog(process_name, description, process_type)
        VALUES(?, ?, ?)
        ON CONFLICT(process_name) DO UPDATE SET
            description=excluded.description,
            process_type=excluded.process_type
        """,
        (process_name, description, process_type),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/machine-aliases")
def machine_aliases(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT machine_name, alias FROM machine_aliases ORDER BY machine_name;")
    rows = cur.fetchall()
    conn.close()
    return {"aliases": {r["machine_name"]: r["alias"] for r in rows}}


@app.post("/api/machine-alias")
def set_machine_alias(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    machine_name = str(payload.get("machine_name", "")).strip()
    alias = payload.get("alias", None)
    alias_str = "" if alias is None else str(alias).strip()
    if not machine_name:
        raise HTTPException(status_code=400, detail="machine_name is required")

    now = datetime.now().isoformat(timespec="seconds")
    conn = db_connect()
    cur = conn.cursor()

    if alias_str == "":
        cur.execute("DELETE FROM machine_aliases WHERE machine_name=?;", (machine_name,))
        conn.commit()
        conn.close()
        return {"ok": True, "action": "deleted"}

    cur.execute(
        """
        INSERT INTO machine_aliases(machine_name, alias, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(machine_name) DO UPDATE SET
            alias=excluded.alias,
            updated_at=excluded.updated_at
        """,
        (machine_name, alias_str, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "action": "upserted"}


# Machines (for Analytics/Settings tabs)

@app.get("/api/machines")
def get_machines(x_api_key: Optional[str] = Header(default=None), limit: int = 500):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ms.machine_name,
               ma.alias AS alias,
               mr.role_id AS role_id,
               r.role_name AS role_name,
               ms.last_seen AS last_seen,
               ms.os_info AS os_info,
               ms.current_user AS current_user,
               ms.client_version AS client_version
        FROM machine_state ms
        LEFT JOIN machine_aliases ma ON ma.machine_name = ms.machine_name
        LEFT JOIN machine_roles mr ON mr.machine_name = ms.machine_name
        LEFT JOIN roles r ON r.role_id = mr.role_id
        ORDER BY ms.machine_name
        LIMIT ?
        """,
        (int(limit),),
    )
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items}



# Roles (minimal API for Settings tab)

@app.get("/api/roles")
def get_roles(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.role_id, r.role_name, r.description
        FROM roles r
        ORDER BY r.role_name
        """
    )
    roles = []
    for row in cur.fetchall():
        item = dict(row)
        allowed_process_types = _get_role_allowed_process_types(cur, int(item["role_id"]))
        item["allowed_process_types"] = allowed_process_types
        item["allowed_types_text"] = ", ".join(allowed_process_types)
        roles.append(item)

    conn.close()
    return {"items": roles}


@app.post("/api/roles")
def create_role(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    role_name = str(payload.get("role_name", "")).strip()
    description = str(payload.get("description", "") or "").strip()
    allowed_process_types = payload.get("allowed_process_types", [])
    has_allowed_process_types = "allowed_process_types" in payload
    if not role_name:
        raise HTTPException(status_code=400, detail="role_name is required")

    conn = db_connect()
    cur = conn.cursor()
    try:
        try:
            cur.execute("INSERT INTO roles(role_name, description) VALUES(?, ?);", (role_name, description))
            conn.commit()
            rid = cur.lastrowid
        except sqlite3.IntegrityError:
            cur.execute("SELECT role_id FROM roles WHERE role_name=? LIMIT 1;", (role_name,))
            row = cur.fetchone()
            rid = int(row["role_id"]) if row else None
            conn.commit()

        if rid is not None and has_allowed_process_types:
            _replace_role_allowed_process_types(cur, int(rid), allowed_process_types)
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "role_id": rid}

@app.post("/api/role")
def update_role(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    role_id = payload.get("role_id", None)
    role_name = payload.get("role_name", None)
    description = str(payload.get("description", "") or "").strip()
    allowed_process_types = payload.get("allowed_process_types", None)
    if role_id is None:
        raise HTTPException(status_code=400, detail="role_id is required")

    conn = db_connect()
    cur = conn.cursor()
    try:
        if role_name is None:
            # backward compatible: update description only
            cur.execute("UPDATE roles SET description=? WHERE role_id=?;", (description, int(role_id)))
        else:
            rn = str(role_name).strip()
            if not rn:
                raise HTTPException(status_code=400, detail="role_name is required")
            cur.execute("UPDATE roles SET role_name=?, description=? WHERE role_id=?;", (rn, description, int(role_id)))
            if allowed_process_types is not None:
                _replace_role_allowed_process_types(cur, int(role_id), allowed_process_types)
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="role_name must be unique")
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/machine-roles")
def get_machine_roles(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT mr.machine_name, mr.role_id, r.role_name
        FROM machine_roles mr
        LEFT JOIN roles r ON r.role_id=mr.role_id
        ORDER BY mr.machine_name
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": rows}


@app.post("/api/machine-role")
def set_machine_role(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    machine_name = str(payload.get("machine_name", "")).strip()
    role_id = payload.get("role_id", None)
    if not machine_name:
        raise HTTPException(status_code=400, detail="machine_name is required")
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_connect()
    cur = conn.cursor()
    if role_id is None:
        cur.execute("DELETE FROM machine_roles WHERE machine_name=?;", (machine_name,))
    else:
        cur.execute(
            """
            INSERT INTO machine_roles(machine_name, role_id, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(machine_name) DO UPDATE SET role_id=excluded.role_id, updated_at=excluded.updated_at
            """,
            (machine_name, int(role_id), now),
        )
    conn.commit()
    conn.close()
    return {"ok": True}


# Analytics (MVP)
# Alerts (MVP): server-side detector


def _median(values: List[float]) -> Optional[float]:
    vv = [float(x) for x in values if x is not None]  # type: ignore[arg-type]
    vv = [x for x in vv if isinstance(x, (int, float))]
    if not vv:
        return None
    vv.sort()
    n = len(vv)
    mid = n // 2
    if n % 2 == 1:
        return float(vv[mid])
    return float((vv[mid - 1] + vv[mid]) / 2.0)


def _bucket_hour_from_sample(sample_time: str) -> str:
    dt = _safe_parse_iso(sample_time)
    if not dt:
        # fallback: best-effort "YYYY-MM-DDTHH"
        s = str(sample_time).strip()
        return s[:13] if len(s) >= 13 else s
    # keep it consistent: use the parsed timezone (agent sends UTC)
    return dt.strftime("%Y-%m-%dT%H")


def _session_key_from_row(r: sqlite3.Row) -> Tuple[str, int, str]:
    return (str(r["machine_name"]), int(r["pid"] or 0), str(r["start_time"]))


def _get_latest_and_prev_for_session(cur: sqlite3.Cursor, machine: str, pid: int, start_time: str, current_sample: str) -> Tuple[Optional[sqlite3.Row], Optional[sqlite3.Row]]:
    cur.execute(
        """
        SELECT *
        FROM events
        WHERE machine_name=? AND pid IS ? AND start_time=? AND sample_time<=?
        ORDER BY sample_time DESC
        LIMIT 2
        """,
        (machine, int(pid), start_time, current_sample),
    )
    rows = cur.fetchall()
    if not rows:
        return None, None
    latest = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    return latest, prev


def _collect_baseline_values(
    cur: sqlite3.Cursor,
    metric: str,
    user_name: str,
    sha256: str,
    exe_path: str,
    since_iso: str,
    until_iso: str,
) -> List[float]:
    """
    Baseline = median over last N days for (user, sha256, metric),
    fallback to (user, exe_path, metric) if sha256 is empty.
    For cpu_delta/io_delta baseline: compute deltas per session in python from rows.
    """
    use_sha = bool((sha256 or "").strip())
    vals: List[float] = []

    if metric in ("rss", "net_conn_count"):
        metric_col = "rss_bytes" if metric == "rss" else "net_conn_count"

        if use_sha:
            cur.execute(
                f"""
                SELECT {metric_col} AS v
                FROM events
                WHERE user_name=? AND NULLIF(sha256,'') IS NOT NULL AND sha256=?
                  AND sample_time>=? AND sample_time<?
                  AND {metric_col} IS NOT NULL
                """,
                (user_name, sha256, since_iso, until_iso),
            )
        else:
            cur.execute(
                f"""
                SELECT {metric_col} AS v
                FROM events
                WHERE user_name=? AND (sha256 IS NULL OR sha256='') AND exe_path=?
                  AND sample_time>=? AND sample_time<?
                  AND {metric_col} IS NOT NULL
                """,
                (user_name, exe_path, since_iso, until_iso),
            )
        for r in cur.fetchall():
            try:
                vals.append(float(r["v"]))
            except Exception:
                continue
        return vals

    # cpu_delta / io_delta: need adjacent samples inside the same session
    if use_sha:
        cur.execute(
            """
            SELECT machine_name, pid, start_time, sample_time,
                   cpu_user_time_s, cpu_system_time_s,
                   io_read_bytes, io_write_bytes
            FROM events
            WHERE user_name=? AND NULLIF(sha256,'') IS NOT NULL AND sha256=?
              AND sample_time>=? AND sample_time<?
            ORDER BY machine_name, pid, start_time, sample_time
            """,
            (user_name, sha256, since_iso, until_iso),
        )
    else:
        cur.execute(
            """
            SELECT machine_name, pid, start_time, sample_time,
                   cpu_user_time_s, cpu_system_time_s,
                   io_read_bytes, io_write_bytes
            FROM events
            WHERE user_name=? AND (sha256 IS NULL OR sha256='') AND exe_path=?
              AND sample_time>=? AND sample_time<?
            ORDER BY machine_name, pid, start_time, sample_time
            """,
            (user_name, exe_path, since_iso, until_iso),
        )

    last_by_sess: Dict[Tuple[str, int, str], Dict[str, float]] = {}
    for r in cur.fetchall():
        try:
            m = str(r["machine_name"])
            pid = int(r["pid"] or 0)
            st = str(r["start_time"])
            sk = (m, pid, st)
            prev = last_by_sess.get(sk)

            cpu_u = r["cpu_user_time_s"]
            cpu_s = r["cpu_system_time_s"]
            io_r = r["io_read_bytes"]
            io_w = r["io_write_bytes"]

            cpu_tot = None
            io_tot = None
            if cpu_u is not None or cpu_s is not None:
                cpu_tot = float(cpu_u or 0.0) + float(cpu_s or 0.0)
            if io_r is not None or io_w is not None:
                io_tot = float(io_r or 0.0) + float(io_w or 0.0)

            if prev:
                if metric == "cpu_delta" and cpu_tot is not None and "cpu_tot" in prev:
                    vals.append(float(max(0.0, cpu_tot - prev["cpu_tot"])))
                if metric == "io_delta" and io_tot is not None and "io_tot" in prev:
                    vals.append(float(max(0.0, io_tot - prev["io_tot"])))

            nxt: Dict[str, float] = {}
            if cpu_tot is not None:
                nxt["cpu_tot"] = float(cpu_tot)
            if io_tot is not None:
                nxt["io_tot"] = float(io_tot)
            if nxt:
                last_by_sess[sk] = nxt
        except Exception:
            continue

    return vals

def _severity_from_ratio(ratio: Optional[float]) -> str:
    if ratio is None:
        return "med"
    if ratio > 10:
        return "high"
    if ratio >= 5:
        return "med"
    return "low"


def _raise_severity(current: str, target: str) -> str:
    order = {"low": 0, "med": 1, "high": 2}
    return target if order.get(target, 1) > order.get(current, 1) else current


def _binary_key(sha256: Optional[str], exe_path: Optional[str], process_name: str) -> str:
    s = (sha256 or "").strip()
    if s:
        return s
    p = (exe_path or "").strip().lower()
    if p:
        return f"path:{p}"
    return f"name:{(process_name or 'unknown').strip().lower()}"


def _make_alert_dedup_key(alert: Dict[str, Any]) -> str:
    if alert["entity_type"] == "process_chain":
        return f"{alert['machine_name']}|{alert['user_name']}|{alert.get('chain_key') or ''}|{alert['metric']}|{alert['bucket_hour']}"
    return f"{alert['machine_name']}|{alert['user_name']}|{_binary_key(alert.get('sha256'), alert.get('exe_path'), alert.get('process_name') or '')}|{alert['metric']}|{alert['bucket_hour']}"


def _try_insert_alert(cur: sqlite3.Cursor, alert: Dict[str, Any]) -> bool:
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO alerts(
                created_at, sample_time,
                machine_name, user_name,
                entity_type,
                process_name, pid, start_time,
                sha256, exe_path,
                parent_process_name, parent_sha256, parent_exe_path, chain_key,
                metric, value, baseline, score, severity, reason,
                status, ack_by, ack_at, closed_at,
                bucket_hour, dedup_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                alert["created_at"],
                alert["sample_time"],
                alert["machine_name"],
                alert["user_name"],
                alert["entity_type"],
                alert["process_name"],
                alert.get("pid"),
                alert.get("start_time"),
                alert.get("sha256"),
                alert.get("exe_path"),
                alert.get("parent_process_name"),
                alert.get("parent_sha256"),
                alert.get("parent_exe_path"),
                alert.get("chain_key"),
                alert["metric"],
                alert.get("value"),
                alert.get("baseline"),
                alert.get("score"),
                alert["severity"],
                alert["reason"],
                alert.get("status", "new"),
                alert.get("ack_by"),
                alert.get("ack_at"),
                alert.get("closed_at"),
                alert["bucket_hour"],
                alert["dedup_key"],
            ),
        )
        return cur.rowcount > 0
    except Exception:
        return False


def _load_parent_context(cur: sqlite3.Cursor, latest: sqlite3.Row) -> Optional[Dict[str, Any]]:
    machine = str(latest["machine_name"] or "")
    ppid = latest["ppid"]
    if ppid is None:
        return None

    child_start = _safe_parse_iso(str(latest["start_time"]))
    child_sample = _safe_parse_iso(str(latest["sample_time"]))
    if not child_sample:
        return None

    cur.execute(
        """
        SELECT *
        FROM events
        WHERE machine_name=? AND pid=?
          AND sample_time<=?
        ORDER BY sample_time DESC
        LIMIT 20
        """,
        (machine, int(ppid), str(latest["sample_time"])),
    )

    candidates = cur.fetchall()
    if not candidates:
        return None

    parent = None
    for cand in candidates:
        cand_start = _safe_parse_iso(str(cand["start_time"]))
        cand_end = _safe_parse_iso(str(cand["end_time"])) if cand["end_time"] else None

        if child_start and cand_start and cand_start > child_start:
            continue
        if child_start and cand_end and cand_end < child_start:
            continue

        parent = cand
        break

    if not parent:
        parent = candidates[0]

    parent_key = _binary_key(parent["sha256"], parent["exe_path"], str(parent["process_name"] or ""))
    child_key = _binary_key(latest["sha256"], latest["exe_path"], str(latest["process_name"] or ""))

    return {
        "parent_process_name": str(parent["process_name"] or ""),
        "parent_sha256": str(parent["sha256"] or "").strip() or None,
        "parent_exe_path": str(parent["exe_path"] or "").strip() or None,
        "chain_key": f"{parent_key} -> {child_key}",
        "parent_key": parent_key,
        "child_key": child_key,
    }

def _count_prior_binary_sessions(
    cur: sqlite3.Cursor,
    machine: str,
    user_name: str,
    pid: int,
    start_time: str,
    sample_time: str,
    sha256: str,
    exe_path: str,
    process_name: str,
) -> int:
    params: List[Any] = [machine, sample_time, machine, int(pid), start_time]

    if sha256:
        match_sql = "NULLIF(sha256, '') IS NOT NULL AND sha256=?"
        params.append(sha256)
    elif exe_path:
        match_sql = "lower(COALESCE(exe_path, ''))=?"
        params.append(exe_path.lower())
    else:
        match_sql = "lower(process_name)=?"
        params.append(process_name.lower())

    user_sql = ""
    if user_name:
        user_sql = " AND COALESCE(user_name, '')=?"
        params.append(user_name)

    cur.execute(
        f"""
        SELECT COUNT(DISTINCT machine_name || '|' || COALESCE(pid, '') || '|' || start_time) AS c
        FROM events
        WHERE machine_name=?
          AND sample_time < ?
          AND NOT (machine_name=? AND COALESCE(pid, 0)=? AND start_time=?)
          AND {match_sql}
          {user_sql}
        """,
        params,
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _process_in_catalog(cur: sqlite3.Cursor, process_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM process_catalog WHERE lower(process_name)=lower(?) LIMIT 1;",
        (process_name.strip(),),
    )
    return cur.fetchone() is not None


def _count_process_sessions_on_machine(
    cur: sqlite3.Cursor,
    machine: str,
    pid: int,
    start_time: str,
    sample_time: str,
    process_name: str,
) -> int:
    cur.execute(
        """
        SELECT COUNT(DISTINCT machine_name || '|' || COALESCE(pid, '') || '|' || start_time) AS c
        FROM events
        WHERE machine_name=?
          AND sample_time < ?
          AND lower(process_name)=lower(?)
          AND NOT (machine_name=? AND COALESCE(pid, 0)=? AND start_time=?)
        """,
        (machine, sample_time, process_name, machine, int(pid), start_time),
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _count_prior_chain_sessions(
    cur: sqlite3.Cursor,
    machine: str,
    user_name: str,
    pid: int,
    start_time: str,
    sample_time: str,
    parent_key: str,
    child_key: str,
) -> int:
    params: List[Any] = [machine, sample_time, machine, int(pid), start_time]

    user_sql = ""
    if user_name:
        user_sql = " AND COALESCE(c.user_name, '')=?"
        params.append(user_name)

    cur.execute(
        f"""
        WITH child_sessions AS (
          SELECT
              machine_name,
              COALESCE(user_name, '') AS user_name,
              pid,
              ppid,
              start_time,
              sample_time,
              end_time,
              COALESCE(NULLIF(sha256, ''), 'path:' || lower(COALESCE(exe_path, '')), 'name:' || lower(process_name)) AS child_key
          FROM events
          WHERE machine_name=?
            AND sample_time < ?
            AND NOT (machine_name=? AND COALESCE(pid, 0)=? AND start_time=?)
          GROUP BY machine_name, pid, start_time
        ),
        parent_candidates AS (
          SELECT
              machine_name,
              pid,
              start_time,
              end_time,
              sample_time,
              COALESCE(NULLIF(sha256, ''), 'path:' || lower(COALESCE(exe_path, '')), 'name:' || lower(process_name)) AS parent_key
          FROM events
        )
        SELECT COUNT(DISTINCT c.machine_name || '|' || COALESCE(c.pid, '') || '|' || c.start_time) AS c
        FROM child_sessions c
        JOIN parent_candidates p
          ON p.machine_name = c.machine_name
         AND p.pid = c.ppid
         AND p.start_time <= c.start_time
         AND (p.end_time IS NULL OR p.end_time = '' OR p.end_time >= c.start_time)
        WHERE c.child_key=?
          AND p.parent_key=?
          {user_sql}
        """,
        params + [child_key, parent_key],
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _chain_in_catalog(cur: sqlite3.Cursor, chain_key: str) -> bool:
    cur.execute("SELECT 1 FROM chain_catalog WHERE chain_key=? LIMIT 1;", (chain_key,))
    return cur.fetchone() is not None

def _get_machine_role_profile(cur: sqlite3.Cursor, machine_name: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT r.role_id, r.role_name
        FROM machine_roles mr
        JOIN roles r ON r.role_id = mr.role_id
        WHERE mr.machine_name = ?
        LIMIT 1
        """,
        (machine_name,),
    )
    row = cur.fetchone()
    if not row:
        return None

    item = dict(row)
    allowed_process_types = _get_role_allowed_process_types(cur, int(item["role_id"]))
    item["allowed_process_types"] = allowed_process_types
    item["allowed_types_text"] = ", ".join(allowed_process_types)
    return item

def _get_process_type(cur: sqlite3.Cursor, process_name: str) -> str:
    cur.execute(
        """
        SELECT process_type
        FROM process_catalog
        WHERE lower(process_name) = lower(?)
        LIMIT 1
        """,
        (process_name.strip(),),
    )
    row = cur.fetchone()
    return str(row["process_type"] or "").strip() if row else ""

def _normalize_allowed_process_types(raw: Any) -> List[str]:
    if raw is None:
        return []

    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).split(",")

    out: List[str] = []
    seen = set()

    for x in items:
        s = str(x or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _get_role_allowed_process_types(cur: sqlite3.Cursor, role_id: int) -> List[str]:
    cur.execute(
        """
        SELECT process_type
        FROM role_allowed_process_types
        WHERE role_id = ?
        ORDER BY process_type COLLATE NOCASE
        """,
        (int(role_id),),
    )
    rows = cur.fetchall()
    return [str(row["process_type"]).strip() for row in rows if str(row["process_type"] or "").strip()]


def _replace_role_allowed_process_types(cur: sqlite3.Cursor, role_id: int, raw: Any) -> List[str]:
    allowed_process_types = _normalize_allowed_process_types(raw)
    cur.execute("DELETE FROM role_allowed_process_types WHERE role_id=?;", (int(role_id),))

    if not allowed_process_types:
        return []

    now = datetime.now().isoformat(timespec="seconds")
    cur.executemany(
        """
        INSERT INTO role_allowed_process_types(role_id, process_type, updated_at)
        VALUES(?, ?, ?)
        """,
        [(int(role_id), process_type, now) for process_type in allowed_process_types],
    )
    return allowed_process_types

def _typical_hours_for_process(
    cur: sqlite3.Cursor,
    machine: str,
    user_name: str,
    pid: int,
    start_time: str,
    sample_time: str,
    sha256: str,
    exe_path: str,
    process_name: str,
) -> Tuple[set, int]:
    min_samples = int(_cfg_get("time_hist_min_samples", TIME_HIST_MIN_SAMPLES_DEFAULT))
    params: List[Any] = [machine, sample_time, machine, int(pid), start_time]

    if sha256:
        match_sql = "NULLIF(sha256, '') IS NOT NULL AND sha256=?"
        params.append(sha256)
    elif exe_path:
        match_sql = "lower(COALESCE(exe_path, ''))=?"
        params.append(exe_path.lower())
    else:
        match_sql = "lower(process_name)=?"
        params.append(process_name.lower())

    user_sql = ""
    if user_name:
        user_sql = " AND COALESCE(user_name, '')=?"
        params.append(user_name)

    cur.execute(
        f"""
        WITH sessions AS (
          SELECT
              start_time,
              machine_name || '|' || COALESCE(pid, '') || '|' || start_time AS session_key
          FROM events
          WHERE machine_name=?
            AND sample_time < ?
            AND NOT (machine_name=? AND COALESCE(pid, 0)=? AND start_time=?)
            AND {match_sql}
            {user_sql}
          GROUP BY machine_name, pid, start_time
        )
        SELECT start_time
        FROM sessions
        """,
        params,
    )

    hist: Dict[int, int] = {}
    total = 0
    for r in cur.fetchall():
        hh = _local_hour_from_iso(r["start_time"])
        if hh is None:
            continue
        hist[hh] = hist.get(hh, 0) + 1
        total += 1

    if total < min_samples or not hist:
        return set(), total

    peak = max(hist.values())
    thr = max(2, int((peak * 0.5) + 0.9999))
    typical = {hh for hh, cnt in hist.items() if cnt >= thr and cnt >= 2}
    return typical, total


def detect_alerts_for_ingested_events(events_payload: List[Dict[str, Any]]) -> int:
    if not events_payload:
        return 0

    newest: Dict[Tuple[str, int, str], str] = {}
    for e in events_payload:
        try:
            key = (
                str(e.get("machine_name") or "").strip(),
                int(e.get("pid") or 0),
                str(e.get("start_time") or "").strip(),
            )
            sm = str(e.get("sample_time") or "").strip()
            if not (key[0] and key[1] and key[2] and sm):
                continue
            prev = newest.get(key)
            if (not prev) or sm > prev:
                newest[key] = sm
        except Exception:
            continue

    if not newest:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    days = int(_cfg_get("alert_baseline_window_days", ALERT_BASELINE_WINDOW_DAYS_DEFAULT))
    nmin = int(_cfg_get("alert_baseline_min_points", ALERT_BASELINE_MIN_POINTS_DEFAULT))
    rare_thr = int(_cfg_get("rare_count_threshold", RARE_COUNT_THRESHOLD_DEFAULT))

    conn = db_connect()
    cur = conn.cursor()
    inserted = 0

    for (machine, pid, start_time), current_sample in newest.items():
        latest, prev = _get_latest_and_prev_for_session(cur, machine, pid, start_time, current_sample)
        if not latest:
            continue

        user_name = str(latest["user_name"] or "").strip()
        process_name = str(latest["process_name"] or "").strip() or "unknown"
        exe_path = str(latest["exe_path"] or "").strip()
        sha256 = str(latest["sha256"] or "").strip()

        until_iso = str(latest["sample_time"])
        since_dt = _safe_parse_iso(until_iso)
        since_iso = (since_dt - timedelta(days=days)).isoformat(timespec="seconds") if since_dt else _window_bounds(days)[0]

        bucket_hour = _bucket_hour_from_sample(until_iso)
        session_alerts: List[Dict[str, Any]] = []

        cpu_delta_val = None
        io_delta_val = None
        if prev:
            try:
                cpu_delta_val = max(
                    0.0,
                    (float(latest["cpu_user_time_s"] or 0.0) + float(latest["cpu_system_time_s"] or 0.0)) -
                    (float(prev["cpu_user_time_s"] or 0.0) + float(prev["cpu_system_time_s"] or 0.0))
                )
            except Exception:
                cpu_delta_val = None
            try:
                io_delta_val = max(
                    0.0,
                    (float(latest["io_read_bytes"] or 0.0) + float(latest["io_write_bytes"] or 0.0)) -
                    (float(prev["io_read_bytes"] or 0.0) + float(prev["io_write_bytes"] or 0.0))
                )
            except Exception:
                io_delta_val = None

        metrics_now: Dict[str, Optional[float]] = {
            "cpu_delta": cpu_delta_val,
            "io_delta": io_delta_val,
            "rss": (float(latest["rss_bytes"]) if latest["rss_bytes"] is not None else None),
            "net_conn_count": (float(latest["net_conn_count"]) if latest["net_conn_count"] is not None and latest["net_active"] not in (0, False) else None),
        }

        for metric, value_now in metrics_now.items():
            if value_now is None:
                continue

            rule = ALERT_RULES.get(metric)
            if not rule:
                continue

            baseline_values = _collect_baseline_values(
                cur=cur,
                metric=metric,
                user_name=user_name,
                sha256=sha256,
                exe_path=exe_path,
                since_iso=since_iso,
                until_iso=until_iso,
            )
            baseline = _median(baseline_values)
            ratio = (float(value_now) / float(baseline)) if (baseline is not None and baseline > 0) else None

            fired = False
            if baseline is not None and len(baseline_values) >= nmin:
                fired = value_now > float(baseline) * float(rule["K"]) and value_now > float(rule["abs"])
            else:
                fired = value_now > float(rule["very_high_abs"])

            if not fired:
                continue

            reason = (
                f"Ресурсная аномалия: {metric}, значение={value_now:.4g}, "
                f"baseline={(baseline if baseline is not None else 'null')}, "
                f"ratio={(ratio if ratio is not None else 'null')}."
            )

            session_alerts.append({
                "created_at": now_iso,
                "sample_time": until_iso,
                "machine_name": machine,
                "user_name": user_name,
                "entity_type": "process_session",
                "process_name": process_name,
                "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                "sha256": sha256 or None,
                "exe_path": exe_path or None,
                "parent_process_name": None,
                "parent_sha256": None,
                "parent_exe_path": None,
                "chain_key": None,
                "metric": metric,
                "value": float(value_now),
                "baseline": (float(baseline) if baseline is not None else None),
                "score": (float(ratio) if ratio is not None else None),
                "severity": _severity_from_ratio(ratio),
                "reason": reason,
                "status": "new",
                "bucket_hour": bucket_hour,
            })

        process_known = _process_in_catalog(cur, process_name)
        prior_process_sessions = _count_process_sessions_on_machine(
            cur, machine, pid, start_time, until_iso, process_name
        )
        total_process_sessions = prior_process_sessions + 1

        if (not process_known) and total_process_sessions <= rare_thr:
            if total_process_sessions == 1:
                reason = "Процесс ранее не наблюдался на данной машине (отсутствует в справочнике)."
                sev = "med"
            else:
                reason = f"Процесс отсутствует в справочнике; число наблюдений на данной машине: {total_process_sessions}, что ниже порога {rare_thr}."
                sev = "low"

            session_alerts.append({
                "created_at": now_iso,
                "sample_time": until_iso,
                "machine_name": machine,
                "user_name": user_name,
                "entity_type": "process_session",
                "process_name": process_name,
                "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                "sha256": sha256 or None,
                "exe_path": exe_path or None,
                "parent_process_name": None,
                "parent_sha256": None,
                "parent_exe_path": None,
                "chain_key": None,
                "metric": "rarity",
                "value": float(total_process_sessions),
                "baseline": float(rare_thr),
                "score": None,
                "severity": sev,
                "reason": reason,
                "status": "new",
                "bucket_hour": bucket_hour,
            })

        parent_ctx = _load_parent_context(cur, latest)
        if parent_ctx:
            chain_key = str(parent_ctx["chain_key"] or "")
            chain_known = _chain_in_catalog(cur, chain_key)
            # if known chain from catalog -> do not create chain_rarity alert
            if not chain_known:
                prior_chain_sessions = _count_prior_chain_sessions(
                    cur, machine, user_name, pid, start_time, until_iso, parent_ctx["parent_key"],
                    parent_ctx["child_key"]
                )

                if prior_chain_sessions <= rare_thr:
                    if prior_chain_sessions == 0:
                        reason = "Цепочка не описана в справочнике и ранее не наблюдалась на данной машине."
                        sev = "med"
                    else:
                        reason = f"Цепочка не описана в справочнике; ранее наблюдалась ограниченно ({prior_chain_sessions} раз(а)) на данной машине."
                        sev = "low"

                    session_alerts.append({
                        "created_at": now_iso,
                        "sample_time": until_iso,
                        "machine_name": machine,
                        "user_name": user_name,
                        "entity_type": "process_chain",
                        "process_name": process_name,
                        "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                        "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                        "sha256": sha256 or None,
                        "exe_path": exe_path or None,
                        "parent_process_name": parent_ctx["parent_process_name"],
                        "parent_sha256": parent_ctx["parent_sha256"],
                        "parent_exe_path": parent_ctx["parent_exe_path"],
                        "chain_key": chain_key,
                        "metric": "chain_rarity",
                        "value": float(prior_chain_sessions),
                        "baseline": float(rare_thr),
                        "score": None,
                        "severity": sev,
                        "reason": reason,
                        "status": "new",
                        "bucket_hour": bucket_hour,
                    })

        role_profile = _get_machine_role_profile(cur, machine)
        if role_profile:
            allowed_types = role_profile.get("allowed_process_types") or []
            process_type = _get_process_type(cur, process_name)

            if allowed_types and process_type:
                allowed_norm = {x.strip().lower() for x in allowed_types if str(x).strip()}
                proc_type_norm = process_type.strip().lower()

                if proc_type_norm not in allowed_norm:
                    session_alerts.append({
                        "created_at": now_iso,
                        "sample_time": until_iso,
                        "machine_name": machine,
                        "user_name": user_name,
                        "entity_type": "process_session",
                        "process_name": process_name,
                        "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                        "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                        "sha256": sha256 or None,
                        "exe_path": exe_path or None,
                        "parent_process_name": None,
                        "parent_sha256": None,
                        "parent_exe_path": None,
                        "chain_key": None,
                        "metric": "role_type_mismatch",
                        "value": None,
                        "baseline": None,
                        "score": None,
                        "severity": "med",
                        "reason": f"Тип процесса {process_type} не разрешён для роли {role_profile.get('role_name') or 'unknown'}.",
                        "status": "new",
                        "bucket_hour": bucket_hour,
                    })

        typical_hours, hist_size = _typical_hours_for_process(
            cur, machine, user_name, pid, start_time, until_iso, sha256, exe_path, process_name
        )

        start_hour_local = _local_hour_from_iso(start_time)
        if start_hour_local is not None and typical_hours and hist_size >= int(_cfg_get("time_hist_min_samples", TIME_HIST_MIN_SAMPLES_DEFAULT)):
            if start_hour_local not in typical_hours:
                hh = ", ".join(f"{x:02d}" for x in sorted(typical_hours))
                session_alerts.append({
                    "created_at": now_iso,
                    "sample_time": until_iso,
                    "machine_name": machine,
                    "user_name": user_name,
                    "entity_type": "process_session",
                    "process_name": process_name,
                    "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                    "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                    "sha256": sha256 or None,
                    "exe_path": exe_path or None,
                    "parent_process_name": None,
                    "parent_sha256": None,
                    "parent_exe_path": None,
                    "chain_key": None,
                    "metric": "time_anomaly",
                    "value": float(start_hour_local),
                    "baseline": None,
                    "score": None,
                    "severity": "med",
                    "reason": f"Запуск произошёл в нетипичный час {start_hour_local:02d}:00. Типичные часы запуска: {hh}.",
                    "status": "new",
                    "bucket_hour": bucket_hour,
                })

        metrics = {a["metric"] for a in session_alerts}
        if "rarity" in metrics and "chain_rarity" in metrics:
            for a in session_alerts:
                if a["metric"] in ("rarity", "chain_rarity"):
                    a["severity"] = _raise_severity(a["severity"], "high")
                    a["reason"] = "[combined] Редкий процесс + нетипичная цепочка. " + a["reason"]

        for alert_row in session_alerts:
            alert_row["dedup_key"] = _make_alert_dedup_key(alert_row)
            if _try_insert_alert(cur, alert_row):
                inserted += 1

    conn.commit()
    conn.close()
    return inserted

def _window_bounds(days: int) -> Tuple[str, str]:
    now = datetime.now()
    since = now - timedelta(days=days)
    return since.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")


@app.get("/api/analytics/rare")
def analytics_rare(x_api_key: Optional[str] = Header(default=None), days: int = 14, limit: int = 30):
    require_api_key(x_api_key)
    days = int(days)
    limit = int(limit)
    since, _ = _window_bounds(days)

    conn = db_connect()
    cur = conn.cursor()

    # Rare binaries counted by unique sessions (not samples), with machine_names list.
    cur.execute(
        """
        SELECT COALESCE(NULLIF(sha256, ''), 'path:' || lower(COALESCE(exe_path, '')))        AS bin_key,
               MIN(process_name)                                                             AS process_name,

               COUNT(DISTINCT machine_name || '|' || COALESCE(pid, '') || '|' || start_time) AS sessions,
               COUNT(DISTINCT machine_name)                                                  AS machines,
               COUNT(DISTINCT COALESCE(user_name, ''))                                       AS users,
               GROUP_CONCAT(DISTINCT machine_name)                                           AS machine_names

        FROM events
        WHERE sample_time >= ?
        GROUP BY bin_key
        ORDER BY sessions ASC, machines ASC, users ASC LIMIT ?
        """,
        (since, limit),
    )

    bins = [dict(r) for r in cur.fetchall()]




    # Rare parent -> child chains counted by unique CHILD sessions (not samples).
    # Also return machine_names list for UI.
    cur.execute(
        """
        WITH c AS (
          SELECT
            machine_name, sample_time,
            pid, ppid, start_time,
            COALESCE(NULLIF(sha256,''), 'path:' || lower(COALESCE(exe_path,''))) AS child_key,
            lower(process_name) AS child_name,
            COALESCE(user_name,'') AS user_name
          FROM events
          WHERE sample_time >= ? AND ppid IS NOT NULL
        ),
        p AS (
          SELECT
            machine_name, sample_time, pid,
            COALESCE(NULLIF(sha256,''), 'path:' || lower(COALESCE(exe_path,''))) AS parent_key,
            lower(process_name) AS parent_name
          FROM events
          WHERE sample_time >= ?
        )
        SELECT
          (p.parent_key || ' -> ' || c.child_key) AS chain_key,
          p.parent_key AS parent_key,
          c.child_key AS child_key,
          MIN(p.parent_name) AS parent_name,
          MIN(c.child_name) AS child_name,

          COUNT(DISTINCT c.machine_name || '|' || c.pid || '|' || c.start_time) AS sessions,
          COUNT(DISTINCT c.machine_name) AS machines,
          COUNT(DISTINCT c.user_name) AS users,
          GROUP_CONCAT(DISTINCT c.machine_name) AS machine_names,
          CASE WHEN MIN(p.parent_name)=MIN(c.child_name) THEN 1 ELSE 0 END AS self_chain


        FROM c
        JOIN p
          ON p.machine_name = c.machine_name
         AND p.sample_time  = c.sample_time
         AND p.pid          = c.ppid
        GROUP BY chain_key
        ORDER BY sessions ASC, machines ASC
        LIMIT ?
        """,
        (since, since, limit),
    )
    chains = [dict(r) for r in cur.fetchall()]


    conn.close()

    # thresholds (flag "rare" in response)
    rare_count_thr = int(_cfg_get("rare_count_threshold", RARE_COUNT_THRESHOLD_DEFAULT))
    rare_machine_thr = int(_cfg_get("rare_machine_threshold", RARE_MACHINE_THRESHOLD_DEFAULT))

    for b in bins:
        b["rare"] = bool(
            int(b.get("sessions") or 0) <= rare_count_thr or int(b.get("machines") or 0) <= rare_machine_thr)
    for c in chains:
        c["rare"] = bool(
            int(c.get("sessions") or 0) <= rare_count_thr or int(c.get("machines") or 0) <= rare_machine_thr)

    def _looks_like_sha(val: object) -> bool:
        if not val:
            return False
        ss = str(val).strip().lower()
        if len(ss) != 64:
            return False
        return all(c in "0123456789abcdef" for c in ss)

    return {
        "window_days": days,

        "rare_binaries": [
            {
                "pkey": b.get("bin_key"),
                "process_name": b.get("process_name"),
                "sha256": (b.get("bin_key") if _looks_like_sha(b.get("bin_key")) else ""),
                "machines": int(b.get("machines") or 0),
                "rows": int(b.get("sessions") or 0),  # backward field name (now = sessions)
                "sessions": int(b.get("sessions") or 0),
                "users": int(b.get("users") or 0),
                "machine_names": (b.get("machine_names") or ""),
            }
            for b in bins
        ],
        "rare_chains": [
            {
                "pkey": c.get("chain_key"),
                "parent_process": c.get("parent_name"),
                "child_process": c.get("child_name"),
                "parent_key": c.get("parent_key"),
                "child_key": c.get("child_key"),
                "parent_sha256": (c.get("parent_key") if _looks_like_sha(c.get("parent_key")) else ""),
                "child_sha256": (c.get("child_key") if _looks_like_sha(c.get("child_key")) else ""),
                "machines": int(c.get("machines") or 0),
                "users": int(c.get("users") or 0),
                "self_chain": bool(int(c.get("self_chain") or 0)),
                "rows": int(c.get("sessions") or 0),  # backward field name (now = sessions)
                "sessions": int(c.get("sessions") or 0),
                "machine_names": (c.get("machine_names") or ""),

            }
            for c in chains
        ],

    }


@app.get("/api/analytics/host-profile")
def analytics_host_profile(machine_name: str, x_api_key: Optional[str] = Header(default=None), days: int = 14, limit: int = 100):
    require_api_key(x_api_key)
    machine = machine_name.strip()
    if not machine:
        raise HTTPException(status_code=400, detail="machine_name required")
    days = int(days)
    limit = int(limit)
    since, _ = _window_bounds(days)

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        WITH base AS (SELECT e.process_name,
                             e.pid,
                             e.ppid,
                             e.start_time,
                             e.sample_time,
                             e.duration_seconds,
                             EXISTS(SELECT 1
                                    FROM events c
                                    WHERE c.machine_name = e.machine_name
                                      AND c.sample_time = e.sample_time
                                      AND lower(c.process_name) = lower(e.process_name)
                                      AND c.ppid = e.pid) AS has_children
                      FROM events e
                      WHERE e.machine_name = ?
                        AND e.sample_time >= ?),
             mains AS (SELECT *,
                              ROW_NUMBER() OVER (
                   PARTITION BY lower(process_name), sample_time
                   ORDER BY has_children DESC, start_time ASC, duration_seconds DESC, pid ASC
                 ) AS rn
                       FROM base),
             s AS (SELECT process_name,
                          pid,
                          start_time,
                          MIN(sample_time)      AS first_seen,
                          MAX(sample_time)      AS last_seen,
                          MAX(duration_seconds) AS max_dur,
            date (MIN (sample_time)) AS day
        FROM mains
        WHERE rn = 1
        GROUP BY process_name, pid, start_time
            )
        SELECT process_name,
               COUNT(*)            AS runs,
               COUNT(DISTINCT day) AS seen_days,
               AVG(max_dur)        AS avg_duration_s
        FROM s
        GROUP BY process_name
        ORDER BY runs DESC LIMIT ?
        """,
        (machine, since, limit),
    )

    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"machine_name": machine, "window_days": days, "items": items}

@app.get("/api/analytics/process-names")
def analytics_process_names(
    x_api_key: Optional[str] = Header(default=None),
    q: str = "",
    limit: int = 100,
):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    needle = q.strip().lower()

    cur.execute(
        """
        SELECT MIN(process_name) AS process_name, lower(process_name) AS pname, COUNT(*) AS rows_count
        FROM events
        WHERE TRIM(COALESCE(process_name, '')) <> ''
          AND (? = '' OR lower(process_name) LIKE '%' || ? || '%')
        GROUP BY lower(process_name)
        ORDER BY rows_count DESC, pname ASC
        LIMIT ?
        """,
        (needle, needle, max(1, min(int(limit), 500))),
    )
    items = [{"process_name": r["process_name"]} for r in cur.fetchall()]
    conn.close()
    return {"items": items}


@app.get("/api/analytics/process-profile")
def analytics_process_profile(
    process_name: str,
    x_api_key: Optional[str] = Header(default=None),
    days: int = 14,
):
    require_api_key(x_api_key)
    pname = process_name.strip()
    if not pname:
        raise HTTPException(status_code=400, detail="process_name required")

    since, _ = _window_bounds(int(days))

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            machine_name,
            COALESCE(user_name, '') AS user_name,
            pid,
            start_time,
            sample_time,
            rss_bytes,
            cpu_user_time_s,
            cpu_system_time_s,
            io_read_bytes,
            io_write_bytes,
            net_active,
            net_conn_count
        FROM events
        WHERE lower(process_name) = lower(?)
          AND sample_time >= ?
        ORDER BY machine_name, user_name, pid, start_time, sample_time
        """,
        (pname, since),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    sessions: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    for r in rows:
        sk = (str(r["machine_name"]), str(r["user_name"]), int(r["pid"] or 0), str(r["start_time"]))
        cur_s = sessions.setdefault(sk, {
            "machine_name": str(r["machine_name"]),
            "user_name": str(r["user_name"]),
            "start_time": str(r["start_time"]),
            "day": (_safe_parse_iso(r["start_time"]).astimezone().date().isoformat() if _safe_parse_iso(r["start_time"]) else str(r["start_time"])[:10]),
            "hour": _local_hour_from_iso(r["start_time"]),
            "rss_values": [],
            "cpu_prev": None,
            "io_prev": None,
            "cpu_deltas": [],
            "io_deltas": [],
            "net_seen": False,
        })

        if r["rss_bytes"] is not None:
            cur_s["rss_values"].append(float(r["rss_bytes"]))

        cpu_tot = None
        if r["cpu_user_time_s"] is not None or r["cpu_system_time_s"] is not None:
            cpu_tot = float(r["cpu_user_time_s"] or 0.0) + float(r["cpu_system_time_s"] or 0.0)
            if cur_s["cpu_prev"] is not None:
                cur_s["cpu_deltas"].append(max(0.0, cpu_tot - cur_s["cpu_prev"]))
            cur_s["cpu_prev"] = cpu_tot

        io_tot = None
        if r["io_read_bytes"] is not None or r["io_write_bytes"] is not None:
            io_tot = float(r["io_read_bytes"] or 0.0) + float(r["io_write_bytes"] or 0.0)
            if cur_s["io_prev"] is not None:
                cur_s["io_deltas"].append(max(0.0, io_tot - cur_s["io_prev"]))
            cur_s["io_prev"] = io_tot

        if r["net_active"] in (1, True) or (r["net_conn_count"] is not None and int(r["net_conn_count"]) > 0):
            cur_s["net_seen"] = True

    by_pair: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for s in sessions.values():
        pk = (s["machine_name"], s["user_name"])
        item = by_pair.setdefault(pk, {
            "machine_name": s["machine_name"],
            "user_name": s["user_name"],
            "runs": 0,
            "days": set(),
            "hours": {},
            "rss": [],
            "cpu_delta": [],
            "io_delta": [],
            "net_sessions": 0,
        })
        item["runs"] += 1
        item["days"].add(s["day"])
        if s["hour"] is not None:
            item["hours"][s["hour"]] = item["hours"].get(s["hour"], 0) + 1
        if s["rss_values"]:
            item["rss"].append(max(s["rss_values"]))
        if s["cpu_deltas"]:
            item["cpu_delta"].append(max(s["cpu_deltas"]))
        if s["io_deltas"]:
            item["io_delta"].append(max(s["io_deltas"]))
        if s["net_seen"]:
            item["net_sessions"] += 1

    items = []
    for item in by_pair.values():
        if item["hours"]:
            peak = max(item["hours"].values())
            hour_thr = max(2, int((peak * 0.5) + 0.9999))
            typical_hours = sorted([hh for hh, cnt in item["hours"].items() if cnt >= hour_thr and cnt >= 2])
        else:
            typical_hours = []

        items.append({
            "machine_name": item["machine_name"],
            "user_name": item["user_name"],
            "runs": item["runs"],
            "seen_days": len(item["days"]),
            "typical_hours": ", ".join(f"{h:02d}" for h in typical_hours),
            "median_rss": _median(item["rss"]),
            "median_cpu_delta": _median(item["cpu_delta"]),
            "median_io_delta": _median(item["io_delta"]),
            "net_sessions": item["net_sessions"],
        })

    items.sort(key=lambda x: (-int(x["runs"]), str(x["machine_name"]).lower(), str(x["user_name"]).lower()))
    return {"process_name": pname, "window_days": int(days), "items": items}


@app.get("/api/analytics/time-anomalies")
def analytics_time_anomalies(x_api_key: Optional[str] = Header(default=None), days: int = 14, limit: int = 50):
    require_api_key(x_api_key)
    days = int(days)
    limit = int(limit)
    since, _ = _window_bounds(days)

    conn = db_connect()
    cur = conn.cursor()

    min_samples = int(_cfg_get("time_hist_min_samples", TIME_HIST_MIN_SAMPLES_DEFAULT))

    cur.execute(
        """
        WITH latest AS (
          SELECT machine_name, pid, start_time, MAX(sample_time) AS max_sample
          FROM events
          WHERE sample_time >= ?
          GROUP BY machine_name, pid, start_time
        )
        SELECT e.*
        FROM events e
        JOIN latest l
          ON e.machine_name=l.machine_name AND e.pid IS l.pid AND e.start_time=l.start_time AND e.sample_time=l.max_sample
        ORDER BY e.start_time DESC
        LIMIT 1000
        """,
        (since,),
    )
    rows = cur.fetchall()
    conn.close()

    hist: Dict[str, Dict[int, int]] = {}
    total: Dict[str, int] = {}

    for r in rows:
        pname = (r["process_name"] or "").lower()
        hh = _local_hour_from_iso(r["start_time"])
        if not pname or hh is None:
            continue
        hist.setdefault(pname, {})[hh] = hist.setdefault(pname, {}).get(hh, 0) + 1
        total[pname] = total.get(pname, 0) + 1

    typical: Dict[str, set] = {}
    for pname, buckets in hist.items():
        tot = total.get(pname, 0)
        if tot < min_samples:
            continue
        peak = max(buckets.values()) if buckets else 0
        thr = max(2, int((peak * 0.5) + 0.9999))
        typical[pname] = {h for h, c in buckets.items() if c >= thr and c >= 2}

    out = []
    for r in rows:
        pname = (r["process_name"] or "").lower()
        start_hour_local = _local_hour_from_iso(r["start_time"])
        if start_hour_local is None:
            continue
        if pname in typical and typical[pname] and start_hour_local not in typical[pname]:
            d = dict(r)
            d["time_anomaly"] = True
            d["local_start_hour"] = start_hour_local
            d["typical_hours_local"] = sorted(list(typical[pname]))
            d["pkey"] = _binary_key(d.get("sha256"), d.get("exe_path"), d.get("process_name") or "")
            out.append(d)
        if len(out) >= limit:
            break

    return {"window_days": days, "items": out}

@app.get("/api/analytics/chain-keys")
def analytics_chain_keys(
    x_api_key: Optional[str] = Header(default=None),
    days: int = 14,
    limit: int = 200,
):
    require_api_key(x_api_key)
    since, _ = _window_bounds(int(days))

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COALESCE(NULLIF(p.sha256, ''), 'path:' || lower(COALESCE(p.exe_path, '')), 'name:' || lower(p.process_name)) ||
            ' -> ' ||
            COALESCE(NULLIF(c.sha256, ''), 'path:' || lower(COALESCE(c.exe_path, '')), 'name:' || lower(c.process_name)) AS chain_key,
            MIN(p.process_name) AS parent_process_name,
            MIN(c.process_name) AS child_process_name,
            COUNT(*) AS rows_count
        FROM events c
        JOIN events p
          ON p.machine_name = c.machine_name
         AND p.pid = c.ppid
         AND p.start_time <= c.start_time
         AND (p.end_time IS NULL OR p.end_time = '' OR p.end_time >= c.start_time)
        WHERE c.sample_time >= ?
        GROUP BY chain_key
        ORDER BY rows_count DESC, chain_key ASC
        LIMIT ?
        """,
        (since, max(1, min(int(limit), 500))),
    )
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items}

@app.get("/api/analytics/chains")
def analytics_chains(
    x_api_key: Optional[str] = Header(default=None),
    days: int = 7,
    machine_name: str = "",
    process_name: str = "",
    chain_key: str = "",
    limit: int = 50,
):

    require_api_key(x_api_key)
    days = int(days)
    limit = int(limit)
    since, _ = _window_bounds(days)
    machine = machine_name.strip()
    proc = process_name.strip().lower()
    chain_filter = chain_key.strip()

    conn = db_connect()
    cur = conn.cursor()

    wh = ["sample_time >= ?"]
    params: List[Any] = [since]
    if machine:
        wh.append("machine_name = ?")
        params.append(machine)
    if proc:
        wh.append("lower(process_name) = ?")
        params.append(proc)

    cur.execute(
        f"""
        SELECT machine_name, process_name, pid, ppid, start_time, end_time, duration_seconds, sample_time, sha256, exe_path
        FROM events
        WHERE {" AND ".join(wh)}
        ORDER BY machine_name, start_time, sample_time
        """,
        params,
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    latest_by_session: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for r in rows:
        key = (r["machine_name"], int(r["pid"] or 0), r["start_time"])
        prev = latest_by_session.get(key)
        if not prev or str(r["sample_time"]) > str(prev["sample_time"]):
            latest_by_session[key] = r

    nodes = latest_by_session
    children_map: Dict[Tuple[str, int, str], List[Tuple[str, int, str]]] = {}

    node_items = list(nodes.items())
    for child_key, child in node_items:
        ppid = child.get("ppid")
        if ppid is None:
            continue

        child_start = _safe_parse_iso(child.get("start_time"))
        best_parent = None
        best_parent_dt = None

        for parent_key, parent in node_items:
            if parent_key[0] != child_key[0]:
                continue
            if int(parent.get("pid") or 0) != int(ppid):
                continue

            parent_start = _safe_parse_iso(parent.get("start_time"))
            parent_end = _safe_parse_iso(parent.get("end_time")) if parent.get("end_time") else None
            if child_start and parent_start and parent_start > child_start:
                continue
            if child_start and parent_end and parent_end < child_start:
                continue

            if best_parent is None or (parent_start and best_parent_dt and parent_start > best_parent_dt):
                best_parent = parent_key
                best_parent_dt = parent_start

        if best_parent:
            children_map.setdefault(best_parent, []).append(child_key)

    def _node_bin_key(node: Dict[str, Any]) -> str:
        return _binary_key(node.get("sha256"), node.get("exe_path"), node.get("process_name") or "")

    def render_tree(root_key: Tuple[str, int, str], depth: int = 0) -> Tuple[List[str], List[str]]:
        r = nodes[root_key]
        line = f"{'  ' * depth}{'' if depth == 0 else '-> '}{r.get('process_name') or 'unknown'} (pid={r.get('pid')}, start={r.get('start_time')})"
        lines = [line]
        edge_keys: List[str] = []

        for ck in children_map.get(root_key, []):
            parent_node = nodes[root_key]
            child_node = nodes[ck]
            edge_key = f"{_node_bin_key(parent_node)} -> {_node_bin_key(child_node)}"
            edge_keys.append(edge_key)

            child_lines, child_edge_keys = render_tree(ck, depth + 1)
            lines.extend(child_lines)
            edge_keys.extend(child_edge_keys)

        return lines, edge_keys

    all_children = {ck for lst in children_map.values() for ck in lst}
    roots = [k for k in nodes.keys() if k not in all_children]

    items = []
    for rk in roots[:limit]:
        lines, edge_keys = render_tree(rk)
        items.append({
            "machine_name": rk[0],
            "root_process": nodes[rk].get("process_name"),
            "chain_keys": edge_keys,
            "text": "\n".join(lines),
        })

    if chain_filter:
        items = [item for item in items if chain_filter in (item.get("chain_keys") or [])]

    return {"window_days": days, "items": items}


# Alerts API (MVP)

@app.get("/api/alerts")
def get_alerts(
    x_api_key: Optional[str] = Header(default=None),
    since: str = "",
    status: str = "",
    severity: str = "",
    machine: str = "",
    user: str = "",
    metric: str = "",
    entity_type: str = "",
    limit: int = 200,
    offset: int = 0,
):
    require_api_key(x_api_key)

    wh = []
    params: List[Any] = []

    if since.strip():
        wh.append("created_at >= ?")
        params.append(since.strip())
    if status.strip():
        wh.append("status = ?")
        params.append(status.strip())
    if severity.strip():
        wh.append("severity = ?")
        params.append(severity.strip())
    if machine.strip():
        wh.append("machine_name = ?")
        params.append(machine.strip())
    if user.strip():
        wh.append("user_name = ?")
        params.append(user.strip())
    if metric.strip():
        wh.append("metric = ?")
        params.append(metric.strip())
    if entity_type.strip():
        wh.append("entity_type = ?")
        params.append(entity_type.strip())

    where_sql = ("WHERE " + " AND ".join(wh)) if wh else ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) AS c FROM alerts {where_sql}", params)
    total = int((cur.fetchone() or {"c": 0})["c"])

    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))

    cur.execute(
        f"""
        SELECT
            id, created_at, sample_time,
            machine_name, user_name,
            entity_type,
            process_name, pid, start_time,
            sha256, exe_path,
            parent_process_name, parent_sha256, parent_exe_path, chain_key,
            metric, value, baseline, score, severity, reason,
            status, ack_by, ack_at, closed_at,
            bucket_hour, dedup_key
        FROM alerts
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        params + [lim, off],
    )
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items, "total": total}


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    payload: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)
    ack_by = str(payload.get("ack_by") or "").strip() or "user"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE alerts
        SET status='ack', ack_by=?, ack_at=?
        WHERE id=?
        """,
        (ack_by, now, int(alert_id)),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return {"ok": True, "updated": int(changed)}


@app.post("/api/alerts/{alert_id}/close")
def close_alert(
    alert_id: int,
    payload: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)
    by = str(payload.get("closed_by") or payload.get("ack_by") or "").strip() or "user"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE alerts
        SET status='closed', ack_by=COALESCE(ack_by, ?), ack_at=COALESCE(ack_at, ?), closed_at=?
        WHERE id=?
        """,
        (by, now, now, int(alert_id)),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return {"ok": True, "updated": int(changed)}

@app.post("/api/alerts/{alert_id}/status")
def set_alert_status(
    alert_id: int,
    payload: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    status = str(payload.get("status") or "").strip().lower()
    if status not in {"new", "ack", "closed"}:
        raise HTTPException(status_code=400, detail="status must be one of: new, ack, closed")

    by = str(payload.get("by") or payload.get("ack_by") or payload.get("closed_by") or "").strip() or "user"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    if status == "new":
        cur.execute(
            """
            UPDATE alerts
            SET status='new', ack_by=NULL, ack_at=NULL, closed_at=NULL
            WHERE id=?
            """,
            (int(alert_id),),
        )
    elif status == "ack":
        cur.execute(
            """
            UPDATE alerts
            SET status='ack', ack_by=?, ack_at=?, closed_at=NULL
            WHERE id=?
            """,
            (by, now, int(alert_id)),
        )
    else:
        cur.execute(
            """
            UPDATE alerts
            SET status='closed', ack_by=COALESCE(ack_by, ?), ack_at=COALESCE(ack_at, ?), closed_at=?
            WHERE id=?
            """,
            (by, now, now, int(alert_id)),
        )

    conn.commit()
    changed = cur.rowcount
    conn.close()
    return {"ok": True, "updated": int(changed), "status": status}


# Client release

def _release_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_agent.exe")


@app.get("/api/client-release")
def get_client_release(x_client_key: Optional[str] = Header(default=None)):
    require_client_key(x_client_key)
    cfg = _get_cfg()
    return {"client_release": cfg.get("client_release")}


@app.get("/api/download/client-agent")
def download_client_agent(x_client_key: Optional[str] = Header(default=None)):
    require_client_key(x_client_key)
    p = _release_file_path()
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="Client release file not found")
    return FileResponse(path=p, filename=os.path.basename(p), media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn
    host = str(_cfg_get("host", _cfg_get("listen_host", "0.0.0.0")))
    port = int(_cfg_get("port", _cfg_get("listen_port", 8000)))
    uvicorn.run(app, host=host, port=port)
