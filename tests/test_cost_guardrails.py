"""Rào chắn chi phí còn hở (docs/dev-notes/cost-guardrails.md): trần ngày riêng của điều phối viên, trần requeue run `interrupted`
của task, chi phí ước lượng cho run kết thúc không có `result`, `budget.*` vào notify mặc định + một lần/ngày/scope, ước tính
$/ngày của lịch theo chi phí thật. Không gọi claude (ScriptedRuntime)."""

from __future__ import annotations

import asyncio

import pytest

from cfharness import auth
from cfharness.config import DEFAULTS, validate_setting
from cfharness.cost_guard import interrupted_streak
from cfharness.errors import BLOCKED, Blocked, HubError
from cfharness.events import EVENT_LEVELS, make_event
from cfharness.hub import Hub
from cfharness.notify.slack import format_message
from m3_fake import ScriptedRuntime
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status


async def until(pred, timeout: float = 8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        v = pred()
        if v:
            return v
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("hết giờ chờ")
        await asyncio.sleep(0.02)


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script=None, **cfg):
        rt = ScriptedRuntime(script or (lambda spec: {"text": "ok"}))
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        for k, v in cfg.items():
            h.config.set(k, v)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def evs(hub, t):
    return [e for e in hub.store.query_events(types=[t])]


# ============================================================================ 1. trần ngày riêng của điều phối viên
def test_manager_budget_setting_validated():
    assert DEFAULTS["manager"]["budget_usd_per_day"] is None
    validate_setting("manager.budget_usd_per_day", 2.5)
    validate_setting("manager.budget_usd_per_day", None)
    for bad in (0, -1, "2", True):
        with pytest.raises(ValueError):
            validate_setting("manager.budget_usd_per_day", bad)
    with pytest.raises(ValueError):
        validate_setting("manager", {"budget_usd_per_day": -3})


async def test_manager_daily_budget_blocks_only_manager(hub_home):
    s = MgrScript()
    s.hold_manager = False
    d = await make_daemon(hub_home, s)
    try:
        d.hub.agents.create({"id": "boss", "name": "boss", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
        d.hub.agents.create({"id": "w1", "name": "w1"})
        await rpc(d, "manager.set", {"agent_id": "boss"})
        d.hub.config.set("manager.budget_usd_per_day", 0.0012)
        d.hub.config.set("budget.warn_pct", 50)
        scopes = d.hub.runs.budget_scopes("boss", None)
        assert ("manager", "boss") == scopes[-1][:2] and scopes[-1][3] == pytest.approx(0.0012)
        assert not [x for x in d.hub.runs.budget_scopes("w1", None) if x[0] == "manager"]  # agent thường: không scope manager
        r = await rpc(d, "manager.chat", {"channel": "web", "text": "chào"})
        run = await wait_status(d, r["run_id"], ("completed",))
        assert run["max_budget_usd"] == pytest.approx(0.0012)  # trần run = phần còn lại của trần ngày manager
        warn = [e for e in evs(d.hub, "budget.warning") if e.payload["scope"] == "manager"]
        assert len(warn) == 1 and warn[0].agent_id == "boss"
        # lần 2: 0.001 + 0.001 > 0.0012 → hết ngân sách
        r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "lần 2", "conversation_id": r["conversation_id"]})
        await wait_status(d, r2["run_id"], ("completed",))
        with pytest.raises(HubError) as ei:
            await rpc(d, "manager.chat", {"channel": "web", "text": "lần 3", "conversation_id": r["conversation_id"]})
        assert ei.value.code == BLOCKED and "điều phối viên 'boss'" in ei.value.message
        with pytest.raises(HubError):
            await rpc(d, "manager.chat", {"channel": "web", "text": "lần 4", "conversation_id": r["conversation_id"]})
        exc = [e for e in evs(d.hub, "budget.exceeded") if e.payload["scope"] == "manager"]
        assert len(exc) == 1, "budget.exceeded chỉ một lần/ngày/scope dù bị chặn lặp"
        # agent thường không bị trần của manager
        w = d.hub.runs.submit("w1", "work")
        assert (await wait_status(d, w["id"], ("completed",)))["status"] == "completed"
        # điều phối viên không tự nới trần (config.set ngoài MANAGER_ALLOWED)
        with pytest.raises(HubError) as ei:
            await rpc(d, "config.set", {"key": "manager.budget_usd_per_day", "value": 100},
                      as_caller=auth.manager_caller("boss", r["run_id"]))
        assert ei.value.code == BLOCKED
        assert d.hub.config.get("manager.budget_usd_per_day") == pytest.approx(0.0012)
    finally:
        await close(d)


