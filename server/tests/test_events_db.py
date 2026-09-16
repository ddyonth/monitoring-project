"""
Тесты с реальной временной SQLite-базой: validate_event, insert_events
и интеграционный тест детектора алертов detect_alerts_for_ingested_events.
"""

import pytest
import server_app
from fastapi import HTTPException
from server_app import (
    ALERT_RULES,
    detect_alerts_for_ingested_events,
    insert_events,
    validate_event,
)

MiB = 1024 * 1024
GiB = 1024 * MiB

REQUIRED_FIELDS = ["machine_name", "process_name", "start_time", "sample_time", "duration_seconds", "unique_key"]


def make_event(**overrides):
    e = {
        "machine_name": "PC1",
        "user_name": "alice",
        "process_name": "app.exe",
        "pid": 100,
        "exe_path": "C:\\Tools\\app.exe",
        "sha256": "aabbcc",
        "start_time": "2026-09-15T09:00:00",
        "sample_time": "2026-09-15T09:05:00",
        "duration_seconds": 300,
        "rss_bytes": 200 * MiB,
        "unique_key": "PC1|100|2026-09-15T09:00:00|2026-09-15T09:05:00",
    }
    e.update(overrides)
    return e


# ------------------------------------------------------------- validate_event

class TestValidateEvent:
    def test_full_event_passes(self):
        validate_event(make_event())  # не бросает

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_missing_required_field_gives_400(self, field):
        e = make_event()
        del e[field]
        with pytest.raises(HTTPException) as exc:
            validate_event(e)
        assert exc.value.status_code == 400
        assert field in exc.value.detail

    def test_required_list_matches_code(self):
        # Страховка: если список обязательных полей в коде поменяется,
        # параметризация выше должна быть обновлена.
        e = {k: None for k in REQUIRED_FIELDS}
        validate_event(e)  # присутствие ключа достаточно, значение не проверяется


# -------------------------------------------------------------- insert_events

class TestInsertEvents:
    def test_unique_key_column_is_unique_in_schema(self, db):
        row = db.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()
        assert "unique_key TEXT NOT NULL UNIQUE" in row["sql"]

    def test_new_event_inserted(self, db):
        res = insert_events([make_event()])
        assert res == {"inserted": 1, "deduped": 0}
        rows = db.execute("SELECT * FROM events").fetchall()
        assert len(rows) == 1
        assert rows[0]["machine_name"] == "PC1"
        assert rows[0]["unique_key"] == "PC1|100|2026-09-15T09:00:00|2026-09-15T09:05:00"
        assert rows[0]["rss_bytes"] == 200 * MiB
        assert rows[0]["received_at"]  # заполняется сервером

    def test_duplicate_unique_key_is_deduped(self, db):
        first = insert_events([make_event()])
        second = insert_events([make_event(process_name="other.exe")])  # тот же unique_key
        assert first == {"inserted": 1, "deduped": 0}
        assert second == {"inserted": 0, "deduped": 1}
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    def test_mixed_batch(self, db):
        insert_events([make_event()])
        res = insert_events([make_event(), make_event(unique_key="another")])
        assert res == {"inserted": 1, "deduped": 1}

    def test_machine_state_is_upserted(self, db):
        insert_events([make_event(os_info="Windows 10", client_version="1.2.3")])
        row = db.execute("SELECT * FROM machine_state WHERE machine_name='PC1'").fetchone()
        assert row is not None
        assert row["os_info"] == "Windows 10"
        assert row["client_version"] == "1.2.3"
        assert row["last_seen"] == "2026-09-15T09:05:00"


# ------------------------------------------- detect_alerts_for_ingested_events
#
# Поток данных в функции (по коду):
#  1. из payload берётся самый свежий sample_time на сессию (machine, pid, start_time);
#     pid обязан быть ненулевым;
#  2. latest/prev-строки сессии читаются ИЗ БАЗЫ, поэтому событие надо сначала
#     вставить через insert_events;
#  3. rss берётся из latest; cpu_delta/io_delta требуют prev (второй сэмпл);
#     net_conn_count учитывается, только если net_active не 0/False;
#  4. baseline = _median(значений той же метрики за (user_name, sha256) в окне
#     [sample_time - 14 дней; sample_time) — сам текущий сэмпл в baseline не входит);
#  5. если точек baseline >= alert_baseline_min_points (20):
#        fired = value > baseline*K AND value > abs
#     иначе: fired = value > very_high_abs;
#  6. severity = _severity_from_ratio(value / baseline).
#
# Для rss: K=3, abs=500 MiB, very_high_abs=2 GiB.

BASELINE_RSS = 200 * MiB
SHA = "deadbeef"
USER = "alice"
MACHINE = "PC1"
PROC = "app.exe"


