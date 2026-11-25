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
from test_web_manager import designate, mgr_rpc, snap
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

    side = [a["summary"] for a in (await snap(wenv))["side_actions"]]  # panel "Hành động gần đây" của island
    assert "Sảnh việc số 07" in side and "Sảnh việc số 03" in side and "Sảnh việc số 02" not in side
    assert side.index("Sảnh việc số 07") < side.index("Sảnh việc số 03")  # mới nhất trên cùng
    assert "Việc của luồng Monitor" not in side and "Việc của luồng khác" not in side

    side = [a["summary"] for a in (await snap(wenv, f"?w={w['id']}"))["side_actions"]]
    assert "Việc của luồng Monitor" in side
    assert not any("Sảnh việc" in x for x in side) and "Việc của luồng khác" not in side


# --------------------------------------------------------------------------- PR3 (plan-api-caching): index v36, N+1, cache snapshot
def test_v36_indexes_and_plans(store):
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"tasks_current_run", "runs_kind_queued", "events_agent_epoch_seq"} <= names
    since = iso(datetime.now(timezone.utc))
    assert "tasks_current_run" in _plan(store, "SELECT * FROM tasks WHERE current_run_id = ?", ("x",))
    for kind in ("curator", "review", "graph_extract"):
        assert "runs_kind_queued" in _plan(store, f"SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE kind='{kind}' AND queued_at>=?", (since,))
    # đúng câu lệnh `Store.query_events` của backfill `events.subscribe` (Hub.backfill)
    p = _plan(store, "SELECT * FROM events WHERE agent_id=? AND epoch=? AND seq>? ORDER BY ts, epoch, seq LIMIT ?", ("a", "e", 1, 10))
    assert "events_agent_epoch_seq (agent_id=? AND epoch=? AND seq>?)" in p, p


def test_v36_applies_on_existing_db(tmp_path):
    path = tmp_path / "hub.db"
    s = Store(path)
    for name in ("tasks_current_run", "runs_kind_queued", "events_agent_epoch_seq"):
        s.conn.execute(f"DROP INDEX {name}")
    s.conn.execute(f"PRAGMA user_version={len(MIGRATIONS) - 1}")  # DB cũ: dừng ở v35
    s.close()
    s = Store(path)
    try:
        assert s.schema_version == len(MIGRATIONS)
        names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"tasks_current_run", "runs_kind_queued", "events_agent_epoch_seq"} <= names
    finally:
        s.close()