async def test_manager_budget_does_not_touch_waiting_approval(hub_home):
    """Q-04: hết ngân sách chỉ chặn run MỚI — approval đang chờ không bị huỷ/từ chối."""
    d = await make_daemon(hub_home)
    try:
        res = await manager_run(d)
        rid = res["run_id"]
        ap = d.hub.approvals.open(d.hub.store.get_run(rid), tool_name="Bash", tool_input={"command": "rm -rf build"}, tool_use_id="tu1",
                                  fingerprint="fp1", verdict={"klass": "destructive", "rule": "rm_rf", "reason": "test"}, channel="hook")
        d.hub.config.set("manager.budget_usd_per_day", 0.0000001)
        d.hub.store.update_run(rid, {"cost_usd": 1.0})
        with pytest.raises(Blocked):
            d.hub.runs.submit("boss", "new")
        assert d.hub.approvals.get(ap["id"])["status"] in ("open", "waiting_human")
        assert d.hub.store.get_run(rid)["status"] == "running"
    finally:
        await close(d)


# ============================================================================ 2. trần requeue run interrupted của task
def _fake_run(hub, rid, *, parent=None, outcome="interrupted", task_id=None):
    hub.store.insert_run({"id": rid, "agent_id": "a", "agent_config_revision": 1, "kind": "task", "prompt": "x", "session_id": "s-" + rid,
                          "status": outcome, "queued_at": "2026-11-10T00:00:00Z", "parent_run_id": parent, "task_id": task_id})
    hub.store.update_run(rid, {"outcome": outcome, "ended_at": "2026-11-10T00:01:00Z"})


async def test_interrupted_requeue_cap_blocks_task(make_hub):
    hub, rt = make_hub(lambda spec: {"text": "ok", "hold": True}, **{"tasks.max_interrupted_requeues": 2})
    hub.agents.create({"id": "a", "name": "A"})
    t = hub.tasks.create({"assignee": "a", "title": "x", "body": "x"})
    await until(lambda: hub.tasks.row(t["id"])["current_run_id"])  # run thật giữ slot của agent → task không bị claim lại
    _fake_run(hub, "r_i1", task_id=t["id"])
    _fake_run(hub, "r_i2", parent="r_i1", task_id=t["id"])
    _fake_run(hub, "r_i3", parent="r_i2", task_id=t["id"])
    hub.tasks.update(t["id"], {"current_run_id": "r_i2"})
    hub.dispatcher.on_run_terminal("r_i2")  # 2 lần liên tiếp ≤ trần → requeue như cũ
    assert hub.tasks.row(t["id"])["status"] == "ready"
    hub.tasks.update(t["id"], {"current_run_id": "r_i3", "status": "running"})
    hub.dispatcher.on_run_terminal("r_i3")  # lần 3 > trần 2 → blocked
    row = hub.tasks.row(t["id"])
    assert row["status"] == "blocked" and "max_interrupted_requeues=2" in (row.get("block_reason") or "")
    ex = evs(hub, "task.requeue_exhausted")
    assert len(ex) == 1 and ex[0].payload["interrupted"] == 3 and ex[0].payload["limit"] == 2 and ex[0].task_id == t["id"]
    rt.release()


