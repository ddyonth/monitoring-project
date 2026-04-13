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
            allowed_types_json TEXT,
            updated_at TEXT,
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
            process_name TEXT NOT NULL,
            pid INTEGER,
            start_time TEXT,

            sha256 TEXT,
            exe_path TEXT,

            metric TEXT NOT NULL,              -- cpu_delta | rss | io_delta | net_conn_count
            value REAL NOT NULL,
            baseline REAL,
            score REAL,                        -- ratio (or NULL)
            severity TEXT NOT NULL,            -- low | med | high
            reason TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'new', -- new | ack | closed
            ack_by TEXT,
            ack_at TEXT,
            closed_at TEXT,

            bucket_hour TEXT NOT NULL,         -- YYYY-MM-DDTHH
            dedup_key TEXT NOT NULL
        );
        """
    )

    # indexes for speed
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_sample_time ON alerts(sample_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status_sev_time ON alerts(status, severity, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_machine_time ON alerts(machine_name, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts(user_name, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_metric_time ON alerts(metric, created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_sha_time ON alerts(sha256, created_at);")

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
    refresh_seconds = int(_cfg_get("refresh_seconds", 120))
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
    except Exception:
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
    cur.execute("SELECT role_id, role_name, description FROM roles ORDER BY role_name;")
    roles = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": roles}


@app.post("/api/roles")
def create_role(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    role_name = str(payload.get("role_name", "")).strip()
    description = str(payload.get("description", "") or "").strip()
    if not role_name:
        raise HTTPException(status_code=400, detail="role_name is required")
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO roles(role_name, description) VALUES(?, ?);", (role_name, description))
        conn.commit()
        rid = cur.lastrowid
    except sqlite3.IntegrityError:
        cur.execute("SELECT role_id FROM roles WHERE role_name=? LIMIT 1;", (role_name,))
        row = cur.fetchone()
        rid = int(row["role_id"]) if row else None
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
        if use_sha:
            cur.execute(
                f"""
                SELECT {metric} AS v
                FROM events
                WHERE user_name=? AND NULLIF(sha256,'') IS NOT NULL AND sha256=?
                  AND sample_time>=? AND sample_time<?
                  AND {metric} IS NOT NULL
                """,
                (user_name, sha256, since_iso, until_iso),
            )
        else:
            cur.execute(
                f"""
                SELECT {metric} AS v
                FROM events
                WHERE user_name=? AND (sha256 IS NULL OR sha256='') AND exe_path=?
                  AND sample_time>=? AND sample_time<?
                  AND {metric} IS NOT NULL
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