def test_today_usage_sql_matches_python(store):
    from cfharness.tui import rpc as R

    now = datetime.now(timezone.utc)
    t = iso(now - timedelta(minutes=1))
    old = iso(now - timedelta(days=3))
    usages = [{"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 7, "x": 9},
              {"input_tokens": 3}, None, {"output_tokens": None}, [1, 2]]
    for i, u in enumerate(usages):
        _run(store, f"ra{i}", "a", queued=t, ended=t, cost=0.1 * (i + 1), usage=u)
    _run(store, "rb", "b", queued=t, ended=t, cost=0.25, usage={"input_tokens": 4})
    _run(store, "rold", "a", queued=old, ended=old, cost=9.0, usage={"input_tokens": 999})
    store.conn.execute("UPDATE runs SET usage='{hỏng' WHERE id='ra2'")  # JSON hỏng → 0 token (như `_tokens`)
    since = R._today_start_utc()
    exp_tok: dict[str, int] = {}
    exp_cost: dict[str, float] = {}
    for r in store.conn.execute("SELECT agent_id, usage, cost_usd FROM runs WHERE ended_at >= ?", (since,)):  # cách cũ
        exp_tok[r["agent_id"]] = exp_tok.get(r["agent_id"], 0) + R._tokens(r["usage"])
        exp_cost[r["agent_id"]] = exp_cost.get(r["agent_id"], 0.0) + float(r["cost_usd"] or 0.0)
    hub = type("H", (), {"store": store})()
    tok, cost = R._today_usage(hub, since)
    assert tok == exp_tok == {"a": 1025, "b": 4}
    assert cost.keys() == exp_cost.keys() and all(abs(cost[k] - exp_cost[k]) < 1e-9 for k in cost)
    tok_a, cost_a = R._today_usage(hub, since, "a")
    assert tok_a == {"a": 1025} and abs(cost_a["a"] - exp_cost["a"]) < 1e-9


def test_batched_prefetch_matches_per_agent_queries(store):
    from cfharness.tui import rpc as R

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.conn.execute("INSERT INTO tasks(id, title, status, created_at, updated_at) VALUES ('t1', 'Việc một', 'running', ?, ?)",
                       (iso(t0), iso(t0)))
    _run(store, "a_old", "a", queued=iso(t0), ended=iso(t0))
    _run(store, "a_new", "a", queued=iso(t0 + timedelta(hours=1)), task_id="t1", diff={"kind": "git", "patch": "x" * 100})
    _run(store, "b_1", "b", queued=iso(t0), ended=iso(t0))
    _run(store, "b_2", "b", queued=iso(t0), ended=iso(t0))  # trùng queued_at: phải chọn giống `list_runs(limit=1)`
    hub = type("H", (), {"store": store})()
    agents = [{"id": "a", "current_run_id": "a_new"}, {"id": "b", "current_run_id": None}, {"id": "c", "current_run_id": None}]
    runs, last, titles = R._prefetch(hub, agents, {}, {})
    assert set(runs) == {"a_new"} and "diff" not in runs["a_new"] and runs["a_new"]["task_id"] == "t1"
    assert last["b"]["id"] == store.list_runs(agent_id="b", limit=1)[0]["id"] and "c" not in last and "a" not in last
    assert titles == {"t1": "Việc một"}


async def test_snapshot_cache_shares_and_invalidates(denv):
    from cfharness.tui.rpc import SnapshotCache, snapshot

    hub = denv.hub
    clock = [100.0]
    c = SnapshotCache(hub, ttl=1.0, clock=lambda: clock[0])
    s1 = c.get()
    s2 = c.get()
    assert (c.misses, c.hits) == (1, 1) and s1 == s2 and s1 is not s2
    s2["agents"].clear()  # người gọi sửa bản của mình — cache không đổi
    assert c.get()["agents"] and c.hits == 2
    assert {r["id"] for r in s1["agents"]} == {r["id"] for r in snapshot(hub)["agents"]}  # đúng như bản không cache

    t = iso(datetime.now(timezone.utc))
    _run(hub.store, "r_c1", "w1", queued=t, ended=t, cost=0.5)  # ghi trạng thái thẳng vào DB (không event) → tính lại ngay
    row = next(r for r in c.get()["agents"] if r["id"] == "w1")
    assert c.misses == 2 and row["cost_today_usd"] == 0.5 and row["last_run_id"] == "r_c1"

    c.get()
    assert c.misses == 2
    clock[0] += 1.5  # quá TTL → tính lại dù khoá không đổi
    c.get()
    assert c.misses == 3


async def test_snapshot_cache_shared_while_streaming_but_fresh_on_approval(denv):
    """(a) đang stream message.delta (event được lưu, last_seq tăng): các lần gọi trong TTL dùng cache; (b) approval mới → tính lại NGAY."""
    from cfharness.tui.rpc import SnapshotCache

    hub = denv.hub
    clock = [100.0]
    c = SnapshotCache(hub, ttl=1.0, clock=lambda: clock[0])
    t = iso(datetime.now(timezone.utc))
    _run(hub.store, "r_s", "w1", queued=t, status="running", started_at=t)
    s0 = c.get()
    assert c.misses == 1
    for i in range(20):  # stream: mỗi lần gọi xen giữa các event, vẫn trong TTL
        hub.publish(make_event("message.delta", "w1", {"text": f"chữ {i} "}, run_id="r_s"))  # không lưu DB
        hub.publish(make_event("tool.start", "w1", {"name": "Read"}, run_id="r_s"))  # CÓ lưu DB (insert_event không tính)
        clock[0] += 0.04
        assert c.get()["last_seq"] == s0["last_seq"]
    assert c.misses == 1 and c.hits == 20
    assert hub.bus.last_seq > s0["last_seq"] and len(hub.store.query_events(run_id="r_s", types=["tool.start"])) == 20

    ap = hub.approvals.open(hub.store.get_run("r_s"), tool_name="Bash", tool_input={"command": "rm -rf build"}, tool_use_id="tu1",
                            fingerprint="fp1", verdict={"klass": "fs_delete", "rule": "rm_recursive", "verdict": "destructive", "reason": "xoá"},
                            channel="hook")
    s1 = c.get()  # vẫn trong TTL nhưng trạng thái đổi
    assert c.misses == 2 and ap["id"] in [a["id"] for a in s1["approvals"]]
    assert next(r for r in s1["agents"] if r["id"] == "w1")["pending_approvals"] == [ap["id"]]
    assert s1["last_seq"] == hub.bus.last_seq
    hub.approvals.resolve(ap["id"], "allow", by="user", channel="cli")  # duyệt → cũng thấy ngay
    assert c.get()["approvals"] == [] and c.misses == 3


async def test_snapshot_rpc_uses_cache_and_hub_status_counts(denv):
    before = denv.d.snapshot_cache.misses
    a = await mgr_rpc(denv, "tui.snapshot")
    b = await mgr_rpc(denv, "tui.snapshot")
    assert a["agents"] == b["agents"] and denv.d.snapshot_cache.misses - before <= 1 and denv.d.snapshot_cache.hits >= 1
    assert a["hub"]["agents"] == 2 == denv.hub.status()["agents"] == denv.hub.store.count_agents()
    denv.hub.agents.create({"id": "w2", "name": "w2", "model": "haiku"})
    c = await mgr_rpc(denv, "tui.snapshot")
    assert {r["id"] for r in c["agents"]} == {"boss", "w1", "w2"} and c["hub"]["agents"] == 3