async def test_interrupted_cap_zero_means_unlimited_and_streak_resets(make_hub):
    hub, rt = make_hub(**{"tasks.max_interrupted_requeues": 0})
    hub.agents.create({"id": "a", "name": "A"})
    r1 = hub.runs.submit("a", "x")
    await hub.runs.wait(r1["id"], 5)
    hub.store.update_run(r1["id"], {"status": "interrupted", "outcome": "interrupted"})
    r2 = hub.runs.submit("a", "y", parent_run_id=r1["id"])
    await hub.runs.wait(r2["id"], 5)
    assert interrupted_streak(hub.store, hub.store.get_run(r2["id"])) == 0  # r2 completed → chuỗi không tính
    hub.store.update_run(r2["id"], {"status": "interrupted", "outcome": "interrupted"})
    assert interrupted_streak(hub.store, hub.store.get_run(r2["id"])) == 2
    with pytest.raises(ValueError):
        validate_setting("tasks.max_interrupted_requeues", -1)


# ============================================================================ 3. chi phí run không có result
async def test_crashed_run_records_estimated_cost(make_hub):
    hub, _ = make_hub(lambda spec: {"crash": True, "stream_usage": {"input": 100_000, "output": 10_000}})
    hub.agents.create({"id": "a", "name": "A", "limits": {"budget_usd_per_day": 0.3}})
    r = hub.runs.submit("a", "x", budget_usd=1.0)
    await hub.runs.wait(r["id"], 5)
    run = hub.store.get_run(r["id"])
    assert run["status"] == "crashed"
    # sonnet: 100k in × 3$ × 0.3 + 10k out × 15$ (cùng công thức usage.tick estimated)
    assert run["cost_usd"] == pytest.approx(0.24, rel=1e-3) and run["cost_basis"] == "estimated_no_result"
    assert hub.store.cost_today("a") == pytest.approx(0.24, rel=1e-3)
    fin = [e for e in hub.store.query_events(types=["run.failed"]) if e.source == "hub" and e.run_id == r["id"]]
    assert fin and fin[0].payload["cost_usd"] == pytest.approx(0.24, rel=1e-3)
    assert any(e.payload["scope"] == "agent" for e in evs(hub, "budget.warning"))  # 0.24/0.3 = 80 %
    # chuỗi tự resume dùng chung ngân sách: phần ước lượng được trừ
    from cfharness.auto_resume import chain_budget_left

    assert run["max_budget_usd"] == pytest.approx(0.3)  # kẹp theo trần ngày của agent
    assert chain_budget_left(hub.store, run) == pytest.approx(0.3 - 0.24, rel=1e-3)


async def test_crash_without_usage_keeps_cost_empty(make_hub):
    hub, _ = make_hub(lambda spec: {"crash": True})
    hub.agents.create({"id": "a", "name": "A"})
    r = hub.runs.submit("a", "x")
    await hub.runs.wait(r["id"], 5)
    run = hub.store.get_run(r["id"])
    assert run["status"] == "crashed" and run["cost_usd"] is None