def _try_insert_alert(
    cur: sqlite3.Cursor,
    alert: Dict[str, Any],
) -> bool:
    """
    Inserts alert with hourly dedup. Returns True if inserted.
    """
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO alerts(
                created_at, sample_time,
                machine_name, user_name, process_name, pid, start_time,
                sha256, exe_path,
                metric, value, baseline, score, severity, reason,
                status, ack_by, ack_at, closed_at,
                bucket_hour, dedup_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                alert["created_at"],
                alert["sample_time"],
                alert["machine_name"],
                alert["user_name"],
                alert["process_name"],
                alert.get("pid"),
                alert.get("start_time"),
                alert.get("sha256"),
                alert.get("exe_path"),
                alert["metric"],
                float(alert["value"]),
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


def detect_alerts_for_ingested_events(events_payload: List[Dict[str, Any]]) -> int:
    """
    MVP mode: run after ingest for the newest sample_time per session found in payload.
    Heavy work is limited to per-session and per-(user,binary,metric) baseline windows.
    """
    if not events_payload:
        return 0

    # collect newest sample_time per session_key from payload
    newest: Dict[Tuple[str, int, str], str] = {}
    for e in events_payload:
        try:
            m = str(e.get("machine_name") or "").strip()
            pid = int(e.get("pid") or 0)
            st = str(e.get("start_time") or "").strip()
            sm = str(e.get("sample_time") or "").strip()
            if not (m and pid and st and sm):
                continue
            k = (m, pid, st)
            prev = newest.get(k)
            if (not prev) or (str(sm) > str(prev)):
                newest[k] = sm
        except Exception:
            continue

    if not newest:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    days = int(_cfg_get("alert_baseline_window_days", ALERT_BASELINE_WINDOW_DAYS_DEFAULT))
    nmin = int(_cfg_get("alert_baseline_min_points", ALERT_BASELINE_MIN_POINTS_DEFAULT))

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

        # binary identity + fallback marker
        bin_id = sha256 if sha256 else exe_path
        sha_missing = not bool(sha256)

        # baseline window bounds
        until_iso = str(latest["sample_time"])
        since_dt = _safe_parse_iso(until_iso)
        if since_dt:
            since_iso = (since_dt - timedelta(days=days)).isoformat(timespec="seconds")
        else:
            since_iso, _ = _window_bounds(days)

        # compute metric current values
        cpu_delta_val = None
        io_delta_val = None
        if prev:
            try:
                cu = latest["cpu_user_time_s"]
                cs = latest["cpu_system_time_s"]
                pu = prev["cpu_user_time_s"]
                ps = prev["cpu_system_time_s"]
                if cu is not None or cs is not None:
                    cur_tot = float(cu or 0.0) + float(cs or 0.0)
                    prev_tot = float(pu or 0.0) + float(ps or 0.0)
                    cpu_delta_val = float(max(0.0, cur_tot - prev_tot))
            except Exception:
                cpu_delta_val = None

            try:
                cr = latest["io_read_bytes"]
                cw = latest["io_write_bytes"]
                pr = prev["io_read_bytes"]
                pw = prev["io_write_bytes"]
                if cr is not None or cw is not None:
                    cur_tot = float(cr or 0.0) + float(cw or 0.0)
                    prev_tot = float(pr or 0.0) + float(pw or 0.0)
                    io_delta_val = float(max(0.0, cur_tot - prev_tot))
            except Exception:
                io_delta_val = None

        rss_val = None
        try:
            if latest["rss_bytes"] is not None:
                rss_val = float(latest["rss_bytes"])
        except Exception:
            rss_val = None

        net_val = None
        try:
            if latest["net_conn_count"] is not None:
                net_val = float(latest["net_conn_count"])
        except Exception:
            net_val = None

        # optional: if net_active is present and false -> skip NET alerting
        net_active = latest["net_active"]
        net_skip = (net_active == 0 or net_active is False)

        metrics_now: Dict[str, Optional[float]] = {
            "cpu_delta": cpu_delta_val,
            "io_delta": io_delta_val,
            "rss": rss_val,
            "net_conn_count": (None if net_skip else net_val),
        }

        for metric, value_now in metrics_now.items():
            if value_now is None:
                continue

            rule = ALERT_RULES.get(metric)
            if not rule:
                continue

            K = float(rule["K"])
            abs_thr = float(rule["abs"])
            very_high_abs = float(rule["very_high_abs"])

            # baseline (median)
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

            # decision:
            fired = False
            used_mode = ""
            if baseline is not None and len(baseline_values) >= nmin:
                if (value_now > baseline * K) and (value_now > abs_thr):
                    fired = True
                    used_mode = "ratio"
            else:
                # baseline insufficient -> only very high absolute threshold
                if value_now > very_high_abs:
                    fired = True
                    used_mode = "abs_only"

            if not fired:
                continue

            bucket_hour = _bucket_hour_from_sample(str(latest["sample_time"]))
            dedup_bin = (sha256 if sha256 else exe_path)
            dedup_key = f"{machine}|{user_name}|{dedup_bin}|{metric}|{bucket_hour}"

            # reason (explainable)
            interval_s = None
            if prev:
                dt1 = _safe_parse_iso(str(prev["sample_time"]))
                dt2 = _safe_parse_iso(str(latest["sample_time"]))
                if dt1 and dt2:
                    interval_s = int(max(0, (dt2 - dt1).total_seconds()))

            reason = (
                f"metric={metric} value={value_now:.4g} "
                f"baseline={(baseline if baseline is not None else 'null')} "
                f"ratio={(ratio if ratio is not None else 'null')} "
                f"K={K} abs={abs_thr} mode={used_mode}"
            )
            if interval_s is not None:
                reason += f" interval_s={interval_s}"
            if sha_missing:
                reason += " sha256_missing_fallback_exe_path=true"

            sev = _severity_from_ratio(ratio)

            alert_row = {
                "created_at": now_iso,
                "sample_time": str(latest["sample_time"]),
                "machine_name": machine,
                "user_name": user_name,
                "process_name": process_name,
                "pid": int(latest["pid"] or 0) if latest["pid"] is not None else None,
                "start_time": str(latest["start_time"]) if latest["start_time"] is not None else None,
                "sha256": sha256 or None,
                "exe_path": exe_path or None,
                "metric": metric,
                "value": float(value_now),
                "baseline": (float(baseline) if baseline is not None else None),
                "score": (float(ratio) if ratio is not None else None),
                "severity": sev,
                "reason": reason,
                "status": "new",
                "bucket_hour": bucket_hour,
                "dedup_key": dedup_key,
            }

            if _try_insert_alert(cur, alert_row):
                inserted += 1

    conn.commit()
    conn.close()
    return inserted


def _binary_key(sha256: Optional[str], process_name: str) -> str:
    s = (sha256 or "").strip()
    if s:
        return s
    return f"name:{process_name.lower()}"


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
        SELECT lower(process_name) AS pname, CAST(strftime('%H', start_time) AS INTEGER) AS hh, COUNT(*) AS c
        FROM events
        WHERE start_time >= ?
        GROUP BY pname, hh
        """,
        (since,),
    )
    hist: Dict[str, Dict[int, int]] = {}
    total: Dict[str, int] = {}
    for r in cur.fetchall():
        pname = r["pname"]
        hh = int(r["hh"])
        c = int(r["c"])
        hist.setdefault(pname, {})[hh] = c
        total[pname] = total.get(pname, 0) + c

    typical: Dict[str, set] = {}
    for pname, buckets in hist.items():
        tot = total.get(pname, 0)
        if tot < min_samples:
            continue
        mx = max(buckets.values()) if buckets else 0
        thr = max(2, int(mx * 0.3))
        typical[pname] = {h for h, c in buckets.items() if c >= thr}

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
        LIMIT 500
        """,
        (since,),
    )
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        pname = (r["process_name"] or "").lower()
        st = _safe_parse_iso(r["start_time"])
        if not st:
            continue
        if pname in typical and typical[pname]:
            if st.hour not in typical[pname]:
                d = dict(r)
                d["time_anomaly"] = True
                d["pkey"] = _binary_key(d.get("sha256"), d.get("process_name") or "")
                out.append(d)
        if len(out) >= limit:
            break

    return {"window_days": days, "items": out}


