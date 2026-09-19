import hashlib
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg import ClientCursor
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Config / Paths

def resource_dir() -> str:
    """Directory containing this script (works for PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


RES_DIR = resource_dir()
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# API keys (can be overridden in config.json)
API_KEY_DEFAULT = "CHANGE_ME_LOCAL_KEY"
CLIENT_UPDATE_KEY_DEFAULT = "CHANGE_ME_CLIENT_KEY"

# Secrets: environment variables take priority over config.json for these keys
CFG_ENV_OVERRIDES = {
    "api_key": "MONITORING_API_KEY",
    "client_update_key": "MONITORING_CLIENT_UPDATE_KEY",
    "database_url": "MONITORING_DATABASE_URL",
    # базовый URL дашборда для ссылки в email-уведомлениях (эпик 5); пусто = без ссылки
    "dashboard_base_url": "MONITORING_DASHBOARD_URL",
}

# analytics defaults
PROFILE_WINDOW_DAYS_DEFAULT = 14
RARE_COUNT_THRESHOLD_DEFAULT = 3
RARE_MACHINE_THRESHOLD_DEFAULT = 2
TIME_HIST_MIN_SAMPLES_DEFAULT = 20
ONLINE_THRESHOLD_MINUTES_DEFAULT = 20

# Дерево процессов (эпик 3, фаза A): защитный потолок глубины обхода вверх и вниз
PROCESS_TREE_MAX_DEPTH_DEFAULT = 30

# Эвристики структуры цепочек (process_chain): пороги подобраны по распределению
# в реальных данных (см. отчёт по фазе A). Baseline считается по бинарнику
# (корневому — для глубины, родительскому — для fan-out) по всем машинам за
# alert_baseline_window_days.
CHAIN_DEPTH_MARGIN_DEFAULT = 2            # depth_now > max(prior depths under root) + margin
CHAIN_DEPTH_MIN_SESSIONS_DEFAULT = 10     # минимум прошлых сессий под корнем для baseline
CHAIN_FANOUT_K_DEFAULT = 2.0              # fanout_now > max(prior fanout of parent binary) * K
CHAIN_FANOUT_ABS_DEFAULT = 5              # ... и fanout_now > abs
CHAIN_FANOUT_MIN_PARENTS_DEFAULT = 3      # минимум прошлых сессий родительского бинарника


# Alerts (MVP) defaults

ALERT_BASELINE_WINDOW_DAYS_DEFAULT = 14
ALERT_BASELINE_MIN_POINTS_DEFAULT = 20

# Email-уведомления (эпик 5): порядок severity для severity_min и допустимые режимы правил
SEVERITY_ORDER = {"low": 0, "med": 1, "high": 2}
NOTIFY_TRIGGER_MODES = ("immediate", "repeat_count")
NOTIFY_RESEND_MODES = ("once", "every_occurrence")
# SMTP только из окружения (секреты не хранятся в config.json); STARTTLS, порт по умолчанию 587
SMTP_PORT_DEFAULT = 587
SMTP_TIMEOUT_SECONDS = 15
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
    env_name = CFG_ENV_OVERRIDES.get(key)
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val is not None and env_val.strip():
            return env_val
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

# Row/cursor types: строки читаются как dict (r["column"]), как раньше DbRow
DbRow = Dict[str, Any]
DbCursor = psycopg.Cursor


def _database_url() -> str:
    url = str(_cfg_get("database_url", "") or "").strip()
    if not url:
        raise RuntimeError(
            "Database is not configured: set MONITORING_DATABASE_URL "
            "(postgresql://user:password@host:5432/dbname)"
        )
    return url


def db_connect() -> psycopg.Connection:
    # Соединение на каждый вызов, как раньше с SQLite (без пула).
    # ClientCursor подставляет параметры на клиенте (как psycopg2) — иначе
    # Postgres не может вывести тип параметра в выражениях вроде (%s = '').
    return psycopg.connect(_database_url(), row_factory=dict_row, cursor_factory=ClientCursor)


def ensure_schema() -> None:
    conn = db_connect()
    cur = conn.cursor()

    # NB: все даты/время (start_time, sample_time, end_time, received_at, created_at,
    # last_seen, updated_at, ack_at, closed_at, boot_time, bucket_hour) намеренно
    # остаются TEXT в ISO-формате: код сравнивает их как строки.
    # Счётчики байт — BIGINT (в SQLite INTEGER был 64-битным).

    # --- main append-only events table (one row per sample)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id BIGSERIAL PRIMARY KEY,
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
            duration_seconds BIGINT NOT NULL,

            cpu_user_time_s DOUBLE PRECISION,
            cpu_system_time_s DOUBLE PRECISION,
            rss_bytes BIGINT,
            io_read_bytes BIGINT,
            io_write_bytes BIGINT,
            io_read_count BIGINT,
            io_write_count BIGINT,
            net_active INTEGER,
            net_conn_count INTEGER,

            boot_time TEXT,
            os_info TEXT,
            "current_user" TEXT,
            client_version TEXT,

            unique_key TEXT NOT NULL UNIQUE,
            received_at TEXT NOT NULL
        );
        """
    )

    # --- MIGRATIONS for existing DBs (backward-compatible)
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS sha256 TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS end_time TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS ppid INTEGER;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS net_active INTEGER;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS net_conn_count INTEGER;")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(machine_name, pid, start_time, sample_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_sha ON events(sha256);")
    # обход дерева потомков: на каждом уровне ищем детей по (machine_name, ppid)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ppid ON events(machine_name, ppid);")

    # --- machine inventory/state
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_state (
            machine_name TEXT PRIMARY KEY,
            boot_time TEXT,
            os_info TEXT,
            "current_user" TEXT,
            client_version TEXT,
            last_seen TEXT,
            updated_at TEXT
        );
        """
    )
    cur.execute("ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS updated_at TEXT;")
    cur.execute("ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS last_seen TEXT;")
    cur.execute("ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS client_version TEXT;")
    cur.execute('ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS "current_user" TEXT;')
    cur.execute("ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS os_info TEXT;")
    cur.execute("ALTER TABLE machine_state ADD COLUMN IF NOT EXISTS boot_time TEXT;")

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
    cur.execute("ALTER TABLE chain_catalog ADD COLUMN IF NOT EXISTS updated_at TEXT;")
    # ручная разметка эксперта: ID техники MITRE ATT&CK (например "T1059"), без автоклассификации
    cur.execute("ALTER TABLE chain_catalog ADD COLUMN IF NOT EXISTS attack_technique_id TEXT;")

    # --- roles (minimal)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS roles (
            role_id BIGSERIAL PRIMARY KEY,
            role_name TEXT NOT NULL UNIQUE,
            description TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_roles (
            machine_name TEXT PRIMARY KEY,
            role_id BIGINT,
            updated_at TEXT,
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS role_profiles (
            role_id BIGINT PRIMARY KEY,
            allowed_hashes_json TEXT,
            updated_at TEXT,
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS role_allowed_process_types (
            role_id BIGINT NOT NULL,
            process_type TEXT NOT NULL,
            updated_at TEXT,
            PRIMARY KEY(role_id, process_type),
            FOREIGN KEY(role_id) REFERENCES roles(role_id)
        );
        """
    )

    # --- client agent releases: бинарник хранится в БД, чтобы переживать
    # пересборку Docker-образа; "текущий" релиз = последняя загруженная строка
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS client_releases (
            id SERIAL PRIMARY KEY,
            version TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            data BYTEA NOT NULL,
            uploaded_at TEXT NOT NULL
        );
        """
    )
    # ОС релиза: в старых установках строки были только для Windows
    cur.execute("ALTER TABLE client_releases ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'windows';")

    # Alerts (MVP): server-side detections stored in DB
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id BIGSERIAL PRIMARY KEY,

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
                                                -- | role_type_mismatch | chain_depth_anomaly | chain_fanout_anomaly
            value DOUBLE PRECISION,
            baseline DOUBLE PRECISION,
            score DOUBLE PRECISION,
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
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS entity_type TEXT;")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS parent_process_name TEXT;")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS parent_sha256 TEXT;")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS parent_exe_path TEXT;")
    cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS chain_key TEXT;")

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

    # --- email-уведомления по алертам (эпик 5): правила и журнал попыток отправки.
    # NULL/пустой массив в фильтре правила = любое значение подходит.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_notification_rules (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            entity_types TEXT[] NULL,
            metrics TEXT[] NULL,
            severity_min TEXT NULL,
            machine_names TEXT[] NULL,
            trigger_mode TEXT NOT NULL,            -- 'immediate' | 'repeat_count'
            repeat_threshold INT NULL,
            repeat_window_minutes INT NULL,
            resend_mode TEXT NOT NULL DEFAULT 'once',  -- 'once' | 'every_occurrence'
            recipients TEXT[] NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_notifications (
            id SERIAL PRIMARY KEY,
            rule_id INT NOT NULL REFERENCES alert_notification_rules(id) ON DELETE CASCADE,
            alert_id INT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
            series_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL,                  -- 'sent' | 'failed'
            error TEXT NULL
        );
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_notifications_rule_series ON alert_notifications(rule_id, series_key);"
    )


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
        INSERT INTO machine_state(machine_name, boot_time, os_info, "current_user", client_version, last_seen, updated_at)
        VALUES(%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(machine_name) DO UPDATE SET
            boot_time=COALESCE(excluded.boot_time, machine_state.boot_time),
            os_info=COALESCE(excluded.os_info, machine_state.os_info),
            "current_user"=COALESCE(excluded."current_user", machine_state."current_user"),
            client_version=COALESCE(excluded.client_version, machine_state.client_version),
            last_seen=(CASE WHEN machine_state.last_seen IS NULL THEN NULL
                       ELSE GREATEST(COALESCE(excluded.last_seen, machine_state.last_seen), machine_state.last_seen) END),
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
            # savepoint на каждую строку: в Postgres ошибка (дубликат unique_key)
            # переводит транзакцию в aborted-состояние, откат к savepoint
            # позволяет продолжить вставку остальных событий пакета
            with conn.transaction():
                cur.execute(
                    """
                INSERT INTO events(
                    machine_name, user_name, process_name, pid, ppid, exe_path, sha256,
                    start_time, sample_time, end_time, duration_seconds,
                    cpu_user_time_s, cpu_system_time_s, rss_bytes,
                    io_read_bytes, io_write_bytes, io_read_count, io_write_count,
                    net_active, net_conn_count,
                    boot_time, os_info, "current_user", client_version,
                    unique_key, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        except UniqueViolation:
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

def _latest_samples_by_session(cur: DbCursor, machine: str, since_iso: str) -> List[DbRow]:
    cur.execute(
        """
        WITH latest AS (SELECT pid, start_time, MAX(sample_time) AS max_sample
                        FROM events
                        WHERE machine_name = %s
                          AND sample_time >= %s
                        GROUP BY pid, start_time),
             prev AS (SELECT e.pid, e.start_time, MAX(e.sample_time) AS prev_sample
                      FROM events e
                               JOIN latest l
                                    ON e.pid IS NOT DISTINCT FROM l.pid AND e.start_time = l.start_time
                      WHERE e.machine_name = %s
                        AND e.sample_time < l.max_sample
                      GROUP BY e.pid, e.start_time)
        SELECT e.*,
               p.prev_sample                          AS prev_sample_time,
               (SELECT cpu_user_time_s
                FROM events e2
                WHERE e2.machine_name = e.machine_name
                  AND e2.pid IS NOT DISTINCT FROM e.pid
                  AND e2.start_time = e.start_time
                  AND e2.sample_time = p.prev_sample) AS prev_cpu_user_time_s,
               (SELECT cpu_system_time_s
                FROM events e2
                WHERE e2.machine_name = e.machine_name
                  AND e2.pid IS NOT DISTINCT FROM e.pid
                  AND e2.start_time = e.start_time
                  AND e2.sample_time = p.prev_sample) AS prev_cpu_system_time_s,
               CASE
                   WHEN p.prev_sample IS NULL OR e.cpu_user_time_s IS NULL THEN NULL
                   ELSE (CASE WHEN (SELECT cpu_user_time_s
                                    FROM events e2
                                    WHERE e2.machine_name = e.machine_name
                                      AND e2.pid IS NOT DISTINCT FROM e.pid
                                      AND e2.start_time = e.start_time
                                      AND e2.sample_time = p.prev_sample) IS NULL THEN NULL
                         ELSE GREATEST(0, e.cpu_user_time_s - (SELECT cpu_user_time_s
                                                    FROM events e2
                                                    WHERE e2.machine_name = e.machine_name
                                                      AND e2.pid IS NOT DISTINCT FROM e.pid
                                                      AND e2.start_time = e.start_time
                                                      AND e2.sample_time = p.prev_sample)) END)
                   END                                AS cpu_delta_user_s,
               CASE
                   WHEN p.prev_sample IS NULL OR e.cpu_system_time_s IS NULL THEN NULL
                   ELSE (CASE WHEN (SELECT cpu_system_time_s
                                    FROM events e2
                                    WHERE e2.machine_name = e.machine_name
                                      AND e2.pid IS NOT DISTINCT FROM e.pid
                                      AND e2.start_time = e.start_time
                                      AND e2.sample_time = p.prev_sample) IS NULL THEN NULL
                         ELSE GREATEST(0, e.cpu_system_time_s - (SELECT cpu_system_time_s
                                                      FROM events e2
                                                      WHERE e2.machine_name = e.machine_name
                                                        AND e2.pid IS NOT DISTINCT FROM e.pid
                                                        AND e2.start_time = e.start_time
                                                        AND e2.sample_time = p.prev_sample)) END)
                   END                                AS cpu_delta_system_s
        FROM events e
                 JOIN latest l
                      ON e.machine_name = %s AND e.pid IS NOT DISTINCT FROM l.pid AND e.start_time = l.start_time AND
                         e.sample_time = l.max_sample
                 LEFT JOIN prev p
                           ON p.pid IS NOT DISTINCT FROM e.pid AND p.start_time = e.start_time
        ORDER BY lower(e.process_name)
        """,
        (machine, since_iso, machine, machine),
    )

    return cur.fetchall()


def _group_stopped(rows: List[DbRow], limit_per_group: int = 80) -> List[Dict[str, Any]]:
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


def _platform_from_os_info(os_info: Optional[str]) -> str:
    """Платформа машины по её os_info (для выбора релиза, с которым сравнивать версию).

    Windows-коллектор всегда отдаёт строку вида "Windows 10 ...", Linux-коллектор —
    PRETTY_NAME дистрибутива ("ALT Workstation 11.1", "Astra Linux 1.7_x86-64").
    Пустой os_info (машина ещё не присылала его) считаем windows — как и запрос
    агента без заголовка X-Client-Platform.
    """
    v = str(os_info or "").strip().lower()
    if not v:
        return CLIENT_PLATFORM_DEFAULT
    return "windows" if v.startswith("windows") else "linux"


@app.get("/api/latest")
def latest(x_api_key: Optional[str] = Header(default=None), limit_machines: int = 50):
    require_api_key(x_api_key)

    now_dt = datetime.now(timezone.utc)
    online_thr = int(_cfg_get("online_threshold_minutes", ONLINE_THRESHOLD_MINUTES_DEFAULT))
    online_delta = timedelta(minutes=online_thr)

    conn = db_connect()
    cur = conn.cursor()

    # Версии релизов раздельные по ОС, поэтому каждую машину сравниваем
    # с релизом её платформы (определяется по os_info).
    latest_by_platform = {
        p: str((_get_latest_client_release(cur, p) or {}).get("version") or "").strip()
        for p in CLIENT_PLATFORMS
    }

    cur.execute(
        """
        SELECT ms.machine_name, ms.boot_time, ms.os_info, ms."current_user", ms.client_version, ms.last_seen,
               COALESCE(ma.alias,'') AS alias
        FROM machine_state ms
        LEFT JOIN machine_aliases ma ON ma.machine_name = ms.machine_name
        ORDER BY ms.machine_name
        LIMIT %s
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

        def _choose_main(group: List[DbRow]) -> Dict[str, Any]:
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
        running_by_name: Dict[str, List[DbRow]] = {}
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
        latest_client_version = latest_by_platform.get(_platform_from_os_info(mr["os_info"]), "")
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
        SELECT chain_key, chain_name, chain_type, description, attack_technique_id, updated_at
        FROM chain_catalog
        ORDER BY lower(chain_name), lower(chain_key)
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
    # attack_technique_id: ключ отсутствует в payload -> значение в БД не трогаем
    # (дашборд фазы A поле ещё не знает и не должен затирать разметку);
    # ключ есть (в т.ч. пустая строка) -> записываем, пустое -> NULL
    technique_given = "attack_technique_id" in payload
    attack_technique_id = str(payload.get("attack_technique_id") or "").strip() or None

    if not chain_key:
        raise HTTPException(status_code=400, detail="chain_key required")
    if not chain_name:
        raise HTTPException(status_code=400, detail="chain_name required")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chain_catalog(chain_key, chain_name, chain_type, description, attack_technique_id, updated_at)
        VALUES(%s, %s, %s, %s, %s, %s)
        ON CONFLICT(chain_key) DO UPDATE SET
            chain_name=excluded.chain_name,
            chain_type=excluded.chain_type,
            description=excluded.description,
            attack_technique_id=CASE WHEN %s THEN excluded.attack_technique_id
                                     ELSE chain_catalog.attack_technique_id END,
            updated_at=excluded.updated_at
        """,
        (chain_key, chain_name, chain_type or None, description or None, attack_technique_id, now, technique_given),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# Правила email-уведомлений (эпик 5): CRUD по образцу chain-catalog

def _norm_str_list(value: Any) -> Optional[List[str]]:
    """Список строк из списка или строки через запятую; пусто -> None (= любое значение)."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise HTTPException(status_code=400, detail="list of strings expected")
    items = [x for x in items if x]
    return items or None


def _parse_optional_int(value: Any, field: str, minimum: int) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    if n < minimum:
        raise HTTPException(status_code=400, detail=f"{field} must be >= {minimum}")
    return n


def _validate_alert_rule_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    recipients = _norm_str_list(payload.get("recipients")) or []
    if not recipients:
        raise HTTPException(status_code=400, detail="recipients required (at least one email)")
    bad = [r for r in recipients if not _EMAIL_RE.match(r)]
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid email: {', '.join(bad)}")

    trigger_mode = str(payload.get("trigger_mode") or "").strip()
    if trigger_mode not in NOTIFY_TRIGGER_MODES:
        raise HTTPException(status_code=400, detail="trigger_mode must be 'immediate' or 'repeat_count'")

    repeat_threshold = None
    repeat_window_minutes = None
    if trigger_mode == "repeat_count":
        repeat_threshold = _parse_optional_int(payload.get("repeat_threshold"), "repeat_threshold", 2)
        if repeat_threshold is None:
            raise HTTPException(status_code=400, detail="repeat_threshold required for repeat_count (>= 2)")
        repeat_window_minutes = _parse_optional_int(payload.get("repeat_window_minutes"), "repeat_window_minutes", 1)

    resend_mode = str(payload.get("resend_mode") or "once").strip()
    if resend_mode not in NOTIFY_RESEND_MODES:
        raise HTTPException(status_code=400, detail="resend_mode must be 'once' or 'every_occurrence'")

    severity_min = str(payload.get("severity_min") or "").strip() or None
    if severity_min is not None and severity_min not in SEVERITY_ORDER:
        raise HTTPException(status_code=400, detail="severity_min must be low, med or high")

    enabled = payload.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "yes", "on")

    return {
        "name": name,
        "enabled": bool(enabled),
        "entity_types": _norm_str_list(payload.get("entity_types")),
        "metrics": _norm_str_list(payload.get("metrics")),
        "severity_min": severity_min,
        "machine_names": _norm_str_list(payload.get("machine_names")),
        "trigger_mode": trigger_mode,
        "repeat_threshold": repeat_threshold,
        "repeat_window_minutes": repeat_window_minutes,
        "resend_mode": resend_mode,
        "recipients": recipients,
    }


@app.get("/api/alert-rules")
def get_alert_rules(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alert_notification_rules ORDER BY id")
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items}


@app.post("/api/alert-rule-item")
def upsert_alert_rule_item(payload: Dict[str, Any] = Body(...), x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    fields = _validate_alert_rule_payload(payload)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rule_id = _parse_optional_int(payload.get("id"), "id", 1)

    conn = db_connect()
    cur = conn.cursor()
    try:
        if rule_id is None:
            cur.execute(
                """
                INSERT INTO alert_notification_rules(
                    name, enabled, entity_types, metrics, severity_min, machine_names,
                    trigger_mode, repeat_threshold, repeat_window_minutes, resend_mode, recipients,
                    created_at, updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    fields["name"], fields["enabled"], fields["entity_types"], fields["metrics"],
                    fields["severity_min"], fields["machine_names"], fields["trigger_mode"],
                    fields["repeat_threshold"], fields["repeat_window_minutes"], fields["resend_mode"],
                    fields["recipients"], now, now,
                ),
            )
            rule_id = int(cur.fetchone()["id"])
        else:
            cur.execute(
                """
                UPDATE alert_notification_rules SET
                    name=%s, enabled=%s, entity_types=%s, metrics=%s, severity_min=%s, machine_names=%s,
                    trigger_mode=%s, repeat_threshold=%s, repeat_window_minutes=%s, resend_mode=%s,
                    recipients=%s, updated_at=%s
                WHERE id=%s
                """,
                (
                    fields["name"], fields["enabled"], fields["entity_types"], fields["metrics"],
                    fields["severity_min"], fields["machine_names"], fields["trigger_mode"],
                    fields["repeat_threshold"], fields["repeat_window_minutes"], fields["resend_mode"],
                    fields["recipients"], now, rule_id,
                ),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="rule not found")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": rule_id}


@app.post("/api/alert-rule-item/{rule_id}/delete")
def delete_alert_rule_item(rule_id: int, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM alert_notification_rules WHERE id=%s", (int(rule_id),))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True}


@app.get("/api/alert-rules/{rule_id}/notifications")
def get_alert_rule_notifications(rule_id: int, limit: int = 20, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    limit = max(1, min(int(limit), 200))
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM alert_notification_rules WHERE id=%s", (int(rule_id),))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="rule not found")
    cur.execute(
        """
        SELECT n.id, n.rule_id, n.alert_id, n.series_key, n.sent_at, n.status, n.error,
               a.machine_name, a.metric, a.severity, a.process_name
        FROM alert_notifications n
        LEFT JOIN alerts a ON a.id = n.alert_id
        WHERE n.rule_id = %s
        ORDER BY n.sent_at DESC, n.id DESC
        LIMIT %s
        """,
        (int(rule_id), limit),
    )
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"items": items}


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
        VALUES(%s, %s, %s)
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
        cur.execute("DELETE FROM machine_aliases WHERE machine_name=%s;", (machine_name,))
        conn.commit()
        conn.close()
        return {"ok": True, "action": "deleted"}

    cur.execute(
        """
        INSERT INTO machine_aliases(machine_name, alias, updated_at)
        VALUES(%s, %s, %s)
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
               ms."current_user" AS "current_user",
               ms.client_version AS client_version
        FROM machine_state ms
        LEFT JOIN machine_aliases ma ON ma.machine_name = ms.machine_name
        LEFT JOIN machine_roles mr ON mr.machine_name = ms.machine_name
        LEFT JOIN roles r ON r.role_id = mr.role_id
        ORDER BY ms.machine_name
        LIMIT %s
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
            with conn.transaction():
                cur.execute(
                    "INSERT INTO roles(role_name, description) VALUES(%s, %s) RETURNING role_id;",
                    (role_name, description),
                )
                rid = int(cur.fetchone()["role_id"])
            conn.commit()
        except UniqueViolation:
            cur.execute("SELECT role_id FROM roles WHERE role_name=%s LIMIT 1;", (role_name,))
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
            cur.execute("UPDATE roles SET description=%s WHERE role_id=%s;", (description, int(role_id)))
        else:
            rn = str(role_name).strip()
            if not rn:
                raise HTTPException(status_code=400, detail="role_name is required")
            cur.execute("UPDATE roles SET role_name=%s, description=%s WHERE role_id=%s;", (rn, description, int(role_id)))
            if allowed_process_types is not None:
                _replace_role_allowed_process_types(cur, int(role_id), allowed_process_types)
        conn.commit()
    except UniqueViolation:
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
        cur.execute("DELETE FROM machine_roles WHERE machine_name=%s;", (machine_name,))
    else:
        cur.execute(
            """
            INSERT INTO machine_roles(machine_name, role_id, updated_at)
            VALUES(%s, %s, %s)
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


def _session_key_from_row(r: DbRow) -> Tuple[str, int, str]:
    return (str(r["machine_name"]), int(r["pid"] or 0), str(r["start_time"]))


def _get_latest_and_prev_for_session(cur: DbCursor, machine: str, pid: int, start_time: str, current_sample: str) -> Tuple[Optional[DbRow], Optional[DbRow]]:
    cur.execute(
        """
        SELECT *
        FROM events
        WHERE machine_name=%s AND pid IS NOT DISTINCT FROM %s AND start_time=%s AND sample_time<=%s
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
    cur: DbCursor,
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
                WHERE user_name=%s AND NULLIF(sha256,'') IS NOT NULL AND sha256=%s
                  AND sample_time>=%s AND sample_time<%s
                  AND {metric_col} IS NOT NULL
                """,
                (user_name, sha256, since_iso, until_iso),
            )
        else:
            cur.execute(
                f"""
                SELECT {metric_col} AS v
                FROM events
                WHERE user_name=%s AND (sha256 IS NULL OR sha256='') AND exe_path=%s
                  AND sample_time>=%s AND sample_time<%s
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
            WHERE user_name=%s AND NULLIF(sha256,'') IS NOT NULL AND sha256=%s
              AND sample_time>=%s AND sample_time<%s
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
            WHERE user_name=%s AND (sha256 IS NULL OR sha256='') AND exe_path=%s
              AND sample_time>=%s AND sample_time<%s
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


def _try_insert_alert(cur: DbCursor, alert: Dict[str, Any]) -> bool:
    try:
        with cur.connection.transaction():
            cur.execute(
                """
            INSERT INTO alerts(
                created_at, sample_time,
                machine_name, user_name,
                entity_type,
                process_name, pid, start_time,
                sha256, exe_path,
                parent_process_name, parent_sha256, parent_exe_path, chain_key,
                metric, value, baseline, score, severity, reason,
                status, ack_by, ack_at, closed_at,
                bucket_hour, dedup_key
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
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
            row = cur.fetchone()
        if not row:
            return False
        # id нужен журналу email-уведомлений (alert_notifications.alert_id)
        alert["id"] = int(row["id"])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Email-уведомления по алертам (эпик 5).
# Вызывается синхронно из детектора сразу после успешной вставки алерта;
# любая ошибка (БД, SMTP) гасится внутри — /api/ingest не должен падать.
# ---------------------------------------------------------------------------

def _make_alert_series_key(alert: Dict[str, Any]) -> str:
    """Как _make_alert_dedup_key, но без bucket_hour: одна «серия» = одна сущность + метрика."""
    if alert["entity_type"] == "process_chain":
        return f"{alert['machine_name']}|{alert['user_name']}|{alert.get('chain_key') or ''}|{alert['metric']}"
    return f"{alert['machine_name']}|{alert['user_name']}|{_binary_key(alert.get('sha256'), alert.get('exe_path'), alert.get('process_name') or '')}|{alert['metric']}"


def _rule_matches_alert(rule: Dict[str, Any], alert: Dict[str, Any]) -> bool:
    """NULL/пустой список в фильтре правила = любое значение подходит."""
    entity_types = rule.get("entity_types") or []
    if entity_types and str(alert.get("entity_type") or "") not in entity_types:
        return False
    metrics = rule.get("metrics") or []
    if metrics and str(alert.get("metric") or "") not in metrics:
        return False
    machine_names = rule.get("machine_names") or []
    if machine_names and str(alert.get("machine_name") or "") not in machine_names:
        return False
    severity_min = str(rule.get("severity_min") or "").strip()
    if severity_min:
        if SEVERITY_ORDER.get(str(alert.get("severity") or ""), -1) < SEVERITY_ORDER.get(severity_min, 0):
            return False
    return True


def _count_series_alerts(cur: DbCursor, series_key: str, since_iso: Optional[str]) -> int:
    """Число алертов этой серии (включая текущий). dedup_key = series_key|bucket_hour,
    поэтому сравниваем префикс фиксированной длины (LIKE не подходит: в путях бывают '_' и '%')."""
    prefix = series_key + "|"
    if since_iso:
        cur.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE left(dedup_key, %s) = %s AND created_at >= %s",
            (len(prefix), prefix, since_iso),
        )
    else:
        cur.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE left(dedup_key, %s) = %s",
            (len(prefix), prefix),
        )
    row = cur.fetchone()
    return int(row["c"] or 0) if row else 0


def _smtp_settings() -> Optional[Dict[str, Any]]:
    """Настройки SMTP только из окружения. None = не настроено (нет хоста или отправителя)."""
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        return None
    try:
        port = int(os.environ.get("SMTP_PORT", "").strip() or SMTP_PORT_DEFAULT)
    except ValueError:
        port = SMTP_PORT_DEFAULT
    return {
        "host": host,
        "port": port,
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": sender,
    }


def _build_alert_email(alert: Dict[str, Any], rule: Dict[str, Any]) -> Tuple[str, str]:
    severity = str(alert.get("severity") or "")
    metric = str(alert.get("metric") or "")
    machine = str(alert.get("machine_name") or "")
    subject = f"[monitoring] {severity} {metric} {machine}"

    if alert.get("entity_type") == "process_chain":
        entity_line = f"Цепочка: {alert.get('parent_process_name') or 'unknown'} -> {alert.get('process_name') or ''}"
        if alert.get("chain_key"):
            entity_line += f" ({alert.get('chain_key')})"
    else:
        entity_line = f"Процесс: {alert.get('process_name') or ''}"
        if alert.get("pid") is not None:
            entity_line += f" (pid {alert.get('pid')})"

    lines = [
        f"Правило: {rule.get('name') or ''}",
        f"Уровень: {severity}",
        f"Метрика: {metric}",
        f"Машина: {machine}",
        f"Пользователь: {alert.get('user_name') or ''}",
        entity_line,
        f"Время: {alert.get('sample_time') or alert.get('created_at') or ''}",
        "",
        f"Причина: {alert.get('reason') or ''}",
    ]
    base_url = str(_cfg_get("dashboard_base_url", "") or "").strip().rstrip("/")
    if base_url:
        lines += ["", f"Дашборд: {base_url}/ (вкладка «Аналитика», алерт #{alert.get('id')})"]
    return subject, "\n".join(lines)


def _send_alert_email(recipients: List[str], subject: str, body: str) -> None:
    """Отправка через SMTP + STARTTLS. Бросает исключение при любой ошибке (в т.ч. если SMTP не настроен)."""
    smtp = _smtp_settings()
    if not smtp:
        raise RuntimeError("SMTP не настроен")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp["sender"]
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=SMTP_TIMEOUT_SECONDS) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if smtp["user"] and smtp["password"]:
            server.login(smtp["user"], smtp["password"])
        server.sendmail(smtp["sender"], list(recipients), msg.as_string())


def _record_notification(cur: DbCursor, rule_id: int, alert_id: int, series_key: str,
                         status: str, error: Optional[str]) -> None:
    with cur.connection.transaction():
        cur.execute(
            """
            INSERT INTO alert_notifications(rule_id, alert_id, series_key, sent_at, status, error)
            VALUES(%s, %s, %s, %s, %s, %s)
            """,
            (rule_id, alert_id, series_key, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             status, (error or None)),
        )


def evaluate_and_send_notifications(cur: DbCursor, alert: Dict[str, Any]) -> int:
    """Проверяет включённые правила для только что вставленного алерта (alert["id"] задан)
    и отправляет письма. Возвращает число попыток отправки (успешных и нет)."""
    alert_id = alert.get("id")
    if alert_id is None:
        return 0
    series_key = _make_alert_series_key(alert)

    with cur.connection.transaction():
        cur.execute("SELECT * FROM alert_notification_rules WHERE enabled = true ORDER BY id")
        rules = [dict(r) for r in cur.fetchall()]

    attempts = 0
    for rule in rules:
        if not _rule_matches_alert(rule, alert):
            continue

        if rule.get("trigger_mode") == "repeat_count":
            threshold = int(rule.get("repeat_threshold") or 0)
            window = rule.get("repeat_window_minutes")
            since_iso: Optional[str] = None
            if window:
                ref = _safe_parse_iso(str(alert.get("created_at") or "")) or datetime.now(timezone.utc)
                since_iso = (ref - timedelta(minutes=int(window))).isoformat(timespec="seconds")
            with cur.connection.transaction():
                count = _count_series_alerts(cur, series_key, since_iso)
            if count < threshold:
                continue
        elif rule.get("trigger_mode") != "immediate":
            continue

        if rule.get("resend_mode") != "every_occurrence":
            with cur.connection.transaction():
                cur.execute(
                    "SELECT 1 FROM alert_notifications WHERE rule_id = %s AND series_key = %s AND status = 'sent' LIMIT 1",
                    (rule["id"], series_key),
                )
                if cur.fetchone():
                    continue

        recipients = [str(x).strip() for x in (rule.get("recipients") or []) if str(x).strip()]
        status, error = "sent", None
        try:
            if not recipients:
                raise RuntimeError("нет получателей")
            subject, body = _build_alert_email(alert, rule)
            _send_alert_email(recipients, subject, body)
        except Exception as e:
            status = "failed"
            error = str(e) or type(e).__name__
            print(f"[notify] rule {rule['id']} alert {alert_id}: {error}", file=sys.stderr)
        attempts += 1
        _record_notification(cur, int(rule["id"]), int(alert_id), series_key, status, error)
    return attempts


def _load_parent_context(cur: DbCursor, latest: DbRow) -> Optional[Dict[str, Any]]:
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
        WHERE machine_name=%s AND pid=%s
          AND sample_time<=%s
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
    cur: DbCursor,
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
        match_sql = "NULLIF(sha256, '') IS NOT NULL AND sha256=%s"
        params.append(sha256)
    elif exe_path:
        match_sql = "lower(COALESCE(exe_path, ''))=%s"
        params.append(exe_path.lower())
    else:
        match_sql = "lower(process_name)=%s"
        params.append(process_name.lower())

    user_sql = ""
    if user_name:
        user_sql = " AND COALESCE(user_name, '')=%s"
        params.append(user_name)

    cur.execute(
        f"""
        SELECT COUNT(DISTINCT machine_name || '|' || COALESCE(pid::text, '') || '|' || start_time) AS c
        FROM events
        WHERE machine_name=%s
          AND sample_time < %s
          AND NOT (machine_name=%s AND COALESCE(pid, 0)=%s AND start_time=%s)
          AND {match_sql}
          {user_sql}
        """,
        params,
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _process_in_catalog(cur: DbCursor, process_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM process_catalog WHERE lower(process_name)=lower(%s) LIMIT 1;",
        (process_name.strip(),),
    )
    return cur.fetchone() is not None


def _count_process_sessions_on_machine(
    cur: DbCursor,
    machine: str,
    pid: int,
    start_time: str,
    sample_time: str,
    process_name: str,
) -> int:
    cur.execute(
        """
        SELECT COUNT(DISTINCT machine_name || '|' || COALESCE(pid::text, '') || '|' || start_time) AS c
        FROM events
        WHERE machine_name=%s
          AND sample_time < %s
          AND lower(process_name)=lower(%s)
          AND NOT (machine_name=%s AND COALESCE(pid, 0)=%s AND start_time=%s)
        """,
        (machine, sample_time, process_name, machine, int(pid), start_time),
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _count_prior_chain_sessions(
    cur: DbCursor,
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
        user_sql = " AND COALESCE(c.user_name, '')=%s"
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
          FROM (
            -- одна строка на сессию. В SQLite это делал GROUP BY с "голыми"
            -- колонками: формально произвольная строка группы, фактически (проверено
            -- на индексе idx_events_session) — САМЫЙ РАННИЙ сэмпл сессии. Сохраняем
            -- именно это поведение: ORDER BY sample_time ASC.
            SELECT DISTINCT ON (machine_name, pid, start_time) *
            FROM events
            WHERE machine_name=%s
              AND sample_time < %s
              AND NOT (machine_name=%s AND COALESCE(pid, 0)=%s AND start_time=%s)
            ORDER BY machine_name, pid, start_time, sample_time ASC
          ) AS last_samples
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
        SELECT COUNT(DISTINCT c.machine_name || '|' || COALESCE(c.pid::text, '') || '|' || c.start_time) AS c
        FROM child_sessions c
        JOIN parent_candidates p
          ON p.machine_name = c.machine_name
         AND p.pid = c.ppid
         AND p.start_time <= c.start_time
         AND (p.end_time IS NULL OR p.end_time = '' OR p.end_time >= c.start_time)
        WHERE c.child_key=%s
          AND p.parent_key=%s
          {user_sql}
        """,
        params + [child_key, parent_key],
    )
    row = cur.fetchone()
    return int((row["c"] if row else 0) or 0)

def _chain_in_catalog(cur: DbCursor, chain_key: str) -> bool:
    cur.execute("SELECT 1 FROM chain_catalog WHERE chain_key=%s LIMIT 1;", (chain_key,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Дерево процессов (эпик 3, фаза A)
#
# Узел дерева = сессия процесса (machine_name, pid, start_time). Связь
# родитель -> потомок: тот же принцип, что в _load_parent_context /
# _count_prior_chain_sessions / analytics_chains — по (machine_name, pid=ppid
# потомка) И по времени: start_time родителя <= start_time потомка <= end_time
# родителя (или end_time ещё не известен). Из нескольких кандидатов с одним pid,
# удовлетворяющих условию (pid переиспользован, а end_time старого не записан),
# берётся тот, что стартовал ПОЗЖЕ всех, но не позже потомка (как в
# analytics_chains): NOT EXISTS более поздней сессии с тем же pid, начавшейся
# до потомка. Это даёт ровно одного родителя на потомка внутри одного
# SQL-запроса WITH RECURSIVE, без цикла запросов в Python.
#
# end_time сессии берётся как MAX(end_time) по её сэмплам (агент пишет end_time
# в финальный сэмпл), ppid — из последнего сэмпла.
# ---------------------------------------------------------------------------

def _tree_max_depth() -> int:
    try:
        return max(1, int(_cfg_get("process_tree_max_depth", PROCESS_TREE_MAX_DEPTH_DEFAULT)))
    except Exception:
        return PROCESS_TREE_MAX_DEPTH_DEFAULT


def _node_id(pid: Any, start_time: Any) -> str:
    return f"{int(pid or 0)}|{start_time}"


def _binary_match_sql(sha256: str, exe_path: str, process_name: str, alias: str = "") -> Tuple[str, List[Any]]:
    """Условие "тот же бинарник", как в _count_prior_binary_sessions / _typical_hours_for_process."""
    a = f"{alias}." if alias else ""
    if sha256:
        return f"NULLIF({a}sha256, '') IS NOT NULL AND {a}sha256=%s", [sha256]
    if exe_path:
        return f"lower(COALESCE({a}exe_path, ''))=%s", [exe_path.lower()]
    return f"lower({a}process_name)=%s", [(process_name or "").lower()]


# сравнение времени в SQL — строковое, как в _count_prior_chain_sessions
# (даты хранятся ISO-строками одного формата от агента)
_SESSION_END_SQL = """(SELECT MAX(NULLIF(e.end_time, '')) FROM events e
                        WHERE e.machine_name = {p}.machine_name AND e.pid = {p}.pid AND e.start_time = {p}.start_time)"""


def _descendant_rows(
    cur: DbCursor,
    seed_sql: str,
    seed_params: List[Any],
    max_depth: int,
    since_iso: Optional[str] = None,
    until_iso: Optional[str] = None,
) -> List[DbRow]:
    """
    Обход потомков вниз от "семян" (seed_sql возвращает machine_name, pid,
    start_time; уровень 0). Возвращает строки (machine_name, pid, start_time,
    parent_pid, parent_start, depth); depth <= max_depth + 1 — строки уровня
    max_depth + 1 нужны только чтобы понять, что потолок достигнут.
    since/until ограничивают сэмплы потомков (окно baseline); семена не ограничивают.
    """
    win_sql = ""
    win_params: List[Any] = []
    if since_iso:
        win_sql += " AND c.sample_time >= %s"
        win_params.append(since_iso)
    if until_iso:
        win_sql += " AND c.sample_time < %s"
        win_params.append(until_iso)

    cur.execute(
        f"""
        WITH RECURSIVE down AS (
            SELECT seed.machine_name, seed.pid, seed.start_time,
                   NULL::integer AS parent_pid, NULL::text AS parent_start,
                   0 AS depth,
                   ARRAY[seed.pid::text || '|' || seed.start_time] AS path
            FROM ({seed_sql}) AS seed
            UNION
            SELECT c.machine_name, c.pid, c.start_time,
                   d.pid, d.start_time,
                   d.depth + 1,
                   d.path || (c.pid::text || '|' || c.start_time)
            FROM down d
            JOIN events c
              ON c.machine_name = d.machine_name
             AND c.ppid = d.pid
            WHERE d.depth <= %s
              AND c.pid IS NOT NULL
              AND NOT (c.pid = d.pid AND c.start_time = d.start_time)
              AND NOT ((c.pid::text || '|' || c.start_time) = ANY(d.path))
              -- перекрытие по времени: start родителя <= start потомка <= end родителя (или end неизвестен)
              AND c.start_time >= d.start_time
              AND COALESCE({_SESSION_END_SQL.format(p="d")} >= c.start_time, TRUE)
              -- pid переиспользован: родитель — самая поздняя сессия с этим pid, начавшаяся не позже потомка
              AND NOT EXISTS (SELECT 1 FROM events p2
                              WHERE p2.machine_name = c.machine_name AND p2.pid = c.ppid
                                AND p2.start_time > d.start_time AND p2.start_time <= c.start_time)
              {win_sql}
        )
        SELECT machine_name, pid, start_time, parent_pid, parent_start, depth
        FROM down
        ORDER BY depth, machine_name, start_time, pid
        """,
        list(seed_params) + [int(max_depth)] + win_params,
    )
    return [dict(r) for r in cur.fetchall()]


def _ancestor_rows(cur: DbCursor, machine: str, pid: int, start_time: str, max_depth: int) -> List[DbRow]:
    """
    Путь предков вверх от сессии (уровень 0 — сама сессия). Строки
    (machine_name, pid, start_time, ppid, depth), depth <= max_depth + 1.
    Правило выбора родителя то же, что в _descendant_rows.
    """
    cur.execute(
        f"""
        WITH RECURSIVE up AS (
            SELECT seed.machine_name, seed.pid, seed.start_time, seed.ppid,
                   0 AS depth,
                   ARRAY[seed.pid::text || '|' || seed.start_time] AS path
            FROM (SELECT DISTINCT ON (machine_name, pid, start_time) machine_name, pid, start_time, ppid
                  FROM events
                  WHERE machine_name = %s AND pid = %s AND start_time = %s
                  ORDER BY machine_name, pid, start_time, sample_time DESC) AS seed
            UNION
            SELECT p.machine_name, p.pid, p.start_time,
                   (SELECT e.ppid FROM events e
                     WHERE e.machine_name = p.machine_name AND e.pid = p.pid AND e.start_time = p.start_time
                     ORDER BY e.sample_time DESC LIMIT 1) AS ppid,
                   u.depth + 1,
                   u.path || (p.pid::text || '|' || p.start_time)
            FROM up u
            JOIN events p
              ON p.machine_name = u.machine_name
             AND p.pid = u.ppid
            WHERE u.depth <= %s
              AND u.ppid IS NOT NULL
              AND NOT (p.pid = u.pid AND p.start_time = u.start_time)
              AND NOT ((p.pid::text || '|' || p.start_time) = ANY(u.path))
              AND p.start_time <= u.start_time
              AND COALESCE({_SESSION_END_SQL.format(p="p")} >= u.start_time, TRUE)
              AND NOT EXISTS (SELECT 1 FROM events p2
                              WHERE p2.machine_name = p.machine_name AND p2.pid = p.pid
                                AND p2.start_time > p.start_time AND p2.start_time <= u.start_time)
        )
        SELECT machine_name, pid, start_time, ppid, depth
        FROM up
        ORDER BY depth
        """,
        (machine, int(pid), start_time, int(max_depth)),
    )
    return [dict(r) for r in cur.fetchall()]


def _load_session_nodes(cur: DbCursor, machine: str, node_ids: List[str]) -> Dict[str, DbRow]:
    """Последний сэмпл каждой сессии (по node_id = "pid|start_time") одной машины."""
    if not node_ids:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (pid, start_time)
               machine_name, user_name, process_name, pid, ppid, exe_path, sha256,
               start_time, sample_time, end_time, duration_seconds
        FROM events
        WHERE machine_name = %s
          AND (COALESCE(pid, 0)::text || '|' || start_time) = ANY(%s)
        ORDER BY pid, start_time, sample_time DESC
        """,
        (machine, list(node_ids)),
    )
    out: Dict[str, DbRow] = {}
    for r in cur.fetchall():
        out[_node_id(r["pid"], r["start_time"])] = dict(r)
    return out


def build_process_tree(cur: DbCursor, machine: str, pid: int, start_time: str, max_depth: int) -> Optional[Dict[str, Any]]:
    """
    Предки до корня + поддерево потомков от узла (machine, pid, start_time).
    None — если такой сессии нет. Потолок глубины max_depth действует отдельно
    вверх и вниз; при достижении выставляются флаги truncated.
    """
    root_id = _node_id(pid, start_time)
    up = _ancestor_rows(cur, machine, pid, start_time, max_depth)
    if not up:
        return None

    ancestors_truncated = any(int(r["depth"]) > max_depth for r in up)
    up = [r for r in up if int(r["depth"]) <= max_depth]

    down = _descendant_rows(
        cur,
        "SELECT %s::text AS machine_name, %s::integer AS pid, %s::text AS start_time",
        [machine, int(pid), start_time],
        max_depth,
    )
    descendants_truncated = any(int(r["depth"]) > max_depth for r in down)
    down = [r for r in down if int(r["depth"]) <= max_depth]

    # связи: parent_id для каждого узла
    parent_of: Dict[str, Optional[str]] = {}
    level_of: Dict[str, int] = {}
    ancestor_ids: List[str] = []
    prev_id: Optional[str] = None
    for r in up:  # depth 0 = сам узел, дальше вверх
        nid = _node_id(r["pid"], r["start_time"])
        level_of[nid] = -int(r["depth"])
        if prev_id is not None:
            parent_of[prev_id] = nid
        if r["depth"] > 0:
            ancestor_ids.append(nid)
        prev_id = nid
    if prev_id is not None:
        parent_of.setdefault(prev_id, None)

    for r in down:
        nid = _node_id(r["pid"], r["start_time"])
        if int(r["depth"]) == 0:
            continue
        level_of[nid] = int(r["depth"])
        parent_of[nid] = _node_id(r["parent_pid"], r["parent_start"])

    children_of: Dict[str, List[str]] = {}
    for nid, pid_ in parent_of.items():
        if pid_ is not None:
            children_of.setdefault(pid_, []).append(nid)

    order = list(reversed(ancestor_ids)) + [root_id] + [
        _node_id(r["pid"], r["start_time"]) for r in down if int(r["depth"]) > 0
    ]
    details = _load_session_nodes(cur, machine, order)

    nodes: List[Dict[str, Any]] = []
    for nid in order:
        d = details.get(nid) or {}
        nodes.append({
            "node_id": nid,
            "parent_id": parent_of.get(nid),
            "children_ids": children_of.get(nid, []),
            "level": level_of.get(nid, 0),
            "is_root": nid == root_id,
            "machine_name": machine,
            "process_name": d.get("process_name"),
            "user_name": d.get("user_name"),
            "pid": d.get("pid"),
            "ppid": d.get("ppid"),
            "start_time": d.get("start_time"),
            "end_time": d.get("end_time") or None,
            "sha256": d.get("sha256") or None,
            "exe_path": d.get("exe_path") or None,
            "sample_time": d.get("sample_time"),
            "duration_seconds": d.get("duration_seconds"),
        })

    return {
        "root": {"node_id": root_id, "machine_name": machine, "pid": int(pid), "start_time": start_time},
        "max_depth": int(max_depth),
        "truncated": bool(ancestors_truncated or descendants_truncated),
        "ancestors_truncated": bool(ancestors_truncated),
        "descendants_truncated": bool(descendants_truncated),
        "ancestor_ids": ancestor_ids,  # от родителя вверх к корню
        "nodes": nodes,               # предки сверху вниз, корень, потомки по уровням
    }



# ---------------------------------------------------------------------------
# Эвристики структуры цепочек: аномальная глубина и аномальный fan-out
# ---------------------------------------------------------------------------

def _chain_depth_context(cur: DbCursor, machine: str, pid: int, start_time: str) -> Optional[Dict[str, Any]]:
    """
    Глубина текущей сессии (число предков) и её корень (верхний найденный предок).
    None — если предков нет или потолок обхода достигнут (корень неизвестен).
    """
    cap = _tree_max_depth()
    up = _ancestor_rows(cur, machine, pid, start_time, cap)
    if not up or any(int(r["depth"]) > cap for r in up):
        return None
    if len(up) < 2:
        return None
    top = up[-1]
    lineage = {_node_id(r["pid"], r["start_time"]) for r in up}
    root_nodes = _load_session_nodes(cur, machine, [_node_id(top["pid"], top["start_time"])])
    root = root_nodes.get(_node_id(top["pid"], top["start_time"])) or {}
    root_sha = str(root.get("sha256") or "").strip()
    root_exe = str(root.get("exe_path") or "").strip()
    root_name = str(root.get("process_name") or "").strip()
    return {
        "depth": len(up) - 1,
        "lineage_ids": lineage,
        "root_process_name": root_name,
        "root_key": _binary_key(root_sha, root_exe, root_name),
        "root_match": (root_sha, root_exe, root_name),
    }


def _sessions_of_binary_seed_sql(sha256: str, exe_path: str, process_name: str, since_iso: str, until_iso: str) -> Tuple[str, List[Any]]:
    """Семена обхода: все сессии данного бинарника (все машины) с сэмплами в окне."""
    match_sql, params = _binary_match_sql(sha256, exe_path, process_name)
    sql = f"""SELECT DISTINCT machine_name, pid, start_time
              FROM events
              WHERE pid IS NOT NULL AND {match_sql}
                AND sample_time >= %s AND sample_time < %s"""
    return sql, params + [since_iso, until_iso]


def _chain_depth_baseline_rows(
    cur: DbCursor, root_match: Tuple[str, str, str], since_iso: str, until_iso: str
) -> List[DbRow]:
    """
    Все потомки всех сессий корневого бинарника в окне: (machine, pid, start_time, depth).
    Сессия того же бинарника может быть вложена в другую (systemd -> systemd --user):
    тогда узел приходит дважды с разной глубиной — оставляем максимальную, т.е.
    глубину от самого верхнего корня (так же считается depth текущей сессии).
    """
    seed_sql, seed_params = _sessions_of_binary_seed_sql(*root_match, since_iso, until_iso)
    rows = _descendant_rows(cur, seed_sql, seed_params, _tree_max_depth(), since_iso, until_iso)
    best: Dict[Tuple[str, str], DbRow] = {}
    for r in rows:
        k = (str(r["machine_name"]), _node_id(r["pid"], r["start_time"]))
        if k not in best or int(r["depth"]) > int(best[k]["depth"]):
            best[k] = r
    return list(best.values())


def _chain_fanout_baseline(
    cur: DbCursor, parent_match: Tuple[str, str, str], since_iso: str, until_iso: str
) -> Dict[Tuple[str, str], int]:
    """Число детей у каждой сессии родительского бинарника (все машины) в окне."""
    seed_sql, seed_params = _sessions_of_binary_seed_sql(*parent_match, since_iso, until_iso)
    rows = _descendant_rows(cur, seed_sql, seed_params, 1, since_iso, until_iso)
    counts: Dict[Tuple[str, str], int] = {}
    for r in rows:
        if int(r["depth"]) == 0:
            counts.setdefault((str(r["machine_name"]), _node_id(r["pid"], r["start_time"])), 0)
        elif int(r["depth"]) == 1:
            k = (str(r["machine_name"]), _node_id(r["parent_pid"], r["parent_start"]))
            counts[k] = counts.get(k, 0) + 1
    return counts


def _chain_fanout_now(cur: DbCursor, machine: str, pid: int, start_time: str) -> int:
    """Текущее число детей у одной сессии (без окна: все её дети, записанные к этому моменту)."""
    rows = _descendant_rows(
        cur,
        "SELECT %s::text AS machine_name, %s::integer AS pid, %s::text AS start_time",
        [machine, int(pid), start_time],
        1,
    )
    return sum(1 for r in rows if int(r["depth"]) == 1)


def _find_parent_session(cur: DbCursor, machine: str, pid: int, start_time: str) -> Optional[DbRow]:
    """Родительская сессия (последний сэмпл) по тому же правилу, что и обход дерева."""
    up = _ancestor_rows(cur, machine, pid, start_time, 1)
    parents = [r for r in up if int(r["depth"]) == 1]
    if not parents:
        return None
    nid = _node_id(parents[0]["pid"], parents[0]["start_time"])
    return _load_session_nodes(cur, machine, [nid]).get(nid)


def _get_machine_role_profile(cur: DbCursor, machine_name: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT r.role_id, r.role_name
        FROM machine_roles mr
        JOIN roles r ON r.role_id = mr.role_id
        WHERE mr.machine_name = %s
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

def _get_process_type(cur: DbCursor, process_name: str) -> str:
    cur.execute(
        """
        SELECT process_type
        FROM process_catalog
        WHERE lower(process_name) = lower(%s)
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


def _get_role_allowed_process_types(cur: DbCursor, role_id: int) -> List[str]:
    cur.execute(
        """
        SELECT process_type
        FROM role_allowed_process_types
        WHERE role_id = %s
        ORDER BY lower(process_type)
        """,
        (int(role_id),),
    )
    rows = cur.fetchall()
    return [str(row["process_type"]).strip() for row in rows if str(row["process_type"] or "").strip()]


def _replace_role_allowed_process_types(cur: DbCursor, role_id: int, raw: Any) -> List[str]:
    allowed_process_types = _normalize_allowed_process_types(raw)
    cur.execute("DELETE FROM role_allowed_process_types WHERE role_id=%s;", (int(role_id),))

    if not allowed_process_types:
        return []

    now = datetime.now().isoformat(timespec="seconds")
    cur.executemany(
        """
        INSERT INTO role_allowed_process_types(role_id, process_type, updated_at)
        VALUES(%s, %s, %s)
        """,
        [(int(role_id), process_type, now) for process_type in allowed_process_types],
    )
    return allowed_process_types

def _typical_hours_for_process(
    cur: DbCursor,
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
        match_sql = "NULLIF(sha256, '') IS NOT NULL AND sha256=%s"
        params.append(sha256)
    elif exe_path:
        match_sql = "lower(COALESCE(exe_path, ''))=%s"
        params.append(exe_path.lower())
    else:
        match_sql = "lower(process_name)=%s"
        params.append(process_name.lower())

    user_sql = ""
    if user_name:
        user_sql = " AND COALESCE(user_name, '')=%s"
        params.append(user_name)

    cur.execute(
        f"""
        WITH sessions AS (
          SELECT
              start_time,
              machine_name || '|' || COALESCE(pid::text, '') || '|' || start_time AS session_key
          FROM events
          WHERE machine_name=%s
            AND sample_time < %s
            AND NOT (machine_name=%s AND COALESCE(pid, 0)=%s AND start_time=%s)
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
    depth_margin = int(_cfg_get("chain_depth_margin", CHAIN_DEPTH_MARGIN_DEFAULT))
    depth_min_sessions = int(_cfg_get("chain_depth_min_sessions", CHAIN_DEPTH_MIN_SESSIONS_DEFAULT))
    fanout_k = float(_cfg_get("chain_fanout_k", CHAIN_FANOUT_K_DEFAULT))
    fanout_abs = int(_cfg_get("chain_fanout_abs", CHAIN_FANOUT_ABS_DEFAULT))
    fanout_min_parents = int(_cfg_get("chain_fanout_min_parents", CHAIN_FANOUT_MIN_PARENTS_DEFAULT))

    conn = db_connect()
    cur = conn.cursor()
    inserted = 0

    # baseline глубины/fan-out по бинарнику одинаков для всех сессий пакета —
    # считаем один раз на вызов (обход от всех сессий бинарника за окно)
    depth_baseline_memo: Dict[str, List[DbRow]] = {}
    fanout_baseline_memo: Dict[str, Dict[Tuple[str, str], int]] = {}

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

        # --- 3.1 аномальная глубина цепочки (per корневой бинарник).
        # Не подавляется chain_catalog: справочник описывает допустимость ребра
        # parent->child, а глубина — свойство всей цепочки, которая может целиком
        # состоять из "известных" рёбер.
        if parent_ctx:
            depth_ctx = _chain_depth_context(cur, machine, pid, start_time)
            if depth_ctx and depth_ctx["depth"] > 0:
                rk = depth_ctx["root_key"]
                if rk not in depth_baseline_memo:
                    depth_baseline_memo[rk] = _chain_depth_baseline_rows(cur, depth_ctx["root_match"], since_iso, until_iso)
                # "прошлые" сессии под этим корнем: не сама сессия, не её линия предков
                # (их глубины по построению = depth-1, depth-2, ...), стартовавшие раньше неё
                prior_depths = [
                    int(r["depth"]) for r in depth_baseline_memo[rk]
                    if not (str(r["machine_name"]) == machine and _node_id(r["pid"], r["start_time"]) in depth_ctx["lineage_ids"])
                    and str(r["start_time"]) < start_time
                    and int(r["depth"]) > 0
                ]
                depth_now = int(depth_ctx["depth"])
                if len(prior_depths) >= depth_min_sessions:
                    baseline_depth = max(prior_depths)
                    if depth_now > baseline_depth + depth_margin:
                        excess = depth_now - baseline_depth
                        sev = "high" if excess > 2 * depth_margin else "med"
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
                            "chain_key": str(parent_ctx["chain_key"] or ""),
                            "metric": "chain_depth_anomaly",
                            "value": float(depth_now),
                            "baseline": float(baseline_depth),
                            "score": float(excess),
                            "severity": sev,
                            "reason": (
                                f"Аномальная глубина цепочки: {depth_now} уровней от корня "
                                f"{depth_ctx['root_process_name'] or 'unknown'}; типичный максимум для этого корня "
                                f"{baseline_depth} (порог {baseline_depth + depth_margin}, наблюдений {len(prior_depths)})."
                            ),
                            "status": "new",
                            "bucket_hour": bucket_hour,
                        })

        # --- 3.2 аномальный fan-out родительской сессии (per родительский бинарник).
        # Алерт привязан к родительской сессии (pid/start_time = родитель), чтобы
        # дерево от алерта показывало сам "веер"; chain_key = "<parent_key> -> *"
        # (дедуп на родителя, а не на пару parent->child). chain_catalog не
        # подавляет по той же причине, что и глубину.
        if parent_ctx:
            parent_sess = _find_parent_session(cur, machine, pid, start_time)
            if parent_sess:
                p_sha = str(parent_sess.get("sha256") or "").strip()
                p_exe = str(parent_sess.get("exe_path") or "").strip()
                p_name = str(parent_sess.get("process_name") or "").strip()
                p_key = _binary_key(p_sha, p_exe, p_name)
                if p_key not in fanout_baseline_memo:
                    fanout_baseline_memo[p_key] = _chain_fanout_baseline(cur, (p_sha, p_exe, p_name), since_iso, until_iso)
                p_pid = int(parent_sess["pid"] or 0)
                p_start = str(parent_sess["start_time"])
                p_id = (machine, _node_id(p_pid, p_start))
                prior_fanouts = [v for k, v in fanout_baseline_memo[p_key].items() if k != p_id]
                if len(prior_fanouts) >= fanout_min_parents:
                    fanout_now = _chain_fanout_now(cur, machine, p_pid, p_start)
                    baseline_fanout = max(prior_fanouts)
                    if fanout_now > baseline_fanout * fanout_k and fanout_now > fanout_abs:
                        ratio = (float(fanout_now) / float(baseline_fanout)) if baseline_fanout > 0 else None
                        session_alerts.append({
                            "created_at": now_iso,
                            "sample_time": until_iso,
                            "machine_name": machine,
                            "user_name": str(parent_sess.get("user_name") or "").strip() or user_name,
                            "entity_type": "process_chain",
                            "process_name": p_name or "unknown",
                            "pid": p_pid,
                            "start_time": p_start,
                            "sha256": p_sha or None,
                            "exe_path": p_exe or None,
                            "parent_process_name": None,
                            "parent_sha256": None,
                            "parent_exe_path": None,
                            "chain_key": f"{p_key} -> *",
                            "metric": "chain_fanout_anomaly",
                            "value": float(fanout_now),
                            "baseline": float(baseline_fanout),
                            "score": (float(ratio) if ratio is not None else None),
                            "severity": _severity_from_ratio(ratio),
                            "reason": (
                                f"Аномальный fan-out: у процесса {p_name or 'unknown'} (pid={p_pid}) уже {fanout_now} дочерних "
                                f"процессов (последний — {process_name}); типичный максимум для этого бинарника "
                                f"{baseline_fanout} по {len(prior_fanouts)} сессиям (порог: >{baseline_fanout * fanout_k:g} и >{fanout_abs})."
                            ),
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
                # email-уведомления (эпик 5): сбой почты/правил не должен ронять /api/ingest
                try:
                    evaluate_and_send_notifications(cur, alert_row)
                except Exception as e:
                    print(f"[notify] alert {alert_row.get('id')}: {type(e).__name__}: {e}", file=sys.stderr)

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

               COUNT(DISTINCT machine_name || '|' || COALESCE(pid::text, '') || '|' || start_time) AS sessions,
               COUNT(DISTINCT machine_name)                                                  AS machines,
               COUNT(DISTINCT COALESCE(user_name, ''))                                       AS users,
               string_agg(DISTINCT machine_name, ',')                                        AS machine_names

        FROM events
        WHERE sample_time >= %s
        GROUP BY bin_key
        ORDER BY sessions ASC, machines ASC, users ASC LIMIT %s
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
          WHERE sample_time >= %s AND ppid IS NOT NULL
        ),
        p AS (
          SELECT
            machine_name, sample_time, pid,
            COALESCE(NULLIF(sha256,''), 'path:' || lower(COALESCE(exe_path,''))) AS parent_key,
            lower(process_name) AS parent_name
          FROM events
          WHERE sample_time >= %s
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
          string_agg(DISTINCT c.machine_name, ',') AS machine_names,
          CASE WHEN MIN(p.parent_name)=MIN(c.child_name) THEN 1 ELSE 0 END AS self_chain


        FROM c
        JOIN p
          ON p.machine_name = c.machine_name
         AND p.sample_time  = c.sample_time
         AND p.pid          = c.ppid
        GROUP BY p.parent_key, c.child_key
        ORDER BY sessions ASC, machines ASC
        LIMIT %s
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
                      WHERE e.machine_name = %s
                        AND e.sample_time >= %s),
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
            left(MIN(sample_time), 10) AS day
        FROM mains
        WHERE rn = 1
        GROUP BY process_name, pid, start_time
            )
        SELECT process_name,
               COUNT(*)            AS runs,
               COUNT(DISTINCT day) AS seen_days,
               AVG(max_dur)::double precision AS avg_duration_s
        FROM s
        GROUP BY process_name
        ORDER BY runs DESC LIMIT %s
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
          AND (%s = '' OR lower(process_name) LIKE '%%' || %s || '%%')
        GROUP BY lower(process_name)
        ORDER BY rows_count DESC, pname ASC
        LIMIT %s
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
        WHERE lower(process_name) = lower(%s)
          AND sample_time >= %s
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
          WHERE sample_time >= %s
          GROUP BY machine_name, pid, start_time
        )
        SELECT e.*
        FROM events e
        JOIN latest l
          ON e.machine_name=l.machine_name AND e.pid IS NOT DISTINCT FROM l.pid AND e.start_time=l.start_time AND e.sample_time=l.max_sample
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
        WHERE c.sample_time >= %s
        GROUP BY chain_key
        ORDER BY rows_count DESC, chain_key ASC
        LIMIT %s
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

    wh = ["sample_time >= %s"]
    params: List[Any] = [since]
    if machine:
        wh.append("machine_name = %s")
        params.append(machine)
    if proc:
        wh.append("lower(process_name) = %s")
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

@app.get("/api/analytics/process-tree")
def analytics_process_tree(
    x_api_key: Optional[str] = Header(default=None),
    machine_name: str = "",
    pid: int = 0,
    start_time: str = "",
    alert_id: int = 0,
    max_depth: int = 0,
):
    """
    Дерево процессов от одного узла: предки до корня + потомки вниз.
    Корень задаётся либо координатами сессии (machine_name, pid, start_time —
    как у строк /api/latest), либо alert_id (координаты берутся из алерта).
    max_depth — необязательное сужение потолка (не больше настроенного).
    """
    require_api_key(x_api_key)

    ceiling = _tree_max_depth()
    depth_cap = ceiling if int(max_depth or 0) <= 0 else max(1, min(int(max_depth), ceiling))

    conn = db_connect()
    cur = conn.cursor()
    try:
        if int(alert_id or 0) > 0:
            cur.execute("SELECT machine_name, pid, start_time FROM alerts WHERE id=%s", (int(alert_id),))
            a = cur.fetchone()
            if not a:
                raise HTTPException(status_code=404, detail="alert not found")
            machine_name = str(a["machine_name"] or "")
            pid = int(a["pid"] or 0)
            start_time = str(a["start_time"] or "")

        machine = machine_name.strip()
        start = start_time.strip()
        if not machine or not start or int(pid or 0) <= 0:
            raise HTTPException(status_code=400, detail="machine_name, pid and start_time (or alert_id) required")

        tree = build_process_tree(cur, machine, int(pid), start, depth_cap)
        if tree is None:
            raise HTTPException(status_code=404, detail="process session not found")
        return tree
    finally:
        conn.close()


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
    pid: Optional[int] = None,
    start_time: str = "",
    limit: int = 200,
    offset: int = 0,
):
    """
    pid / start_time — точечный фильтр по узлу дерева процессов (alerts.pid,
    alerts.start_time), вместе с machine — «есть ли алерт по этой сессии»
    (вкладка «Графы», клик по узлу).
    """
    require_api_key(x_api_key)

    wh = []
    params: List[Any] = []

    if since.strip():
        wh.append("created_at >= %s")
        params.append(since.strip())
    if status.strip():
        wh.append("status = %s")
        params.append(status.strip())
    if severity.strip():
        wh.append("severity = %s")
        params.append(severity.strip())
    if machine.strip():
        wh.append("machine_name = %s")
        params.append(machine.strip())
    if user.strip():
        wh.append("user_name = %s")
        params.append(user.strip())
    if metric.strip():
        wh.append("metric = %s")
        params.append(metric.strip())
    if entity_type.strip():
        wh.append("entity_type = %s")
        params.append(entity_type.strip())
    if pid is not None:
        wh.append("pid = %s")
        params.append(int(pid))
    if start_time.strip():
        wh.append("start_time = %s")
        params.append(start_time.strip())

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
        LIMIT %s OFFSET %s
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
        SET status='ack', ack_by=%s, ack_at=%s
        WHERE id=%s
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
        SET status='closed', ack_by=COALESCE(ack_by, %s), ack_at=COALESCE(ack_at, %s), closed_at=%s
        WHERE id=%s
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
            WHERE id=%s
            """,
            (int(alert_id),),
        )
    elif status == "ack":
        cur.execute(
            """
            UPDATE alerts
            SET status='ack', ack_by=%s, ack_at=%s, closed_at=NULL
            WHERE id=%s
            """,
            (by, now, int(alert_id)),
        )
    else:
        cur.execute(
            """
            UPDATE alerts
            SET status='closed', ack_by=COALESCE(ack_by, %s), ack_at=COALESCE(ack_at, %s), closed_at=%s
            WHERE id=%s
            """,
            (by, now, now, int(alert_id)),
        )

    conn.commit()
    changed = cur.rowcount
    conn.close()
    return {"ok": True, "updated": int(changed), "status": status}


# Client release
#
# Релизы агента хранятся в таблице client_releases (BYTEA), текущий — последняя
# загруженная строка. Публикация — привилегированное действие (api_key),
# чтение метаданных и скачивание — действия агента (client_update_key).

# "1.4", "1.4.2": числа через точку, минимум два компонента
_CLIENT_VERSION_RE = re.compile(r"^\d+(\.\d+)+$")

# ОС, под которые публикуются релизы агента
CLIENT_PLATFORMS = ("windows", "linux")
CLIENT_PLATFORM_DEFAULT = "windows"


def _client_platform_from_header(value: Optional[str]) -> str:
    """Платформа агента из заголовка X-Client-Platform.

    Заголовка нет или значение незнакомое -> "windows": так ведут себя уже
    задеплоенные старые агенты, которые заголовок не шлют.
    """
    p = str(value or "").strip().lower()
    return p if p in CLIENT_PLATFORMS else CLIENT_PLATFORM_DEFAULT


def _get_latest_client_release(
    cur: DbCursor, platform: str, with_data: bool = False
) -> Optional[Dict[str, Any]]:
    """Последний релиз для платформы; None, если релизов под неё нет.

    with_data=False — только метаданные (без бинарника), with_data=True — ещё и data.
    """
    columns = "id, version, sha256, filename, size_bytes, uploaded_at"
    if with_data:
        columns += ", data"
    cur.execute(
        f"""
        SELECT {columns}
        FROM client_releases
        WHERE platform = %s
        ORDER BY uploaded_at DESC, id DESC
        LIMIT 1
        """,
        (platform,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


@app.post("/api/client-release")
def publish_client_release(
    version: str = Form(default=""),
    platform: str = Form(default=CLIENT_PLATFORM_DEFAULT),
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    v = str(version or "").strip()
    if not _CLIENT_VERSION_RE.match(v):
        raise HTTPException(status_code=400, detail="version must be numbers separated by dots, e.g. 1.4 or 1.4.2")

    plat = str(platform or "").strip().lower()
    if plat not in CLIENT_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {', '.join(CLIENT_PLATFORMS)}")

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="file is empty")

    # Хэш считается сервером от полученных байт; хэш из запроса не принимается.
    sha = hashlib.sha256(data).hexdigest()
    # Имя файла — только последняя компонента, разделители и Windows (\), и POSIX (/)
    filename = re.split(r"[\\/]", str(file.filename or "").strip())[-1] or "client_agent.exe"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO client_releases(version, sha256, filename, size_bytes, data, uploaded_at, platform)
        VALUES(%s, %s, %s, %s, %s, %s, %s)
        """,
        (v, sha, filename, len(data), data, now, plat),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "version": v, "platform": plat, "sha256": sha, "size_bytes": len(data)}


@app.get("/api/client-release")
def get_client_release(
    x_client_key: Optional[str] = Header(default=None),
    x_client_platform: Optional[str] = Header(default=None),
):
    require_client_key(x_client_key)
    conn = db_connect()
    cur = conn.cursor()
    rel = _get_latest_client_release(cur, _client_platform_from_header(x_client_platform))
    conn.close()
    if not rel:
        return {"client_release": None}
    return {
        "client_release": {
            "version": rel["version"],
            "sha256": rel["sha256"],
            "size_bytes": int(rel["size_bytes"]),
        }
    }


@app.get("/api/download/client-agent")
def download_client_agent(
    x_client_key: Optional[str] = Header(default=None),
    x_client_platform: Optional[str] = Header(default=None),
):
    require_client_key(x_client_key)
    conn = db_connect()
    cur = conn.cursor()
    row = _get_latest_client_release(cur, _client_platform_from_header(x_client_platform), with_data=True)
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Client release not found")
    data = bytes(row["data"])
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
    )


if __name__ == "__main__":
    import uvicorn
    host = str(_cfg_get("host", _cfg_get("listen_host", "0.0.0.0")))
    port = int(_cfg_get("port", _cfg_get("listen_port", 8000)))
    uvicorn.run(app, host=host, port=port)