async def test_completed_run_cost_unchanged(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok", "cost": 0.05, "stream_usage": {"input": 100_000, "output": 10_000}})
    hub.agents.create({"id": "a", "name": "A"})
    r = hub.runs.submit("a", "x")
    await hub.runs.wait(r["id"], 5)
    run = hub.store.get_run(r["id"])
    assert run["cost_usd"] == pytest.approx(0.05) and run["cost_basis"] != "estimated_no_result"  # số chuẩn = result


# ============================================================================ 4. notify mặc định + định dạng
def test_budget_events_in_default_notify_lists():
    for ch in ("telegram", "slack"):
        evl = DEFAULTS["notify"][ch]["events"]
        assert "budget.warning" in evl and "budget.exceeded" in evl
    assert EVENT_LEVELS["task.requeue_exhausted"] == "summary"


def test_budget_notify_format():
    w = format_message(make_event("budget.warning", "boss", {"scope": "manager", "owner": "boss", "used": 1.6, "limit": 2.0, "pct": 80.0}))
    assert "điều phối viên" in w and "$1.60/$2.00" in w and "80.0%" in w
    x = format_message(make_event("budget.exceeded", None, {"scope": "hub", "owner": "hub", "used": 5, "limit": 5}))
    assert "toàn hub" in x and "chặn" in x and "chờ duyệt vẫn giữ" in x


async def test_budget_warning_once_per_day_per_scope(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok", "cost": 0.03})
    hub.agents.create({"id": "a", "name": "A", "limits": {"budget_usd_per_day": 0.1}})
    for i in range(2):  # 0.03 → 0.06
        r = hub.runs.submit("a", f"x{i}")
        await hub.runs.wait(r["id"], 5)
    hub.config.set("budget.warn_pct", 50)
    for i in (3, 4):  # 0.09 (cảnh báo) → 0.12 (vượt; cờ cảnh báo hôm nay đã đặt)
        r = hub.runs.submit("a", f"x{i}")
        await hub.runs.wait(r["id"], 5)
    assert len([e for e in evs(hub, "budget.warning") if e.payload["scope"] == "agent"]) == 1
    with pytest.raises(Blocked):
        hub.runs.submit("a", "x5")
    with pytest.raises(Blocked):
        hub.runs.submit("a", "x6")
    assert len([e for e in evs(hub, "budget.exceeded") if e.payload["scope"] == "agent"]) == 1


# ============================================================================ 5. ước tính $/ngày của lịch theo chi phí thật
async def test_schedule_estimate_uses_recent_real_cost(hub_home):
    d = await make_daemon(hub_home)
    try:
        d.hub.schedules.stop()
        res = await manager_run(d)
        rid = res["run_id"]
        d.hub.config.set("schedules.manager_daily_budget_usd", 2.0)
        a = await tool(d, rid, "schedule_create", {"name": "A", "expr": "hourly :05", "prompt": "check A", "budget_usd": 0.05})
        sch = d.hub.schedules
        assert sch.estimate_daily_usd("hourly :05", 0.05, schedule_id=a["id"]) == pytest.approx(1.2)  # chưa có lịch sử → trần
        with pytest.raises(HubError) as ei:  # 1.2 (B) + 1.2 (A theo trần) > 2.0
            await tool(d, rid, "schedule_create", {"name": "B", "expr": "hourly :35", "prompt": "check B", "budget_usd": 0.05})
        assert "ước tính chi phí ngày" in ei.value.message
        conn = d.hub.store.conn
        for _ in range(2):
            conn.execute("INSERT INTO schedule_fires (schedule_id, fired_at, status, cost_usd) VALUES (?, '2026-11-10T00:00:00Z', 'completed', 0.01)", (a["id"],))
        assert sch.fire_cost_estimate(a["id"], 0.05) == pytest.approx(0.05)  # < estimate_min_fires (3) → vẫn trần
        conn.execute("INSERT INTO schedule_fires (schedule_id, fired_at, status, cost_usd) VALUES (?, '2026-11-10T01:00:00Z', 'failed', 0.01)", (a["id"],))
        assert sch.fire_cost_estimate(a["id"], 0.05) == pytest.approx(0.01)
        assert sch.estimate_daily_usd("hourly :05", 0.05, schedule_id=a["id"]) == pytest.approx(0.24)
        b = await tool(d, rid, "schedule_create", {"name": "B", "expr": "hourly :35", "prompt": "check B", "budget_usd": 0.05})
        assert b["id"]
        # chi phí thật không bao giờ vượt trần của lịch
        conn.execute("INSERT INTO schedule_fires (schedule_id, fired_at, status, cost_usd) VALUES (?, '2026-11-10T02:00:00Z', 'completed', 9.0)", (a["id"],))
        assert sch.fire_cost_estimate(a["id"], 0.05) == pytest.approx(0.05)
        d.hub.config.set("schedules.estimate_recent_fires", 0)  # tắt → luôn theo trần
        assert sch.fire_cost_estimate(a["id"], 0.05) == pytest.approx(0.05)
    finally:
        await close(d)
