"""
Эпик 3, фаза A: дерево процессов, эвристики глубины/fan-out, attack_technique_id.

- индекс idx_events_ppid и колонка chain_catalog.attack_technique_id создаются ensure_schema (идемпотентно);
- GET /api/analytics/process-tree: предки + потомки одним WITH RECURSIVE с условием
  временного перекрытия; переиспользованный pid не смешивает сессии; потолок глубины
  помечается truncated;
- chain_depth_anomaly / chain_fanout_anomaly: создаются в ожидаемых случаях и не создаются
  при недостаточном baseline / в пределах нормы;
- attack_technique_id пишется и читается через /api/chain-catalog-item и /api/chain-catalog.
"""

import pytest
import server_app
from fastapi.testclient import TestClient
from server_app import detect_alerts_for_ingested_events, insert_events

API_KEY = "TEST_API_KEY"
H = {"X-API-Key": API_KEY}
M = "PC1"
DAY = "2026-09-15"


def ts(hhmm: str, day: str = DAY) -> str:
    return f"{day}T{hhmm}:00"


def ev(pid, ppid, name, start, sample, end=None, machine=M, user="alice", sha=None):
    return {
        "machine_name": machine,
        "user_name": user,
        "process_name": name,
        "pid": pid,
        "ppid": ppid,
        "exe_path": f"C:\\bin\\{name}",
        "sha256": sha if sha is not None else f"sha_{name}",
        "start_time": start,
        "sample_time": sample,
        "end_time": end,
        "duration_seconds": 60,
        "rss_bytes": 10 * 1024 * 1024,
        "unique_key": f"{machine}|{pid}|{start}|{sample}",
    }


def nid(pid, start):
    return f"{pid}|{start}"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MONITORING_API_KEY", API_KEY)
    return TestClient(server_app.app)


def get_tree(client, **params):
    r = client.get("/api/analytics/process-tree", params=params, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ схема

class TestSchema:
    def test_ppid_index_exists_and_schema_is_idempotent(self, db):
        server_app.ensure_schema()
        server_app.ensure_schema()
        row = db.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename='events' AND indexname='idx_events_ppid'"
        ).fetchone()
        assert row is not None
        assert "(machine_name, ppid)" in row["indexdef"]

    def test_chain_catalog_has_attack_technique_id(self, db):
        row = db.execute(
            """SELECT data_type, is_nullable FROM information_schema.columns
               WHERE table_name='chain_catalog' AND column_name='attack_technique_id'"""
        ).fetchone()
        assert row is not None
        assert row["data_type"] == "text"
        assert row["is_nullable"] == "YES"

    def test_events_still_insert_with_index(self, db):
        assert insert_events([ev(1, 0, "root.exe", ts("09:00"), ts("09:01"))])["inserted"] == 1


# ------------------------------------------------------- дерево процессов

OLD_END = ts("09:10")


def _pid_reuse_events(old_end=OLD_END):
    """
    root(1) -> old.exe(pid 500, 09:02..old_end) -> c1(600 @09:05)
    root(1) -> new.exe(pid 500, 09:20..)        -> c2(700 @09:25) -> c3(800 @09:30)
    """
    return [
        ev(1, 0, "root.exe", ts("09:00"), ts("09:01")),
        ev(500, 1, "old.exe", ts("09:02"), ts("09:03")),
        ev(500, 1, "old.exe", ts("09:02"), old_end or ts("09:09"), end=old_end),
        ev(600, 500, "c1.exe", ts("09:05"), ts("09:06")),
        ev(500, 1, "new.exe", ts("09:20"), ts("09:21")),
        ev(700, 500, "c2.exe", ts("09:25"), ts("09:26")),
        ev(800, 700, "c3.exe", ts("09:30"), ts("09:31")),
    ]


