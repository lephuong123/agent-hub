"""Dispatcher giới hạn đồng thời toàn cục (FR-TK-07) + ESTOP (FR-TK-10) + run qua FakeRuntime."""

import asyncio

import pytest

from cfharness.errors import Blocked
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0.01)


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(**kw):
        rt = FakeRuntime(**kw)
        h = Hub(hub_home, runtime=rt)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.store.close()


async def test_global_limit(make_hub):
    hub, rt = make_hub(hold=True)
    hub.config.set("hub.max_concurrent_runs", 2)
    for i in range(4):
        hub.agents.create({"id": f"a{i}", "name": f"A{i}"})
    runs = [hub.runs.submit(f"a{i}", "do it") for i in range(4)]
    await settle()
    assert len(hub.runs.active) == 2 and len(hub.runs.queue) == 2
    statuses = [hub.store.get_run(r["id"])["status"] for r in runs]
    assert statuses.count("running") == 2 and statuses.count("queued") == 2
    # 1 run xong → run kế tiếp (FIFO) được khởi chạy
    rt.release(runs[0]["id"])
    await hub.runs.wait(runs[0]["id"], 5)
    await settle()
    assert hub.store.get_run(runs[2]["id"])["status"] == "running"
    assert hub.store.get_run(runs[3]["id"])["status"] == "queued"
    # tăng setting → có hiệu lực ngay, không restart
    hub.config.set("hub.max_concurrent_runs", 4)
    hub.runs.tick()
    await settle()
    assert hub.store.get_run(runs[3]["id"])["status"] == "running"
    rt.release()
    for r in runs:
        await hub.runs.wait(r["id"], 5)
    assert all(hub.store.get_run(r["id"])["outcome"] == "completed" for r in runs)


async def test_per_agent_limit(make_hub):
    hub, rt = make_hub(hold=True)
    hub.agents.create({"id": "solo", "name": "S"})  # max_concurrent_runs mặc định 1
    r1 = hub.runs.submit("solo", "one")
    r2 = hub.runs.submit("solo", "two")
    await settle()
    assert hub.store.get_run(r1["id"])["status"] == "running"
    assert hub.store.get_run(r2["id"])["status"] == "queued"
    rt.release()
    await hub.runs.wait(r1["id"], 5)
    await settle()
    rt.release()
    await hub.runs.wait(r2["id"], 5)
    assert hub.store.get_run(r2["id"])["outcome"] == "completed"


async def test_estop_blocks_and_stops(make_hub):
    hub, rt = make_hub(hold=True)
    hub.agents.create({"id": "a", "name": "A"})
    r1 = hub.runs.submit("a", "long")
    await settle()
    assert hub.store.get_run(r1["id"])["status"] == "running"
    await hub.runs.set_estop(True, reason="kiểm thử", stop_running=True)
    with pytest.raises(Blocked, match="ESTOP"):
        hub.runs.submit("a", "more")
    await hub.runs.wait(r1["id"], 5)
    run = hub.store.get_run(r1["id"])
    assert run["outcome"] == "cancelled"
    assert any(e.type == "hub.estop" for e in hub.store.query_events())
    assert any(e.type == "run.cancelled" for e in hub.store.query_events(run_id=r1["id"]))
    await hub.runs.set_estop(False)
    rt.hold = False
    r2 = hub.runs.submit("a", "again")
    await hub.runs.wait(r2["id"], 5)
    assert hub.store.get_run(r2["id"])["outcome"] == "completed"


async def test_estop_without_stop_running_lets_run_finish(make_hub):
    hub, rt = make_hub(hold=True)
    hub.agents.create({"id": "a", "name": "A"})
    r1 = hub.runs.submit("a", "x")
    await settle()
    await hub.runs.set_estop(True, reason="no new")
    rt.release()
    await hub.runs.wait(r1["id"], 5)
    assert hub.store.get_run(r1["id"])["outcome"] == "completed"


