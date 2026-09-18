"""
Эпик 5, email-уведомления по алертам: series_key, сопоставление правил с алертом
(entity_types / metrics / severity_min / machine_names), trigger_mode repeat_count
с окном и без, resend_mode once / every_occurrence, CRUD правил и валидация,
graceful-failure при ненастроенном SMTP (ingest не падает, в журнал пишется failed).
Письма наружу не уходят: SMTP подменяется фейком.
"""

from datetime import datetime, timedelta, timezone

import pytest
import server_app
from fastapi.testclient import TestClient

API_KEY = "TEST_API_KEY"
H = {"X-API-Key": API_KEY}
MiB = 1024 * 1024
GiB = 1024 * MiB


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MONITORING_API_KEY", API_KEY)
    return TestClient(server_app.app)


@pytest.fixture(autouse=True)
def _no_smtp_env(monkeypatch):
    """По умолчанию SMTP не настроен (как в CI): реальные письма невозможны."""
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sent(monkeypatch):
    """Перехват отправки: фиксируем вызовы _send_alert_email, письма не уходят."""
    calls = []

    def fake(recipients, subject, body):
        calls.append({"recipients": list(recipients), "subject": subject, "body": body})

    monkeypatch.setattr(server_app, "_send_alert_email", fake)
    return calls