class TestProcessTree:
    def test_requires_api_key(self, client):
        r = client.get("/api/analytics/process-tree", params={"machine_name": M, "pid": 1, "start_time": ts("09:00")})
        assert r.status_code == 401

    def test_missing_coordinates_is_400(self, client):
        assert client.get("/api/analytics/process-tree", params={"machine_name": M}, headers=H).status_code == 400

    def test_unknown_session_is_404(self, client):
        r = client.get("/api/analytics/process-tree", params={"machine_name": M, "pid": 1, "start_time": ts("09:00")}, headers=H)
        assert r.status_code == 404

    def test_descendants_and_ancestors_with_pid_reuse(self, client):
        insert_events(_pid_reuse_events())

        # от корня видно обе сессии pid 500, дети привязаны к "своей" сессии
        t = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"))
        assert t["truncated"] is False
        by_id = {n["node_id"]: n for n in t["nodes"]}
        assert by_id[nid(1, ts("09:00"))]["is_root"] is True
        assert by_id[nid(1, ts("09:00"))]["level"] == 0
        assert set(by_id[nid(1, ts("09:00"))]["children_ids"]) == {nid(500, ts("09:02")), nid(500, ts("09:20"))}
        assert by_id[nid(500, ts("09:02"))]["children_ids"] == [nid(600, ts("09:05"))]
        assert by_id[nid(500, ts("09:20"))]["children_ids"] == [nid(700, ts("09:25"))]
        assert by_id[nid(600, ts("09:05"))]["parent_id"] == nid(500, ts("09:02"))
        assert by_id[nid(700, ts("09:25"))]["parent_id"] == nid(500, ts("09:20"))
        assert by_id[nid(800, ts("09:30"))]["level"] == 3
        assert by_id[nid(500, ts("09:02"))]["end_time"] == ts("09:10")
        assert by_id[nid(500, ts("09:02"))]["process_name"] == "old.exe"
        # поля узла, нужные фазе B
        for k in ("machine_name", "process_name", "pid", "ppid", "start_time", "end_time",
                  "sha256", "exe_path", "sample_time", "parent_id", "children_ids", "level"):
            assert k in by_id[nid(800, ts("09:30"))]

        # поддерево старой сессии pid 500 не содержит детей новой
        t_old = get_tree(client, machine_name=M, pid=500, start_time=ts("09:02"))
        ids = {n["node_id"] for n in t_old["nodes"]}
        assert nid(600, ts("09:05")) in ids
        assert nid(700, ts("09:25")) not in ids

        # предки c3: new.exe (не old.exe) -> root
        t_leaf = get_tree(client, machine_name=M, pid=800, start_time=ts("09:30"))
        assert t_leaf["ancestor_ids"] == [nid(700, ts("09:25")), nid(500, ts("09:20")), nid(1, ts("09:00"))]
        levels = {n["node_id"]: n["level"] for n in t_leaf["nodes"]}
        assert levels[nid(1, ts("09:00"))] == -3
        assert levels[nid(800, ts("09:30"))] == 0
        assert nid(500, ts("09:02")) not in levels

    def test_pid_reuse_when_old_end_time_unknown(self, client):
        # end_time старой сессии не записан: выбирается самая поздняя сессия
        # с этим pid, начавшаяся не позже потомка
        insert_events(_pid_reuse_events(old_end=None))
        t = get_tree(client, machine_name=M, pid=700, start_time=ts("09:25"))
        assert t["ancestor_ids"][0] == nid(500, ts("09:20"))
        t_old = get_tree(client, machine_name=M, pid=500, start_time=ts("09:02"))
        assert {n["node_id"] for n in t_old["nodes"]} == {nid(1, ts("09:00")), nid(500, ts("09:02")), nid(600, ts("09:05"))}

    def test_child_started_before_parent_is_not_attached(self, client):
        insert_events([
            ev(1, 0, "root.exe", ts("09:00"), ts("09:01")),
            ev(500, 1, "p.exe", ts("09:20"), ts("09:21")),
            ev(600, 500, "orphan.exe", ts("09:10"), ts("09:11")),  # стартовал до родителя с этим pid
        ])
        t = get_tree(client, machine_name=M, pid=500, start_time=ts("09:20"))
        assert [n["node_id"] for n in t["nodes"] if n["level"] > 0] == []
        t2 = get_tree(client, machine_name=M, pid=600, start_time=ts("09:10"))
        assert t2["ancestor_ids"] == []

    def test_other_machine_is_isolated(self, client):
        insert_events([
            ev(1, 0, "root.exe", ts("09:00"), ts("09:01")),
            ev(500, 1, "p.exe", ts("09:02"), ts("09:03"), machine="PC2"),
        ])
        t = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"))
        assert [n["node_id"] for n in t["nodes"]] == [nid(1, ts("09:00"))]

    @staticmethod
    def _chain(length: int):
        # pid i+1 <- pid i, старт каждые 1 минуту
        events = [ev(1, 0, "root.exe", ts("09:00"), ts("09:59"))]
        for i in range(2, length + 1):
            mm = f"{(i % 60):02d}"
            hh = f"{9 + i // 60:02d}"
            events.append(ev(i, i - 1, f"p{i}.exe", f"{DAY}T{hh}:{mm}:00", f"{DAY}T{hh}:{mm}:30"))
        return events

    def test_depth_cap_param_marks_truncated(self, client):
        insert_events(self._chain(6))
        t = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"), max_depth=3)
        assert t["max_depth"] == 3
        assert t["truncated"] is True and t["descendants_truncated"] is True and t["ancestors_truncated"] is False
        assert max(n["level"] for n in t["nodes"]) == 3
        # вверх от листа
        leaf_start = next(e for e in self._chain(6) if e["pid"] == 6)["start_time"]
        t_up = get_tree(client, machine_name=M, pid=6, start_time=leaf_start, max_depth=3)
        assert t_up["ancestors_truncated"] is True and t_up["descendants_truncated"] is False
        assert len(t_up["ancestor_ids"]) == 3

    def test_default_ceiling_is_respected(self, client, monkeypatch):
        # цепочка длиннее потолка: обход останавливается на потолке и помечает truncated
        monkeypatch.setattr(server_app, "PROCESS_TREE_MAX_DEPTH_DEFAULT", 8)
        insert_events(self._chain(12))
        t = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"))
        assert t["max_depth"] == 8
        assert t["descendants_truncated"] is True
        assert len(t["nodes"]) == 9  # корень + 8 уровней
        # запросить больше потолка нельзя
        t2 = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"), max_depth=50)
        assert t2["max_depth"] == 8

    def test_full_chain_without_cap_is_not_truncated(self, client):
        insert_events(self._chain(12))
        t = get_tree(client, machine_name=M, pid=1, start_time=ts("09:00"))
        assert t["truncated"] is False
        assert len(t["nodes"]) == 12

    def test_root_by_alert_id(self, client, db):
        insert_events(_pid_reuse_events())
        db.execute(
            """INSERT INTO alerts(created_at, sample_time, machine_name, user_name, entity_type, process_name,
                                  pid, start_time, metric, severity, reason, status, bucket_hour, dedup_key)
               VALUES(%s,%s,%s,%s,'process_chain','c2.exe',%s,%s,'chain_rarity','med','r','new','2026-09-15T09','k1')""",
            (ts("09:26"), ts("09:26"), M, "alice", 700, ts("09:25")),
        )
        alert_id = db.execute("SELECT id FROM alerts").fetchone()["id"]
        t = get_tree(client, alert_id=alert_id)
        assert t["root"] == {"node_id": nid(700, ts("09:25")), "machine_name": M, "pid": 700, "start_time": ts("09:25")}
        assert t["ancestor_ids"] == [nid(500, ts("09:20")), nid(1, ts("09:00"))]
        assert client.get("/api/analytics/process-tree", params={"alert_id": alert_id + 100}, headers=H).status_code == 404