async def test_run_records_cost_session_and_events(make_hub):
    hub, rt = make_hub()
    hub.agents.create({"id": "researcher", "name": "R", "model": "haiku"})
    run = hub.runs.submit("researcher", "tóm tắt README")
    final = await hub.runs.wait(run["id"], 5)
    assert final["status"] == "completed" and final["outcome"] == "completed"
    assert final["cost_usd"] == 0.00474188
    assert final["cost_basis"] == "list"
    assert final["summary"] == "xong" and final["duration_ms"] == 2925
    assert final["usage"]["output_tokens"] == 184
    assert final["agent_config_revision"] == 1
    assert final["system_prompt_hash"]
    assert final["raw_log_path"].endswith(".jsonl.gz")
    evs = hub.store.query_events(run_id=run["id"])
    t = [e.type for e in evs]
    assert t[0] == "run.queued"
    started = next(e for e in evs if e.type == "run.started")
    assert started.payload["session_id"] == run["session_id"] == final["session_id"]
    assert t.count("run.completed") == 1  # chỉ event chính thức của hub
    done = next(e for e in evs if e.type == "run.completed")
    assert done.source == "hub" and done.payload["cost_usd"] == 0.00474188
    assert "message.delta" not in t  # delta không ghi DB
    # fixture mẫu ghi KHÔNG cô lập (có hook Serena, plugin cf-hubspot, auto-memory) → hub phát hiện cả ba
    kinds = {(v["kind"], v["name"].split(":")[0]) for v in final["isolation_violations"]}
    assert ("hook", "SessionStart") in kinds and ("plugin", "cf-hubspot") in kinds
    assert any(v["kind"] == "auto_memory" for v in final["isolation_violations"])
    assert hub.store.get_session(run["session_id"])["agent_id"] == "researcher"
    assert hub.store.get_agent("researcher")["status"] == "idle"


async def test_budget_per_day(make_hub):
    hub, rt = make_hub()
    hub.agents.create({"id": "b", "name": "B", "limits": {"budget_usd_per_day": 0.3, "max_budget_usd_per_run": 1.0}})
    run = hub.runs.submit("b", "x")
    assert run["max_budget_usd"] == 0.3  # min(run, ngày còn lại)
    await hub.runs.wait(run["id"], 5)
    hub.agents.update("b", {"limits": {"budget_usd_per_day": 0.004}})
    with pytest.raises(Blocked, match="ngân sách"):
        hub.runs.submit("b", "y")
    assert any(e.type == "budget.exceeded" for e in hub.store.query_events(agent_id="b"))


async def test_stop_queued_run(make_hub):
    hub, rt = make_hub(hold=True)
    hub.config.set("hub.max_concurrent_runs", 1)
    hub.agents.create({"id": "a", "name": "A"})
    hub.agents.create({"id": "b", "name": "B"})
    r1 = hub.runs.submit("a", "x")
    r2 = hub.runs.submit("b", "y")
    await settle()
    await hub.runs.stop(r2["id"])
    assert hub.store.get_run(r2["id"])["status"] == "cancelled"
    rt.release()
    await hub.runs.wait(r1["id"], 5)


async def test_spawn_failure_marks_run_failed(make_hub, hub_home):
    hub, rt = make_hub()
    hub.agents.create({"id": "a", "name": "A", "tool_policy": {"permission_mode": "bypassPermissions"}})
    from cfharness.runtime import ClaudeCliRuntime

    hub.runs._runtime = ClaudeCliRuntime({"binary": "/nonexistent/claude"})
    run = hub.runs.submit("a", "x")
    final = await hub.runs.wait(run["id"], 5)
    assert final["status"] == "failed" and final["error"]
    assert hub.store.get_agent("a")["status"] == "error"


async def test_auth_block(make_hub, tmp_path):
    fx = tmp_path / "auth.jsonl"
    fx.write_text('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Not logged in · Please run /login","total_cost_usd":0}\n', encoding="utf-8")
    hub, _ = make_hub()
    hub.runs._runtime = FakeRuntime(fx)
    hub.agents.create({"id": "a", "name": "A"})
    run = hub.runs.submit("a", "x")
    await hub.runs.wait(run["id"], 5)
    assert hub.runs.auth_block
    with pytest.raises(Blocked, match="chưa đăng nhập"):
        hub.runs.submit("a", "y")
    hub.runs.clear_auth_block()
    hub.runs.submit("a", "z")
