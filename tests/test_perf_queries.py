"""Hiệu năng truy vấn của daemon (SQLite chạy đồng bộ trên event loop → truy vấn chậm làm khựng mọi RPC/SSE).

- v32: index `runs(queued_at)`, `runs(ended_at)`, `runs(agent_id, ended_at)`; `EXPLAIN QUERY PLAN` phải dùng index cho `list_runs`,
  `cost_since` (+ bản gộp theo agent) và đếm event `learning.*` của `tui.snapshot`.
- `list_runs` không trả cột `diff` (nặng); `get_run`/`find_run`/`run.diff` vẫn có.
- `tui.agent_state` = đúng dòng agent của `tui.snapshot`, chỉ một agent.
- Panel "Hành động gần đây" của trang Điều phối: `manager.action` MỚI NHẤT, lọc theo luồng đang xem.
Không gọi `claude`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cfharness.events import iso, make_event
from cfharness.store import MIGRATIONS, Store
from cfharness.tui.rpc import LEARN_COUNT_SQL

from test_web import ISOLATED, _start, _stop  # noqa: F401
from test_web_manager import designate, mgr_rpc
from cfharness.runtime import FakeRuntime


def _plan(s: Store, sql: str, args: tuple = ()) -> str:
    return " | ".join(str(r[3]) for r in s.conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def _run(s: Store, rid: str, agent: str, *, queued: str, ended: str | None = None, cost: float = 0.0, **extra) -> None:
    s.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "status": "completed" if ended else "queued", "queued_at": queued,
                  "ended_at": ended, "cost_usd": cost, **extra})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    now = datetime.now(timezone.utc)
    for aid in ("a", "b"):
        s.conn.execute("INSERT INTO agents(id, name, home_path, status, config_revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                       (aid, aid, "/tmp/" + aid, "idle", 1, iso(now), iso(now)))
    yield s
    s.close()


def test_migration_adds_runs_indexes(store):
    assert store.schema_version == len(MIGRATIONS)
    idx = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='runs'")}
    assert {"runs_queued", "runs_ended", "runs_agent_ended"} <= idx


def test_query_plans_use_indexes(store):
    since = iso(datetime.now(timezone.utc))
    plan = _plan(store, f"SELECT {store._runs_list_cols()} FROM runs ORDER BY queued_at DESC LIMIT ?", (50,))
    assert "runs_queued" in plan and "TEMP B-TREE" not in plan, plan
    assert "runs_ended" in _plan(store, "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE ended_at >= ?", (since,))
    assert "runs_agent_ended" in _plan(store, "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE agent_id=? AND ended_at >= ?", ("a", since))
    p = _plan(store, "SELECT agent_id, COALESCE(SUM(cost_usd), 0) FROM runs WHERE ended_at >= ? GROUP BY +agent_id", (since,))
    assert "SEARCH runs USING INDEX runs_ended" in p, p  # đúng câu lệnh của Store.cost_since_by_agent
    assert "runs_ended" in _plan(store, "SELECT agent_id, usage, cost_usd FROM runs WHERE ended_at >= ?", (since,))  # tui.snapshot
    p = _plan(store, LEARN_COUNT_SQL, (since,))
    assert "events_type_ts" in p and "SCAN events" not in p, p


def test_list_runs_omits_diff_single_run_keeps_it(store):
    diff = {"kind": "git", "stat": "+1 -0", "files": [{"path": "x.py", "status": "M"}], "patch": "x" * 10_000}
    _run(store, "r1", "a", queued="2026-01-01T00:00:00Z", ended="2026-01-01T00:01:00Z", diff=diff, run_opts={"k": 1})
    _run(store, "r2", "a", queued="2026-01-02T00:00:00Z")
    rows = store.list_runs()
    assert [r["id"] for r in rows] == ["r2", "r1"]  # mới nhất trước
    assert all("diff" not in r for r in rows)
    assert rows[1]["run_opts"] == {"k": 1}  # cột JSON khác vẫn được giải mã
    assert store.list_runs(agent_id="a", status="completed")[0]["id"] == "r1"
    assert store.get_run("r1")["diff"] == diff
    assert store.find_run("r1")["diff"] == diff


def test_cost_since_batched_matches_per_agent(store):
    now = datetime.now(timezone.utc)
    old, recent = iso(now - timedelta(days=3)), iso(now - timedelta(minutes=5))
    _run(store, "r1", "a", queued=old, ended=old, cost=9.0)
    _run(store, "r2", "a", queued=recent, ended=recent, cost=1.25)
    _run(store, "r3", "a", queued=recent, ended=recent, cost=0.75)
    _run(store, "r4", "b", queued=recent, ended=recent, cost=0.5)
    _run(store, "r5", "b", queued=recent)  # chưa kết thúc → không tính
    since = now - timedelta(hours=1)
    by = store.cost_since_by_agent(since)
    assert by == {"a": 2.0, "b": 0.5}
    assert by["a"] == store.cost_since(since, "a") and by["b"] == store.cost_since(since, "b")
    assert store.cost_since(since) == 2.5
    assert store.cost_today_by_agent().get("a") == store.cost_today("a")


def test_query_events_newest_first_types_and_workstream(store):
    _run(store, "rl", "a", queued="2026-01-01T00:00:00Z", run_opts={"manager_conversation": "c1"})
    _run(store, "rw", "a", queued="2026-01-01T00:00:00Z", run_opts={"manager_workstream": "ws_1"})
    seq = 0
    for i in range(6):
        for rid in ("rl", "rw"):
            for typ in ("manager.action", "tool.start"):
                seq += 1
                ev = make_event(typ, "a", {"i": i, "rid": rid}, run_id=rid).model_copy(
                    update={"epoch": "e", "seq": seq, "ts": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seq)})
                store.insert_event(ev)
    got = store.query_events(agent_id="a", types=["manager.action"], newest_first=True, limit=3)
    assert [e.type for e in got] == ["manager.action"] * 3
    assert [e.seq for e in got] == sorted((e.seq for e in got), reverse=True) and got[0].payload == {"i": 5, "rid": "rw"}
    ws = store.query_events(agent_id="a", types=["manager.action"], newest_first=True, manager_workstream="ws_1")
    assert {e.run_id for e in ws} == {"rw"} and len(ws) == 6
    lobby = store.query_events(agent_id="a", types=["manager.action"], manager_workstream="")
    assert {e.run_id for e in lobby} == {"rl"} and [e.seq for e in lobby] == sorted(e.seq for e in lobby)  # mặc định cũ → mới


# --------------------------------------------------------------------------- RPC (daemon in-process)
@pytest.fixture
async def denv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    e.hub.agents.create({"id": "w1", "name": "worker", "model": "haiku"})
    yield e
    await _stop(e)


async def test_agent_state_matches_snapshot_row_and_agent_list_cost(denv):
    s = denv.hub.store
    t = iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    _run(s, "r_b1", "boss", queued=t, ended=t, cost=0.4, usage={"input_tokens": 10, "output_tokens": 5})
    _run(s, "r_w1", "w1", queued=t, ended=t, cost=0.1)
    snap = await mgr_rpc(denv, "tui.snapshot")
    row = next(r for r in snap["agents"] if r["id"] == "boss")
    st = await mgr_rpc(denv, "tui.agent_state", {"id": "boss"})
    assert st["agent"] == row
    assert row["cost_today_usd"] == 0.4 and row["tokens_today"] == 15 and row["last_run_id"] == "r_b1"
    assert (await mgr_rpc(denv, "tui.agent_state", {"id": "khong-co"}))["agent"] is None
    resp = await denv.d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tui.agent_state", "params": {}})
    assert "error" in resp  # thiếu id
    agents = {a["id"]: a for a in await mgr_rpc(denv, "agent.list")}
    assert agents["boss"]["cost_today_usd"] == 0.4 and agents["w1"]["cost_today_usd"] == 0.1


async def test_run_list_has_no_diff_run_get_and_run_diff_do(denv):
    s = denv.hub.store
    t = iso(datetime.now(timezone.utc))
    diff = {"kind": "scratch", "stat": "+1 -0", "files": [{"path": "a.txt", "status": "A"}]}
    _run(s, "r_d", "w1", queued=t, ended=t, diff=diff)
    rows = await mgr_rpc(denv, "run.list", {"agent": "w1"})
    assert rows and all("diff" not in r for r in rows)
    assert (await mgr_rpc(denv, "run.get", {"id": "r_d"}))["diff"] == diff
    assert (await mgr_rpc(denv, "run.diff", {"id": "r_d"}))["diff"] == diff


# --------------------------------------------------------------------------- web: panel hành động theo luồng
@pytest.fixture
async def wenv(hub_home):
    from cfharness.web import manager_stub

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    yield e
    await _stop(e)


async def test_side_actions_newest_and_filtered_by_workstream(wenv, monkeypatch):
    from cfharness.web import manager as M

    await wenv.login()
    await designate(wenv)
    w = await mgr_rpc(wenv, "workstream.create", {"title": "Monitor", "key": "LH-1"})
    s = wenv.hub.store
    t = iso(datetime.now(timezone.utc))
    _run(s, "r_lobby", "boss", queued=t, ended=t, run_opts={"manager_conversation": "x"})
    _run(s, "r_ws", "boss", queued=t, ended=t, run_opts={"manager_workstream": w["id"]})
    _run(s, "r_other", "boss", queued=t, ended=t, run_opts={"manager_workstream": "ws_khac"})
    monkeypatch.setattr(M, "ACTIONS_LIMIT", 5)
    for i in range(8):  # 8 hành động Sảnh: chỉ 5 MỚI NHẤT được đọc
        wenv.hub.publish(make_event("manager.action", "boss", {"tool": "agent_update", "summary": f"Sảnh việc số {i:02d}"}, run_id="r_lobby"))
    wenv.hub.publish(make_event("manager.action", "boss", {"tool": "task_create", "summary": "Việc của luồng Monitor"}, run_id="r_ws"))
    wenv.hub.publish(make_event("manager.action", "boss", {"tool": "task_create", "summary": "Việc của luồng khác"}, run_id="r_other"))
    for i in range(30):  # event loại khác của điều phối viên không lấn chỗ thẻ hành động
        wenv.hub.publish(make_event("tool.start", "boss", {"name": "Read"}, run_id="r_lobby"))

    r = await wenv.client.get("/manager")
    side = r.text.split('id="mgr-actions"', 1)[1].split("</ol>", 1)[0]
    assert "Sảnh việc số 07" in side and "Sảnh việc số 03" in side and "Sảnh việc số 02" not in side
    assert side.index("Sảnh việc số 07") < side.index("Sảnh việc số 03")  # mới nhất trên cùng
    assert "luồng Monitor" not in side and "luồng khác" not in side

    r = await wenv.client.get(f"/manager?w={w['id']}")
    side = r.text.split('id="mgr-actions"', 1)[1].split("</ol>", 1)[0]
    assert "Việc của luồng Monitor" in side
    assert "Sảnh việc" not in side and "luồng khác" not in side