@app.get("/api/analytics/chains")
def analytics_chains(x_api_key: Optional[str] = Header(default=None), days: int = 7, machine_name: str = "", limit: int = 50):
    require_api_key(x_api_key)
    days = int(days)
    limit = int(limit)
    since, _ = _window_bounds(days)
    machine = machine_name.strip()

    conn = db_connect()
    cur = conn.cursor()

    q = """
    WITH latest AS (
      SELECT machine_name, pid, start_time, MAX(sample_time) AS max_sample
      FROM events
      WHERE sample_time >= ?
      {machine_filter}
      GROUP BY machine_name, pid, start_time
    )
    SELECT e.machine_name, e.process_name, e.pid, e.ppid, e.start_time, e.end_time, e.duration_seconds
    FROM events e
    JOIN latest l
      ON e.machine_name=l.machine_name AND e.pid IS l.pid AND e.start_time=l.start_time AND e.sample_time=l.max_sample
    ORDER BY e.machine_name, e.start_time
    """
    mf = ""
    params: List[Any] = [since]
    if machine:
        mf = "AND machine_name=?"
        params.append(machine)

    cur.execute(q.format(machine_filter=mf), params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    nodes: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for r in rows:
        m = r["machine_name"]
        pid = r.get("pid")
        st = r.get("start_time")
        if pid is None or st is None:
            continue
        nodes[(m, int(pid), st)] = r

    children_map: Dict[Tuple[str, int, str], List[Tuple[str, int, str]]] = {}
    for (m, pid, st), r in nodes.items():
        ppid = r.get("ppid")
        if ppid is None:
            continue
        for (m2, ppid2, st2) in list(nodes.keys()):
            if m2 == m and ppid2 == int(ppid):
                children_map.setdefault((m2, ppid2, st2), []).append((m, pid, st))
                break

    def render_tree(root_key: Tuple[str, int, str], depth: int = 0) -> List[str]:
        r = nodes[root_key]
        name = r.get("process_name") or "unknown"
        pid = r.get("pid")
        st = r.get("start_time") or ""
        dur = r.get("duration_seconds") or 0
        end = r.get("end_time")
        line = f"{'  '*depth}{'└─' if depth else ''}{name} (pid={pid}, start={st}, dur={dur}s{' end='+end if end else ''})"
        lines = [line]
        for ck in children_map.get(root_key, []):
            lines.extend(render_tree(ck, depth + 1))
        return lines

    all_children = {ck for lst in children_map.values() for ck in lst}
    roots = [k for k in nodes.keys() if k not in all_children]

    trees = []
    for rk in roots[:limit]:
        trees.append({"machine_name": rk[0], "text": "\n".join(render_tree(rk))})

    return {"window_days": days, "items": trees}


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

    where_sql = ("WHERE " + " AND ".join(wh)) if wh else ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) AS c FROM alerts {where_sql}", params)
    total = int((cur.fetchone() or {"c": 0})["c"])

    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))

    cur.execute(
        f"""
        SELECT *
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
    host = str(_cfg_get("host", "0.0.0.0"))
    port = int(_cfg_get("port", 8000))
    uvicorn.run(app, host=host, port=port)