# ---------------------------------------------------- chain_depth_anomaly

ROOT_SHA = "sha_root"


def _root_with_shallow_children(n: int, day="2026-09-10"):
    """Корень (sha_root) и n прямых детей (глубина 1) в прошлые дни."""
    events = [ev(1, 0, "root.exe", ts("08:00", day), ts("08:01", day), sha=ROOT_SHA)]
    for i in range(n):
        events.append(ev(100 + i, 1, "shallow.exe", ts(f"08:{i + 1:02d}", day), ts(f"08:{i + 1:02d}", day)))
    return events


def _deep_chain(depth: int):
    """Цепочка root(1) -> d1 -> d2 -> ... -> d<depth> (глубина листа = depth) в день DAY."""
    events = []
    ppid = 1
    for i in range(1, depth + 1):
        pid = 200 + i
        events.append(ev(pid, ppid, f"d{i}.exe", ts(f"09:{i:02d}"), ts(f"09:{i + 1:02d}")))
        ppid = pid
    return events


class TestChainDepthAnomaly:
    def _alerts(self, db):
        return db.execute("SELECT * FROM alerts WHERE metric='chain_depth_anomaly' ORDER BY id").fetchall()

    def test_deep_chain_fires_only_beyond_margin(self, db):
        base = _root_with_shallow_children(12)
        insert_events(base)
        detect_alerts_for_ingested_events(base)
        assert self._alerts(db) == []

        chain = _deep_chain(4)   # baseline max=1, порог 1+2=3: d4 (глубина 4) — алерт, d3 — нет
        insert_events(chain)
        detect_alerts_for_ingested_events(chain)
        rows = self._alerts(db)
        assert [r["process_name"] for r in rows] == ["d4.exe"]
        a = rows[0]
        assert a["entity_type"] == "process_chain"
        assert a["value"] == 4.0
        assert a["baseline"] == 1.0
        assert a["score"] == 3.0
        assert a["severity"] == "med"
        assert a["parent_process_name"] == "d3.exe"
        assert a["chain_key"] == "sha_d3.exe -> sha_d4.exe"
        assert a["dedup_key"] == f"{M}|alice|sha_d3.exe -> sha_d4.exe|chain_depth_anomaly|2026-09-15T09"
        assert "root.exe" in a["reason"]

    def test_much_deeper_chain_is_high(self, db):
        base = _root_with_shallow_children(12)
        insert_events(base)
        chain = _deep_chain(7)   # excess = 6 > 2*margin
        insert_events(chain)
        detect_alerts_for_ingested_events(chain)
        rows = self._alerts(db)
        assert rows[-1]["process_name"] == "d7.exe"
        assert rows[-1]["severity"] == "high"

    def test_not_enough_baseline_sessions_gives_no_alert(self, db):
        base = _root_with_shallow_children(server_app.CHAIN_DEPTH_MIN_SESSIONS_DEFAULT - 1)
        insert_events(base)
        chain = _deep_chain(6)
        insert_events(chain)
        detect_alerts_for_ingested_events(chain)
        assert self._alerts(db) == []

    def test_depth_within_typical_max_gives_no_alert(self, db):
        base = _root_with_shallow_children(12)
        # плюс одна легитимная глубокая цепочка в прошлом (глубина 5) — поднимает baseline
        old_chain = []
        ppid = 1
        for i in range(1, 6):
            old_chain.append(ev(300 + i, ppid, f"o{i}.exe", ts(f"10:{i:02d}", "2026-09-10"), ts(f"10:{i + 1:02d}", "2026-09-10")))
            ppid = 300 + i
        insert_events(base + old_chain)
        chain = _deep_chain(6)   # 6 > 5+2? нет
        insert_events(chain)
        detect_alerts_for_ingested_events(chain)
        assert self._alerts(db) == []

    def test_second_similar_chain_is_deduped_by_hour(self, db):
        base = _root_with_shallow_children(12)
        insert_events(base)
        chain = _deep_chain(4)
        insert_events(chain)
        detect_alerts_for_ingested_events(chain)
        assert len(self._alerts(db)) == 1
        assert detect_alerts_for_ingested_events(chain) == 0
        assert len(self._alerts(db)) == 1


    def test_nested_root_binary_counts_depth_from_topmost(self, db):
        # systemd(pid 1) -> systemd --user(pid 5, тот же бинарник) -> child: узел приходит
        # и от корня (глубина 2), и от вложенного семени (глубина 1) — берём от верхнего
        insert_events([
            ev(1, 0, "systemd", ts("08:00", "2026-09-10"), ts("08:01", "2026-09-10"), sha=ROOT_SHA),
            ev(5, 1, "systemd", ts("08:02", "2026-09-10"), ts("08:03", "2026-09-10"), sha=ROOT_SHA),
            ev(6, 5, "child.exe", ts("08:04", "2026-09-10"), ts("08:05", "2026-09-10")),
        ])
        conn = server_app.db_connect()
        try:
            rows = server_app._chain_depth_baseline_rows(
                conn.cursor(), (ROOT_SHA, "", "systemd"), "2026-09-01T00:00:00", "2026-09-16T00:00:00"
            )
        finally:
            conn.close()
        depths = {nid(r["pid"], r["start_time"]): r["depth"] for r in rows}
        assert len(rows) == 3
        assert depths[nid(5, ts("08:02", "2026-09-10"))] == 1
        assert depths[nid(6, ts("08:04", "2026-09-10"))] == 2


