import sqlite3

import pytest

from agent_hub.events import make_event
from agent_hub.store import MIGRATIONS, Store


def test_migrate_wal_and_idempotent(tmp_path):
    s = Store(tmp_path / "hub.db")
    assert s.schema_version == len(MIGRATIONS)
    assert s.journal_mode.lower() == "wal"
    s.close()
    s2 = Store(tmp_path / "hub.db")  # mở lại: không chạy lại migration
    assert s2.schema_version == len(MIGRATIONS)
    tables = {r[0] for r in s2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agents", "agent_revisions", "runs", "events", "sessions", "hub_state"} <= tables


def test_events_append_only_and_query(tmp_path):
    s = Store(tmp_path / "hub.db")
    ev = make_event("tool.start", "a1", {"name": "Bash"}, run_id="r1").model_copy(update={"epoch": "e1", "seq": 1})
    s.insert_event(ev)
    ev2 = make_event("tool.complete", "a1", {"name": "Bash"}, run_id="r1").model_copy(update={"epoch": "e1", "seq": 2})
    s.insert_event(ev2)
    got = s.query_events(run_id="r1")
    assert [e.type for e in got] == ["tool.start", "tool.complete"]
    assert got[0].payload == {"name": "Bash"}
    assert [e.seq for e in s.query_events(run_id="r1", since_seq=1)] == [2]
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("UPDATE events SET type='x'")
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("DELETE FROM events")
    with pytest.raises(sqlite3.IntegrityError):  # PK (epoch, seq)
        s.insert_event(ev)


def test_state_and_tx_rollback(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.state_set("estop", {"on": True})
    assert s.state_get("estop") == {"on": True}
    assert s.state_get("missing", 5) == 5
    with pytest.raises(RuntimeError):
        with s.tx():
            s.state_set("x", 1)
            raise RuntimeError("boom")
    assert s.state_get("x") is None


def test_run_rows_json_and_cost(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.conn.execute(
        "INSERT INTO agents(id,name,home_path,created_at,updated_at) VALUES ('a1','a1','/x','t','t')"
    )
    s.insert_run({"id": "r_1", "agent_id": "a1", "status": "queued", "queued_at": "2026-01-01T00:00:00Z", "usage": {"input_tokens": 3}})
    s.update_run("r_1", {"status": "completed", "cost_usd": 0.25, "ended_at": "2999-01-01T00:00:00Z"})
    r = s.get_run("r_1")
    assert r["usage"] == {"input_tokens": 3}
    assert s.find_run("r_") ["id"] == "r_1"
    assert s.cost_today("a1") == pytest.approx(0.25)
