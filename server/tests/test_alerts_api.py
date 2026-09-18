"""
GET /api/alerts: точечный фильтр по узлу дерева процессов — новые необязательные
параметры pid и start_time (точное совпадение с alerts.pid / alerts.start_time),
в связке с machine. Остальные фильтры не меняются.
"""

import pytest
import server_app
from fastapi.testclient import TestClient

API_KEY = "TEST_API_KEY"
H = {"X-API-Key": API_KEY}


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MONITORING_API_KEY", API_KEY)
    return TestClient(server_app.app)


def _insert_alert(db, *, machine="PC1", pid=100, start_time="2026-09-15T09:00:00", metric="chain_rarity",
                  entity_type="process_chain", created_at="2026-09-15T09:05:00", dedup_key=None):
    dedup_key = dedup_key or f"{machine}|{pid}|{start_time}|{metric}|{created_at}"
    db.execute(
        """INSERT INTO alerts(created_at, sample_time, machine_name, user_name, entity_type, process_name,
                              pid, start_time, metric, severity, reason, status, bucket_hour, dedup_key)
           VALUES(%s, %s, %s, 'alice', %s, 'p.exe', %s, %s, %s, 'med', 'r', 'new', '2026-09-15T09', %s)""",
        (created_at, created_at, machine, entity_type, pid, start_time, metric, dedup_key),
    )


def _ids(client, **params):
    r = client.get("/api/alerts", params=params, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    return sorted(a["id"] for a in body["items"]), body["total"]


class TestAlertsNodeFilter:
    def test_pid_and_start_time_filter_exact_session(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00")            # целевой
        _insert_alert(db, pid=100, start_time="2026-09-15T10:00:00")            # тот же pid, другая сессия
        _insert_alert(db, pid=200, start_time="2026-09-15T09:00:00")            # другой pid
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00", machine="PC2")  # другая машина
        target = db.execute(
            "SELECT id FROM alerts WHERE machine_name='PC1' AND pid=100 AND start_time='2026-09-15T09:00:00'"
        ).fetchone()["id"]

        ids, total = _ids(client, machine="PC1", pid=100, start_time="2026-09-15T09:00:00")
        assert ids == [target]
        assert total == 1

    def test_pid_alone_and_start_time_alone(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00")
        _insert_alert(db, pid=100, start_time="2026-09-15T10:00:00")
        _insert_alert(db, pid=200, start_time="2026-09-15T09:00:00")
        ids, total = _ids(client, pid=100)
        assert total == 2 and len(ids) == 2
        ids, total = _ids(client, start_time="2026-09-15T09:00:00")
        assert total == 2 and len(ids) == 2

    def test_no_match_gives_empty_list(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00")
        ids, total = _ids(client, machine="PC1", pid=100, start_time="2026-09-15T09:00:01")
        assert ids == [] and total == 0
        ids, total = _ids(client, machine="PC1", pid=101, start_time="2026-09-15T09:00:00")
        assert ids == [] and total == 0

    def test_without_new_params_behaviour_unchanged(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00")
        _insert_alert(db, pid=200, start_time="2026-09-15T09:00:00", entity_type="process_session", metric="rss")
        ids, total = _ids(client)
        assert total == 2 and len(ids) == 2
        ids, total = _ids(client, entity_type="process_chain")
        assert total == 1

    def test_node_filter_combines_with_other_filters(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00", metric="chain_rarity")
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00", metric="chain_depth_anomaly",
                      created_at="2026-09-15T09:06:00")
        _, total = _ids(client, machine="PC1", pid=100, start_time="2026-09-15T09:00:00")
        assert total == 2
        _, total = _ids(client, machine="PC1", pid=100, start_time="2026-09-15T09:00:00", metric="chain_depth_anomaly")
        assert total == 1

    def test_non_integer_pid_is_422(self, client):
        assert client.get("/api/alerts", params={"pid": "abc"}, headers=H).status_code == 422

    def test_pid_zero_is_a_real_filter_not_ignored(self, client, db):
        _insert_alert(db, pid=100, start_time="2026-09-15T09:00:00")
        _, total = _ids(client, pid=0)
        assert total == 0