# --------------------------------------------------- chain_fanout_anomaly

PARENT_SHA = "sha_parent"


def _prior_parents(n: int, children_each: int = 1, day="2026-09-10"):
    """n сессий родительского бинарника (sha_parent) в прошлом, у каждой children_each детей."""
    events = [ev(1, 0, "root.exe", ts("07:00", day), ts("07:01", day), sha=ROOT_SHA)]
    for i in range(n):
        ppid = 10 + i
        events.append(ev(ppid, 1, "parent.exe", ts(f"08:{i:02d}", day), ts(f"08:{i:02d}", day), sha=PARENT_SHA))
        for j in range(children_each):
            events.append(ev(1000 + i * 10 + j, ppid, "kid.exe", ts(f"09:{i:02d}", day), ts(f"09:{i:02d}", day)))
    return events


def _current_parent_with_children(n_children: int):
    events = [ev(1, 0, "root.exe", ts("07:00"), ts("07:01"), sha=ROOT_SHA),
              ev(20, 1, "parent.exe", ts("09:00"), ts("09:01"), sha=PARENT_SHA)]
    for j in range(n_children):
        events.append(ev(2000 + j, 20, f"kid{j}.exe", ts(f"09:{j + 1:02d}"), ts(f"09:{j + 2:02d}")))
    return events