def _baseline_events(count=26):
    """count сессий, по одному сэмплу, в 13 днях до дня аномалии (2026-09-15)."""
    events = []
    for i in range(count):
        day = 2 + (i // 2)            # 2026-09-02 .. 2026-09-14
        pid = 1000 + i
        start = f"2026-09-{day:02d}T09:00:00"
        sample = f"2026-09-{day:02d}T09:{5 + (i % 2):02d}:00"
        events.append(make_event(
            pid=pid,
            start_time=start,
            sample_time=sample,
            rss_bytes=BASELINE_RSS,
            sha256=SHA,
            unique_key=f"{MACHINE}|{pid}|{start}|{sample}",
        ))
    return events


def _anomaly_event(rss_bytes):
    return make_event(
        pid=9999,
        start_time="2026-09-15T09:00:00",
        sample_time="2026-09-15T09:05:00",
        rss_bytes=rss_bytes,
        sha256=SHA,
        unique_key=f"{MACHINE}|9999|2026-09-15T09:00:00|2026-09-15T09:05:00",
    )


class TestDetectAlertsRss:
    def _run(self, db, rss_bytes):
        baseline = _baseline_events()
        assert len(baseline) >= server_app.ALERT_BASELINE_MIN_POINTS_DEFAULT
        assert insert_events(baseline)["inserted"] == len(baseline)
        # прогоняем baseline через детектор, чтобы он тоже был "как в проде";
        # эти события ниже very_high_abs и без baseline не дают rss-алертов
        detect_alerts_for_ingested_events(baseline)
        before = db.execute("SELECT COUNT(*) FROM alerts WHERE metric='rss'").fetchone()[0]
        assert before == 0

        anomaly = _anomaly_event(rss_bytes)
        assert insert_events([anomaly])["inserted"] == 1
        inserted = detect_alerts_for_ingested_events([anomaly])
        rows = db.execute(
            "SELECT * FROM alerts WHERE metric='rss' AND pid=9999"
        ).fetchall()
        return inserted, rows

    def test_rss_far_above_baseline_gives_high(self, db):
        value = 2560 * MiB  # ratio = 12.8 > 10 -> high; > baseline*3 и > 500 MiB
        inserted, rows = self._run(db, value)
        assert inserted >= 1
        assert len(rows) == 1
        a = rows[0]
        assert a["severity"] == "high"
        assert a["entity_type"] == "process_session"
        assert a["machine_name"] == MACHINE
        assert a["user_name"] == USER
        assert a["process_name"] == PROC
        assert a["sha256"] == SHA
        assert a["value"] == float(value)
        assert a["baseline"] == float(BASELINE_RSS)
        assert a["score"] == pytest.approx(value / BASELINE_RSS)
        assert a["status"] == "new"
        assert a["bucket_hour"] == "2026-09-15T09"
        assert a["dedup_key"] == f"{MACHINE}|{USER}|{SHA}|rss|2026-09-15T09"

    def test_rss_moderately_above_baseline_gives_med(self, db):
        value = 1 * GiB  # ratio = 5.12: >= 5 и <= 10 -> med
        inserted, rows = self._run(db, value)
        assert inserted >= 1
        assert len(rows) == 1
        assert rows[0]["severity"] == "med"
        assert rows[0]["score"] == pytest.approx(5.12)

    def test_rss_within_baseline_gives_no_rss_alert(self, db):
        value = 300 * MiB  # < baseline*3 (600 MiB) и < abs (500 MiB)
        _, rows = self._run(db, value)
        assert rows == []

    def test_same_hour_repeat_is_deduped_by_dedup_key(self, db):
        value = 2560 * MiB
        _, rows = self._run(db, value)
        assert len(rows) == 1
        # повторный прогон того же события: dedup_key уникален -> ничего не добавится
        again = detect_alerts_for_ingested_events([_anomaly_event(value)])
        assert again == 0
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE metric='rss' AND pid=9999").fetchone()[0] == 1


class TestDetectAlertsWithoutBaseline:
    def test_no_baseline_uses_very_high_abs(self, db):
        rule = ALERT_RULES["rss"]
        # без baseline срабатывает только very_high_abs; ratio=None -> severity "med"
        below = _anomaly_event(int(rule["very_high_abs"]))          # ровно порог: не строго больше
        insert_events([below])
        detect_alerts_for_ingested_events([below])
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE metric='rss'").fetchone()[0] == 0

        above = make_event(
            pid=7777,
            sha256=SHA,
            rss_bytes=int(rule["very_high_abs"]) + 1,
            unique_key="PC1|7777|2026-09-15T09:00:00|2026-09-15T09:05:00",
        )
        insert_events([above])
        detect_alerts_for_ingested_events([above])
        row = db.execute("SELECT * FROM alerts WHERE metric='rss' AND pid=7777").fetchone()
        assert row is not None
        assert row["severity"] == "med"
        assert row["baseline"] is None
        assert row["score"] is None

    def test_event_with_zero_pid_is_ignored(self, db):
        e = make_event(pid=0, rss_bytes=10 * GiB, unique_key="zero-pid")
        insert_events([e])
        assert detect_alerts_for_ingested_events([e]) == 0

    def test_empty_payload(self, db_path):
        assert detect_alerts_for_ingested_events([]) == 0
