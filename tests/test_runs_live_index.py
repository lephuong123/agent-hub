"""v22: câu "run đang sống của <agent|session|team>" không được duyệt lịch sử run (docs/dev-notes/runs-live-index.md).

Không có ANALYZE, SQLite từng chọn `runs_agent(agent_id, queued_at)` cho `_active_runs` của điều phối viên → đọc mọi run cũ (mỗi dòng
qua trang tràn của `prompt`) mỗi `manager.check_s` → hub đứng 3–14 s trên máy thiếu RAM.
"""

import pytest

from cfharness.store import Store

LIVE = ("queued", "running", "waiting_human")
IN = ",".join("?" * len(LIVE))

# Hình dạng các câu thật: manager/service._active_runs, learning/review.agent_idle, auto_resume / runs.py / dispatch/rpc (session),
# teams.py (team).
CASES = [
    (f"SELECT id FROM runs WHERE agent_id=? AND kind='manager' AND status IN ({IN})", ("m", *LIVE), "runs_agent_status"),
    (f"SELECT COUNT(*) FROM runs WHERE agent_id=? AND status IN ({IN})", ("m", *LIVE), "runs_agent_status"),
    ("SELECT id, status FROM runs WHERE session_id=? AND status IN ('queued','running','waiting_human') LIMIT 1", ("s",), "runs_session_status"),
    ("SELECT id FROM runs WHERE team_id=? AND status IN ('queued','running','waiting_human')", ("t",), "runs_team_status"),
]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.conn.execute("PRAGMA foreign_keys=OFF")
    big = "x" * 6000  # > 1 trang 4 KB: như prompt của điều phối viên
    rows = [(f"r{i}", "m", "manager", big, "s", "t", "completed", "2026-11-01T00:00:00Z") for i in range(400)]
    rows.append(("live", "m", "manager", big, "s", "t", "running", "2026-11-05T00:00:00Z"))
    s.conn.executemany("INSERT INTO runs (id, agent_id, kind, prompt, session_id, team_id, status, queued_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    yield s
    s.close()


@pytest.mark.parametrize("sql,args,index", CASES)
def test_live_run_queries_use_status_index(store, sql, args, index):
    plan = " ".join(r[3] for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql, args))
    assert f"USING INDEX {index}" in plan or f"USING COVERING INDEX {index}" in plan, plan


def test_manager_active_runs_finds_only_live(store):
    rows = store.conn.execute(CASES[0][0], CASES[0][1]).fetchall()
    assert [r[0] for r in rows] == ["live"]