class TestChainFanoutAnomaly:
    def _alerts(self, db):
        return db.execute("SELECT * FROM alerts WHERE metric='chain_fanout_anomaly' ORDER BY id").fetchall()

    def test_fanout_far_above_binary_norm_fires(self, db):
        base = _prior_parents(3)
        insert_events(base)
        detect_alerts_for_ingested_events(base)
        assert self._alerts(db) == []

        cur = _current_parent_with_children(6)   # baseline max=1: порог >max(1*2, 5) -> 6 детей
        insert_events(cur)
        detect_alerts_for_ingested_events(cur)
        rows = self._alerts(db)
        assert len(rows) == 1
        a = rows[0]
        # алерт привязан к родительской сессии
        assert a["entity_type"] == "process_chain"
        assert a["process_name"] == "parent.exe"
        assert a["pid"] == 20
        assert a["start_time"] == ts("09:00")
        assert a["sha256"] == PARENT_SHA
        assert a["value"] == 6.0
        assert a["baseline"] == 1.0
        assert a["score"] == 6.0
        assert a["severity"] == "med"   # ratio 6 -> med
        assert a["chain_key"] == f"{PARENT_SHA} -> *"
        assert a["dedup_key"] == f"{M}|alice|{PARENT_SHA} -> *|chain_fanout_anomaly|2026-09-15T09"

    def test_fanout_within_norm_gives_no_alert(self, db):
        insert_events(_prior_parents(3))
        cur = _current_parent_with_children(5)   # 5 > 5? нет
        insert_events(cur)
        detect_alerts_for_ingested_events(cur)
        assert self._alerts(db) == []

    def test_browser_like_parent_has_higher_norm(self, db):
        # у родителя с высоким фоновым fan-out (по 8 детей) 12 детей — норма
        insert_events(_prior_parents(3, children_each=8))
        cur = _current_parent_with_children(12)
        insert_events(cur)
        detect_alerts_for_ingested_events(cur)
        assert self._alerts(db) == []

    def test_not_enough_prior_parent_sessions_gives_no_alert(self, db):
        insert_events(_prior_parents(server_app.CHAIN_FANOUT_MIN_PARENTS_DEFAULT - 1))
        cur = _current_parent_with_children(10)
        insert_events(cur)
        detect_alerts_for_ingested_events(cur)
        assert self._alerts(db) == []


# ------------------------------------------------- chain_catalog: ATT&CK

class TestChainCatalogAttackTechnique:
    def _items(self, client):
        r = client.get("/api/chain-catalog", headers=H)
        assert r.status_code == 200
        return {x["chain_key"]: x for x in r.json()["items"]}

    def test_write_and_read(self, client, db):
        r = client.post("/api/chain-catalog-item", headers=H,
                        json={"chain_key": "a -> b", "chain_name": "shell", "attack_technique_id": " T1059 "})
        assert r.status_code == 200
        assert self._items(client)["a -> b"]["attack_technique_id"] == "T1059"
        assert db.execute("SELECT attack_technique_id FROM chain_catalog").fetchone()["attack_technique_id"] == "T1059"

    def test_absent_key_keeps_value_and_empty_clears_it(self, client):
        client.post("/api/chain-catalog-item", headers=H,
                    json={"chain_key": "a -> b", "chain_name": "shell", "attack_technique_id": "T1059"})
        # без ключа (как дашборд фазы A) — значение не затирается
        client.post("/api/chain-catalog-item", headers=H, json={"chain_key": "a -> b", "chain_name": "shell2"})
        it = self._items(client)["a -> b"]
        assert it["chain_name"] == "shell2"
        assert it["attack_technique_id"] == "T1059"
        # пустая строка — очистка
        client.post("/api/chain-catalog-item", headers=H,
                    json={"chain_key": "a -> b", "chain_name": "shell2", "attack_technique_id": ""})
        assert self._items(client)["a -> b"]["attack_technique_id"] is None

    def test_new_item_without_technique_is_null(self, client):
        client.post("/api/chain-catalog-item", headers=H, json={"chain_key": "x -> y", "chain_name": "n"})
        assert self._items(client)["x -> y"]["attack_technique_id"] is None
