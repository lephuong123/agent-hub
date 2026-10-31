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


async def test_budget_none_without_limits_then_daily_remainder(make_hub):
    """Điều phối viên bỏ trần mỗi run (max_budget_usd_per_run=null), không trần ngày, không runs.default_max_budget_usd
    → không --max-budget-usd; thêm trần ngày → trần run = phần còn lại của ngày."""
    hub, rt = make_hub()
    hub.agents.create({"id": "m", "name": "M", "limits": {"max_budget_usd_per_run": 2.0}})
    hub.agents.update("m", {"limits": {"max_budget_usd_per_run": None}})
    row = hub.store.get_agent("m")
    assert row["limits"].get("max_budget_usd_per_run") is None and hub.config.get("runs.default_max_budget_usd") is None
    assert hub.runs._budget_for(row, None) is None
    run = hub.runs.submit("m", "x")
    assert run["max_budget_usd"] is None
    await hub.runs.wait(run["id"], 5)
    used = hub.store.cost_today("m")
    hub.agents.update("m", {"limits": {"budget_usd_per_day": 30}})
    assert hub.runs._budget_for(hub.store.get_agent("m"), None) == round(30 - used, 4)


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


class _AuthRuntime(FakeRuntime):
    """FakeRuntime trả `auth_status` theo kịch bản (lần lượt), giữ giá trị cuối."""

    def __init__(self, fixture, statuses):
        super().__init__(fixture)
        self.statuses = list(statuses)
        self.checks = 0

    async def auth_status(self):
        self.checks += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def _auth_fixture(tmp_path):
    fx = tmp_path / "auth.jsonl"
    fx.write_text('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Not logged in · Please run /login","total_cost_usd":0}\n', encoding="utf-8")
    return fx


async def test_auth_transient_not_blocked(make_hub, tmp_path):
    """Lỗi "Not logged in" nhưng `claude auth status` vẫn đăng nhập → chỉ run đó lỗi, KHÔNG chặn cả hub."""
    hub, _ = make_hub()
    rt = _AuthRuntime(_auth_fixture(tmp_path), [True])
    hub.runs._runtime = rt
    hub.agents.create({"id": "a", "name": "A"})
    run = hub.runs.submit("a", "x")
    final = await hub.runs.wait(run["id"], 5)
    assert final["status"] == "failed"
    assert rt.checks == 1
    assert not hub.runs.auth_block
    hub.runs.submit("a", "y")  # không bị Blocked


async def test_auth_block_auto_clears(make_hub, tmp_path):
    """Kiểm lại xác nhận chưa đăng nhập → chặn; vòng nền thấy đăng nhập lại → tự gỡ chặn."""
    hub, _ = make_hub()
    hub.config.set("runtime.claude.auth_recheck_s", 0.05)
    rt = _AuthRuntime(_auth_fixture(tmp_path), [False, False, True])
    hub.runs._runtime = rt
    hub.agents.create({"id": "a", "name": "A"})
    run = hub.runs.submit("a", "x")
    await hub.runs.wait(run["id"], 5)
    assert hub.runs.auth_block
    with pytest.raises(Blocked, match="chưa đăng nhập"):
        hub.runs.submit("a", "y")
    for _ in range(100):
        if not hub.runs.auth_block:
            break
        await asyncio.sleep(0.02)
    assert not hub.runs.auth_block
    assert rt.checks >= 3
    hub.runs.submit("a", "z")


async def test_auth_recheck_disabled(make_hub, tmp_path):
    hub, _ = make_hub()
    hub.config.set("runtime.claude.auth_recheck_s", 0)
    rt = _AuthRuntime(_auth_fixture(tmp_path), [False, True])
    hub.runs._runtime = rt
    hub.agents.create({"id": "a", "name": "A"})
    run = hub.runs.submit("a", "x")
    await hub.runs.wait(run["id"], 5)
    await asyncio.sleep(0.1)
    assert hub.runs.auth_block and rt.checks == 1


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="script shell")
async def test_claude_cli_auth_status(tmp_path):
    from cfharness.runtime.claude_cli import ClaudeCliRuntime

    def bin_(out):
        b = tmp_path / f"claude_{abs(hash(out))}"
        b.write_text(f"#!/bin/sh\nprintf '%s' '{out}'\n", encoding="utf-8")
        b.chmod(0o755)
        return str(b)

    assert await ClaudeCliRuntime({"binary": bin_('{"loggedIn": true}')}).auth_status() is True
    assert await ClaudeCliRuntime({"binary": bin_('{"loggedIn": false}')}).auth_status() is False
    assert await ClaudeCliRuntime({"binary": bin_("garbage")}).auth_status() is None
    assert await ClaudeCliRuntime({"binary": "/nonexistent/claude"}).auth_status() is None