class FakeSMTP:
    """Заглушка smtplib.SMTP: протокол STARTTLS/login/sendmail без сети."""

    instances = []
    fail_with = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, list(recipients), message))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(server_app.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _now_iso(delta_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat(timespec="seconds")


def _alert(**over):
    """Строка алерта в том виде, в каком её собирает детектор (до dedup_key/id)."""
    created = over.pop("created_at", None) or _now_iso()
    a = {
        "created_at": created,
        "sample_time": created,
        "machine_name": "PC1",
        "user_name": "alice",
        "entity_type": "process_session",
        "process_name": "app.exe",
        "pid": 100,
        "start_time": "2026-09-15T09:00:00",
        "sha256": "aabbcc",
        "exe_path": "C:\\Tools\\app.exe",
        "parent_process_name": None,
        "parent_sha256": None,
        "parent_exe_path": None,
        "chain_key": None,
        "metric": "rss",
        "value": 1.0,
        "baseline": None,
        "score": None,
        "severity": "med",
        "reason": "тестовая причина",
        "status": "new",
        "bucket_hour": created[:13],
    }
    a.update(over)
    return a


def _emit(alert):
    """Вставка алерта + проверка правил — как в хвосте detect_alerts_for_ingested_events."""
    conn = server_app.db_connect()
    cur = conn.cursor()
    try:
        alert["dedup_key"] = server_app._make_alert_dedup_key(alert)
        ok = server_app._try_insert_alert(cur, alert)
        attempts = server_app.evaluate_and_send_notifications(cur, alert) if ok else 0
        conn.commit()
    finally:
        conn.close()
    return ok, attempts


def _rule(client, **over):
    payload = {
        "name": "r1",
        "recipients": ["ops@example.com"],
        "trigger_mode": "immediate",
        "resend_mode": "once",
    }
    payload.update(over)
    r = client.post("/api/alert-rule-item", json=payload, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _notifications(db, rule_id=None):
    if rule_id is None:
        return db.execute("SELECT * FROM alert_notifications ORDER BY id").fetchall()
    return db.execute("SELECT * FROM alert_notifications WHERE rule_id=%s ORDER BY id", (rule_id,)).fetchall()


# ------------------------------------------------------------------ series_key

class TestSeriesKey:
    def test_session_key_is_dedup_key_without_bucket_hour(self):
        a = _alert(bucket_hour="2026-09-15T09")
        dedup = server_app._make_alert_dedup_key(a)
        series = server_app._make_alert_series_key(a)
        assert dedup == f"{series}|2026-09-15T09"
        assert series == "PC1|alice|aabbcc|rss"

    def test_chain_key_uses_chain_key_not_binary(self):
        a = _alert(entity_type="process_chain", chain_key="parent->child", metric="chain_rarity")
        assert server_app._make_alert_series_key(a) == "PC1|alice|parent->child|chain_rarity"

    def test_binary_fallback_to_path_then_name(self):
        assert server_app._make_alert_series_key(_alert(sha256=None)) == "PC1|alice|path:c:\\tools\\app.exe|rss"
        assert server_app._make_alert_series_key(_alert(sha256=None, exe_path=None)) == "PC1|alice|name:app.exe|rss"

    def test_schema_has_tables_and_index(self, db):
        cols = {r["column_name"] for r in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='alert_notification_rules'"
        ).fetchall()}
        assert {"id", "name", "enabled", "entity_types", "metrics", "severity_min", "machine_names", "trigger_mode",
                "repeat_threshold", "repeat_window_minutes", "resend_mode", "recipients", "created_at",
                "updated_at"} <= cols
        idx = db.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename='alert_notifications' "
            "AND indexname='idx_alert_notifications_rule_series'"
        ).fetchone()
        assert idx and "rule_id, series_key" in idx["indexdef"]
        server_app.ensure_schema()  # идемпотентно


# --------------------------------------------------------------- matching

class TestRuleMatching:
    def test_empty_filters_match_anything(self, client, db, sent):
        rid = _rule(client)
        assert _emit(_alert()) == (True, 1)
        assert len(sent) == 1
        rows = _notifications(db, rid)
        assert len(rows) == 1 and rows[0]["status"] == "sent" and rows[0]["error"] is None
        assert rows[0]["series_key"] == "PC1|alice|aabbcc|rss"

    def test_disabled_rule_is_ignored(self, client, db, sent):
        _rule(client, enabled=False)
        assert _emit(_alert()) == (True, 0)
        assert sent == [] and _notifications(db) == []

    def test_entity_types_filter(self, client, sent):
        _rule(client, entity_types=["process_chain"])
        _emit(_alert())
        assert sent == []
        _emit(_alert(entity_type="process_chain", chain_key="a->b", metric="chain_rarity", pid=101))
        assert len(sent) == 1

    def test_metrics_filter(self, client, sent):
        _rule(client, metrics=["cpu_delta", "io_delta"])
        _emit(_alert(metric="rss"))
        assert sent == []
        _emit(_alert(metric="io_delta"))
        assert len(sent) == 1

    def test_machine_names_filter(self, client, sent):
        _rule(client, machine_names=["PC2", "PC3"])
        _emit(_alert(machine_name="PC1"))
        assert sent == []
        _emit(_alert(machine_name="PC3"))
        assert len(sent) == 1
        assert "PC3" in sent[0]["subject"]

    @pytest.mark.parametrize("severity_min,severity,expected", [
        ("med", "low", 0), ("med", "med", 1), ("med", "high", 1),
        ("high", "med", 0), ("high", "high", 1), ("low", "low", 1), (None, "low", 1),
    ])
    def test_severity_min(self, client, sent, severity_min, severity, expected):
        _rule(client, severity_min=severity_min)
        _emit(_alert(severity=severity))
        assert len(sent) == expected

    def test_all_filters_combined(self, client, sent):
        _rule(client, entity_types=["process_session"], metrics=["rss"], severity_min="med", machine_names=["PC1"])
        _emit(_alert(machine_name="PC1", metric="rss", severity="high"))
        assert len(sent) == 1
        _emit(_alert(machine_name="PC1", metric="rss", severity="low", pid=2))       # severity ниже
        _emit(_alert(machine_name="PC2", metric="rss", severity="high", pid=3))      # чужая машина
        _emit(_alert(machine_name="PC1", metric="cpu_delta", severity="high", pid=4))  # чужая метрика
        assert len(sent) == 1

    def test_several_rules_each_get_own_record(self, client, db, sent):
        r1 = _rule(client, name="a", recipients=["a@example.com"])
        r2 = _rule(client, name="b", recipients=["b@example.com"], metrics=["cpu_delta"])
        r3 = _rule(client, name="c", recipients=["c@example.com"])
        _emit(_alert())
        assert sorted(x["recipients"][0] for x in sent) == ["a@example.com", "c@example.com"]
        assert sorted(n["rule_id"] for n in _notifications(db)) == sorted([r1, r3])
        assert _notifications(db, r2) == []

    def test_email_content(self, client, sent, monkeypatch):
        monkeypatch.setenv("MONITORING_DASHBOARD_URL", "http://mon.local:8000/")
        _rule(client, name="Правило X")
        _emit(_alert(severity="high", metric="rss", machine_name="WS-7", reason="RSS выше базового"))
        assert sent[0]["subject"] == "[monitoring] high rss WS-7"
        body = sent[0]["body"]
        for piece in ("Правило X", "Уровень: high", "Метрика: rss", "Машина: WS-7", "Процесс: app.exe",
                      "RSS выше базового", "http://mon.local:8000/"):
            assert piece in body

    def test_email_content_chain_and_no_link(self, client, sent, monkeypatch):
        monkeypatch.delenv("MONITORING_DASHBOARD_URL", raising=False)
        _rule(client)
        _emit(_alert(entity_type="process_chain", parent_process_name="explorer.exe", process_name="cmd.exe",
                     chain_key="k1", metric="chain_rarity"))
        body = sent[0]["body"]
        assert "Цепочка: explorer.exe -> cmd.exe (k1)" in body
        assert "Дашборд" not in body


# ------------------------------------------------------------ repeat_count

class TestRepeatCount:
    def test_threshold_without_window_counts_all_history(self, client, db, sent):
        rid = _rule(client, trigger_mode="repeat_count", repeat_threshold=3, repeat_window_minutes=None)
        # три часа назад, два часа назад — разные bucket_hour, одна серия
        _emit(_alert(created_at=_now_iso(-180)))
        _emit(_alert(created_at=_now_iso(-120)))
        assert sent == [] and _notifications(db, rid) == []
        _emit(_alert(created_at=_now_iso()))
        assert len(sent) == 1
        assert _notifications(db, rid)[0]["status"] == "sent"

    def test_window_excludes_old_alerts(self, client, sent):
        """Окно отсчитывается назад от created_at текущего алерта. bucket_hour задан явно:
        в пределах одного часа серия дедуплицируется, и алерт не вставился бы вовсе."""
        _rule(client, trigger_mode="repeat_count", repeat_threshold=3, repeat_window_minutes=60)
        _emit(_alert(created_at=_now_iso(-180), bucket_hour="b1"))
        _emit(_alert(created_at=_now_iso(-120), bucket_hour="b2"))
        assert sent == []
        _emit(_alert(created_at=_now_iso(-100), bucket_hour="b3"))   # окно [-160, -100]: -120, -100 -> 2
        assert sent == []
        _emit(_alert(created_at=_now_iso(-90), bucket_hour="b4"))    # окно [-150, -90]: -120, -100, -90 -> 3
        assert len(sent) == 1
        _emit(_alert(created_at=_now_iso(-20), bucket_hour="b5"))    # окно [-80, -20]: только текущий -> 1
        assert len(sent) == 1

    def test_other_series_not_counted(self, client, sent):
        _rule(client, trigger_mode="repeat_count", repeat_threshold=2)
        _emit(_alert(machine_name="PC1", created_at=_now_iso(-60)))
        _emit(_alert(machine_name="PC2", created_at=_now_iso()))     # другая машина = другая серия
        _emit(_alert(metric="cpu_delta", created_at=_now_iso(-1)))   # другая метрика
        assert sent == []
        _emit(_alert(machine_name="PC1", created_at=_now_iso()))
        assert len(sent) == 1

    def test_series_prefix_is_exact_not_like(self, client, sent):
        # путь с '_' и '%' не должен ломать подсчёт серии (сравнение по префиксу, не LIKE)
        _rule(client, trigger_mode="repeat_count", repeat_threshold=2)
        _emit(_alert(sha256=None, exe_path="C:\\a_b%\\app.exe", created_at=_now_iso(-60)))
        _emit(_alert(sha256=None, exe_path="C:\\a_b%\\app.exe", created_at=_now_iso()))
        assert len(sent) == 1

    def test_immediate_rule_sends_on_first_alert(self, client, sent):
        _rule(client, trigger_mode="immediate")
        _emit(_alert())
        assert len(sent) == 1


# -------------------------------------------------------------- resend_mode

class TestResendMode:
    def test_once_sends_only_first_time_per_series(self, client, db, sent):
        rid = _rule(client, resend_mode="once")
        _emit(_alert(created_at=_now_iso(-120)))
        _emit(_alert(created_at=_now_iso()))       # та же серия, другой час
        assert len(sent) == 1
        assert len(_notifications(db, rid)) == 1
        _emit(_alert(metric="cpu_delta"))          # другая серия -> письмо
        assert len(sent) == 2

    def test_every_occurrence_sends_each_time(self, client, db, sent):
        rid = _rule(client, resend_mode="every_occurrence")
        _emit(_alert(created_at=_now_iso(-120)))
        _emit(_alert(created_at=_now_iso()))
        assert len(sent) == 2
        assert [n["status"] for n in _notifications(db, rid)] == ["sent", "sent"]

    def test_once_retries_after_failed_attempt(self, client, db, monkeypatch):
        """Подавляет повтор только успешная отправка: после failed следующий алерт серии снова шлётся."""
        rid = _rule(client, resend_mode="once")
        _emit(_alert(created_at=_now_iso(-120)))  # SMTP не настроен -> failed
        assert [n["status"] for n in _notifications(db, rid)] == ["failed"]
        calls = []
        monkeypatch.setattr(server_app, "_send_alert_email", lambda r, s, b: calls.append(s))
        _emit(_alert(created_at=_now_iso()))
        assert len(calls) == 1
        assert [n["status"] for n in _notifications(db, rid)] == ["failed", "sent"]

    def test_once_is_per_rule(self, client, db, sent):
        r1 = _rule(client, name="a")
        _emit(_alert(created_at=_now_iso(-120)))
        r2 = _rule(client, name="b")
        _emit(_alert(created_at=_now_iso()))
        assert len(_notifications(db, r1)) == 1
        assert len(_notifications(db, r2)) == 1
        assert len(sent) == 2

    def test_duplicate_alert_same_hour_does_not_notify(self, client, sent):
        _rule(client, resend_mode="every_occurrence")
        a = _alert()
        assert _emit(dict(a)) == (True, 1)
        assert _emit(dict(a)) == (False, 0)  # dedup_key -> не вставлен -> правила не проверяются
        assert len(sent) == 1


# --------------------------------------------------------------------- CRUD

class TestRulesCrud:
    def test_requires_api_key(self, client):
        assert client.get("/api/alert-rules").status_code == 401
        assert client.post("/api/alert-rule-item", json={}).status_code == 401
        assert client.post("/api/alert-rule-item/1/delete").status_code == 401
        assert client.get("/api/alert-rules/1/notifications").status_code == 401

    def test_create_list_update_delete(self, client, db):
        rid = _rule(client, name="n1", entity_types=["process_chain"], metrics=["chain_rarity", "rss"],
                    severity_min="med", machine_names=["PC1"], trigger_mode="repeat_count",
                    repeat_threshold=3, repeat_window_minutes=30, resend_mode="every_occurrence",
                    recipients=["a@example.com", "b@example.com"])
        items = client.get("/api/alert-rules", headers=H).json()["items"]
        assert len(items) == 1
        r = items[0]
        assert r["id"] == rid and r["name"] == "n1" and r["enabled"] is True
        assert r["entity_types"] == ["process_chain"]
        assert r["metrics"] == ["chain_rarity", "rss"]
        assert r["severity_min"] == "med"
        assert r["machine_names"] == ["PC1"]
        assert r["trigger_mode"] == "repeat_count" and r["repeat_threshold"] == 3 and r["repeat_window_minutes"] == 30
        assert r["resend_mode"] == "every_occurrence"
        assert r["recipients"] == ["a@example.com", "b@example.com"]
        assert r["created_at"] and r["updated_at"]

        # update: id в payload -> UPDATE той же строки; фильтры сбрасываются в NULL
        resp = client.post("/api/alert-rule-item", json={
            "id": rid, "name": "n2", "enabled": False, "recipients": "c@example.com",
            "trigger_mode": "immediate", "repeat_threshold": 5,
        }, headers=H)
        assert resp.status_code == 200 and resp.json()["id"] == rid
        items = client.get("/api/alert-rules", headers=H).json()["items"]
        assert len(items) == 1
        r = items[0]
        assert r["name"] == "n2" and r["enabled"] is False and r["recipients"] == ["c@example.com"]
        assert r["entity_types"] is None and r["metrics"] is None and r["machine_names"] is None
        assert r["severity_min"] is None
        assert r["trigger_mode"] == "immediate" and r["repeat_threshold"] is None
        assert r["updated_at"] >= r["created_at"]

        assert client.post(f"/api/alert-rule-item/{rid}/delete", headers=H).status_code == 200
        assert client.get("/api/alert-rules", headers=H).json()["items"] == []
        assert client.post(f"/api/alert-rule-item/{rid}/delete", headers=H).status_code == 404

    def test_update_unknown_id_is_404(self, client):
        resp = client.post("/api/alert-rule-item", json={
            "id": 999, "name": "x", "recipients": ["a@example.com"], "trigger_mode": "immediate",
        }, headers=H)
        assert resp.status_code == 404

    def test_recipients_as_comma_string_and_empty_lists_become_null(self, client):
        rid = _rule(client, recipients=" a@example.com , b@example.com ", entity_types=[], metrics="", machine_names=None)
        r = client.get("/api/alert-rules", headers=H).json()["items"][0]
        assert r["id"] == rid
        assert r["recipients"] == ["a@example.com", "b@example.com"]
        assert r["entity_types"] is None and r["metrics"] is None and r["machine_names"] is None

    @pytest.mark.parametrize("bad,detail", [
        ({"name": ""}, "name"),
        ({"recipients": []}, "recipients"),
        ({"recipients": ""}, "recipients"),
        ({"recipients": ["not-an-email"]}, "invalid email"),
        ({"recipients": ["ok@example.com", "bad@"]}, "invalid email"),
        ({"trigger_mode": ""}, "trigger_mode"),
        ({"trigger_mode": "sometimes"}, "trigger_mode"),
        ({"trigger_mode": "repeat_count"}, "repeat_threshold"),
        ({"trigger_mode": "repeat_count", "repeat_threshold": 1}, "repeat_threshold"),
        ({"trigger_mode": "repeat_count", "repeat_threshold": "abc"}, "repeat_threshold"),
        ({"trigger_mode": "repeat_count", "repeat_threshold": 2, "repeat_window_minutes": 0}, "repeat_window_minutes"),
        ({"resend_mode": "never"}, "resend_mode"),
        ({"severity_min": "critical"}, "severity_min"),
    ])
    def test_validation(self, client, bad, detail):
        payload = {"name": "r", "recipients": ["ops@example.com"], "trigger_mode": "immediate"}
        payload.update(bad)
        resp = client.post("/api/alert-rule-item", json=payload, headers=H)
        assert resp.status_code == 400, resp.text
        assert detail in resp.json()["detail"]
        assert client.get("/api/alert-rules", headers=H).json()["items"] == []

    def test_notifications_endpoint(self, client, db, sent):
        rid = _rule(client, resend_mode="every_occurrence")
        for i in range(3):
            _emit(_alert(created_at=_now_iso(-60 * i), machine_name=f"PC{i}"))
        resp = client.get(f"/api/alert-rules/{rid}/notifications", params={"limit": 2}, headers=H)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        assert items[0]["id"] > items[1]["id"]  # новые первыми
        assert items[0]["status"] == "sent" and items[0]["rule_id"] == rid
        assert items[0]["machine_name"] == "PC2" and items[0]["metric"] == "rss"
        assert client.get("/api/alert-rules/999/notifications", headers=H).status_code == 404

    def test_delete_rule_cascades_notifications(self, client, db, sent):
        rid = _rule(client)
        _emit(_alert())
        assert len(_notifications(db)) == 1
        client.post(f"/api/alert-rule-item/{rid}/delete", headers=H)
        assert _notifications(db) == []
        # сам алерт остаётся
        assert db.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"] == 1


# ------------------------------------------------------ SMTP / graceful failure

def _ingest_event(pid=7777, rss_bytes=None):
    """Событие для /api/ingest: rss > very_high_abs без baseline -> алерт rss (med)."""
    rss = rss_bytes if rss_bytes is not None else int(server_app.ALERT_RULES["rss"]["very_high_abs"]) + 1
    start, sample = "2026-09-15T09:00:00", "2026-09-15T09:05:00"
    return {
        "machine_name": "PC1", "user_name": "alice", "process_name": "app.exe", "pid": pid,
        "exe_path": "C:\\Tools\\app.exe", "sha256": "aabbcc",
        "start_time": start, "sample_time": sample, "duration_seconds": 300,
        "rss_bytes": rss, "unique_key": f"PC1|{pid}|{start}|{sample}",
    }


class TestSmtpGracefulFailure:
    def test_ingest_ok_when_smtp_not_configured(self, client, db):
        rid = _rule(client, metrics=["rss"])
        resp = client.post("/api/ingest", json=[_ingest_event()], headers=H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True and resp.json()["alerts_inserted"] >= 1
        alert = db.execute("SELECT * FROM alerts WHERE metric='rss' AND pid=7777").fetchone()
        assert alert is not None
        rows = _notifications(db, rid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "SMTP не настроен"
        assert rows[0]["alert_id"] == alert["id"]
        assert rows[0]["series_key"] == "PC1|alice|aabbcc|rss"

    def test_partial_smtp_env_is_not_configured(self, client, db, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")   # без SMTP_FROM
        _rule(client)
        _emit(_alert())
        rows = _notifications(db)
        assert rows[0]["status"] == "failed" and rows[0]["error"] == "SMTP не настроен"

    def test_ingest_ok_when_smtp_raises(self, client, db, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "mon@example.com")
        fake_smtp.fail_with = ConnectionRefusedError("connection refused")
        rid = _rule(client, metrics=["rss"])
        resp = client.post("/api/ingest", json=[_ingest_event()], headers=H)
        assert resp.status_code == 200
        assert resp.json()["alerts_inserted"] >= 1
        rows = _notifications(db, rid)
        assert len(rows) == 1 and rows[0]["status"] == "failed"
        assert "connection refused" in rows[0]["error"]

    def test_notification_error_does_not_break_detector(self, client, db, monkeypatch):
        """Исключение внутри evaluate_and_send_notifications гасится, алерт остаётся, ingest 200."""
        _rule(client, metrics=["rss"])

        def boom(cur, alert):
            raise RuntimeError("boom")

        monkeypatch.setattr(server_app, "evaluate_and_send_notifications", boom)
        resp = client.post("/api/ingest", json=[_ingest_event()], headers=H)
        assert resp.status_code == 200 and resp.json()["alerts_inserted"] >= 1
        assert db.execute("SELECT COUNT(*) AS c FROM alerts WHERE metric='rss' AND pid=7777").fetchone()["c"] == 1

    def test_smtp_success_path(self, client, db, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "2525")
        monkeypatch.setenv("SMTP_USER", "mailer")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("SMTP_FROM", "mon@example.com")
        rid = _rule(client, metrics=["rss"], recipients=["a@example.com", "b@example.com"])
        resp = client.post("/api/ingest", json=[_ingest_event()], headers=H)
        assert resp.status_code == 200
        rows = _notifications(db, rid)
        assert len(rows) == 1 and rows[0]["status"] == "sent" and rows[0]["error"] is None

        assert len(fake_smtp.instances) == 1
        s = fake_smtp.instances[0]
        assert (s.host, s.port) == ("smtp.example.com", 2525)
        names = [c[0] for c in s.calls]
        assert names == ["ehlo", "starttls", "ehlo", "login", "sendmail"]
        assert s.calls[3] == ("login", "mailer")
        _, sender, to, message = s.calls[4]
        assert sender == "mon@example.com" and to == ["a@example.com", "b@example.com"]
        assert "To: a@example.com, b@example.com" in message
        assert "Subject:" in message

    def test_smtp_without_credentials_skips_login(self, client, db, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "mon@example.com")
        _rule(client)
        _emit(_alert())
        s = fake_smtp.instances[0]
        assert s.port == server_app.SMTP_PORT_DEFAULT
        assert [c[0] for c in s.calls] == ["ehlo", "starttls", "ehlo", "sendmail"]
        assert _notifications(db)[0]["status"] == "sent"
