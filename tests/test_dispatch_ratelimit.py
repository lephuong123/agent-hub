"""Dispatcher + rate limit (FR-RT-16) — lỗi thấy trên dữ liệu thật:

1. Run REVIEW kết thúc `rate_limited` rơi vào `_on_failure` → 2 lần là circuit open → task `blocked` oan (phải mở tay).
2. Retry sau rate limit bỏ qua thời điểm reset: backoff trần 10 phút → ~15 lần spawn `claude` hỏng ngay trong cửa sổ
   session-limit 1h48. Nay: biết `resetsAt` (event `ratelimit.update` rejected / chữ lỗi "resets 6am") → hẹn đúng lúc reset
   + đóng cổng toàn hub (không claim gì tới lúc đó), không tính vào circuit breaker hay `attempts`.

Không gọi claude: `ScriptedRuntime`."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from cfharness.dispatch.dispatcher import RL_GATE_KEY, reset_from_info, reset_from_text
from cfharness.hub import Hub
from m3_fake import ScriptedRuntime, kind_of

RL_ERR = {"subtype": "error_during_execution", "error_text": "API Error: rate limit exceeded", "api_error_status": 429}


async def settle(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond():
            return True
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script):
        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        h.config.set("tasks.rate_limit_backoff_s", 0.05)
        h.config.set("tasks.rate_limit_backoff_max_s", 0.1)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def mk_team(hub, *, lead=False, reviewer=False, policies=None):
    members = []
    if lead:
        hub.agents.create({"id": "lead", "name": "Lead", "role": "planner", "description": "Plans and splits work"})
        members.append({"agent_id": "lead", "role": "lead"})
    for aid in ("writer", "researcher"):
        hub.agents.create({"id": aid, "name": aid, "role": aid, "description": f"{aid} work"})
        members.append({"agent_id": aid, "role": "worker"})
    if reviewer:
        hub.agents.create({"id": "rev", "name": "Rev", "role": "reviewer", "description": "Reviews results"})
        members.append({"agent_id": "rev", "role": "reviewer"})
    return hub.teams.create({"id": "docs", "name": "Docs", "charter": "c", "policies": policies or {}, "members": members})


def runs_of(hub, kind=None):
    rs = sorted(hub.store.list_runs(), key=lambda r: r["queued_at"])
    return [r for r in rs if kind is None or r["kind"] == kind]


# ============================================================================ 1. review rate_limited ≠ lỗi
async def test_review_rate_limited_retries_without_circuit_breaker(make_hub):
    n = {"rev": 0}

    def script(spec):
        if kind_of(spec) == "review":
            n["rev"] += 1
            if n["rev"] <= 3:  # > failure_limit (2): trước đây task bị blocked "circuit open"
                return RL_ERR
            return {"structured": {"verdict": "approve", "comment": "good"}}
        return {"text": "draft"}

    hub, _ = make_hub(script)
    mk_team(hub, reviewer=True, policies={"review_required": True})
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "doc", "body": "write doc"})
    await settle(lambda: hub.tasks.row(t["id"])["status"] in ("done", "blocked"), timeout=10)
    row = hub.tasks.row(t["id"])
    assert row["status"] == "done", row.get("block_reason")
    assert int(row["consecutive_failures"] or 0) == 0
    assert [r["outcome"] for r in runs_of(hub, "review")] == ["rate_limited"] * 3 + ["completed"]
    assert not any(e.type == "task.circuit_open" for e in hub.store.query_events())


# ============================================================================ 2. tôn trọng thời điểm reset
async def test_worker_rate_limited_waits_for_reset_and_gates_hub(make_hub):
    reset = int(time.time()) + 3600
    info = {"status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour"}
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        if n["i"] == 1:
            return {**RL_ERR, "rate_limit": info}
        return {"text": "ok"}

    hub, _ = make_hub(script)
    mk_team(hub)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["rate_limit_hits"] == 1)
    row = hub.tasks.row(t["id"])
    nb = datetime.fromisoformat(row["not_before"].replace("Z", "+00:00"))
    reset_dt = datetime.fromtimestamp(reset, tz=timezone.utc)
    assert reset_dt <= nb <= reset_dt + timedelta(seconds=31)  # đúng lúc reset + jitter, KHÔNG phải backoff 0.05 s
    assert row["status"] == "ready" and int(row["consecutive_failures"] or 0) == 0 and row["attempts"] == 1
    gate = hub.store.state_get(RL_GATE_KEY)
    assert gate and datetime.fromisoformat(gate["until"].replace("Z", "+00:00")) == reset_dt
    assert hub.status()["rate_limit_gate"]["until"] == gate["until"]
    assert any(e.type == "ratelimit.gate" and e.payload.get("open") is False for e in hub.store.query_events())

    # cổng toàn hub: task MỚI (không backoff) cũng không được claim khi hạn mức đang rejected
    t2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "other", "body": "y"})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "rate_limit_reset")
    await asyncio.sleep(0.3)
    assert len(runs_of(hub)) == 1  # không spawn claude nào trong cửa sổ limit
    assert hub.tasks.row(t2["id"])["status"] == "ready"

    # tới lúc reset → cổng mở, cả hai chạy; lần chạy lại không tính vào attempts
    hub.store.state_set(RL_GATE_KEY, {**gate, "until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    hub.tasks.update(t["id"], {"not_before": None})
    hub.dispatcher.kick()
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done" and hub.tasks.row(t2["id"])["status"] == "done")
    assert hub.tasks.row(t["id"])["attempts"] == 1
    assert hub.store.state_get(RL_GATE_KEY) is None
    assert any(e.type == "ratelimit.gate" and e.payload.get("open") is True for e in hub.store.query_events())


async def test_reset_from_error_text_when_no_event(make_hub):
    def script(spec):
        return {"subtype": "error_during_execution", "error_text": "You've hit your session limit · resets 6am", "api_error_status": 429}

    hub, _ = make_hub(script)
    mk_team(hub)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["rate_limit_hits"] == 1)
    await asyncio.sleep(0.3)
    assert len(runs_of(hub)) == 1  # không retry dồn dập
    row = hub.tasks.row(t["id"])
    nb = datetime.fromisoformat(row["not_before"].replace("Z", "+00:00")).astimezone()
    assert nb.hour == 6 and 0 <= (nb - datetime.now().astimezone()).total_seconds() <= 86400 + 31
    assert row["wait_reason"] == "rate_limit_reset"
    assert hub.store.state_get(RL_GATE_KEY)["source"] == "error_text"


async def test_lead_rate_limited_waits_for_reset(make_hub):
    reset = int(time.time()) + 1800
    hub, _ = make_hub(lambda spec: {**RL_ERR, "rate_limit": {"status": "rejected", "resetsAt": reset}})
    mk_team(hub, lead=True)
    t = hub.tasks.create({"team": "docs", "title": "big", "body": "plan it"})
    await settle(lambda: int(hub.tasks.row(t["id"])["rate_limit_hits"] or 0) == 1)
    row = hub.tasks.row(t["id"])
    assert row["status"] == "triage" and int(row["consecutive_failures"] or 0) == 0
    nb = datetime.fromisoformat(row["not_before"].replace("Z", "+00:00"))
    assert nb >= datetime.fromtimestamp(reset, tz=timezone.utc)


async def test_hub_rejected_event_from_other_run_closes_gate(make_hub):
    """Trạng thái rejected toàn hub (vd từ run ad-hoc) → dispatcher không claim task nào tới lúc reset."""
    reset = int(time.time()) + 600
    hub, _ = make_hub(lambda spec: {"text": "ok", "rate_limit": {"status": "rejected", "resetsAt": reset}})
    mk_team(hub)
    r = hub.runs.submit("writer", "adhoc")
    await hub.runs.wait(r["id"], 5)
    assert hub.store.state_get(RL_GATE_KEY)["source"] == "event"
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "x", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["wait_reason"] == "rate_limit_reset")
    assert len(runs_of(hub)) == 1


# ============================================================================ hàm thuần
def test_reset_from_text_variants():
    now = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
    assert reset_from_text("Claude AI usage limit reached|1792324800", now) == datetime.fromtimestamp(1792324800, tz=timezone.utc)
    d = reset_from_text("5-hour limit reached ∙ resets 6am (UTC)", now)
    assert d == datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
    d = reset_from_text("limit · resets 2:30am (UTC)", now)
    assert d == datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc)  # đã qua hôm nay → ngày mai
    d = reset_from_text("resets 6pm (Asia/Ho_Chi_Minh)", now)
    assert d == datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc)
    assert reset_from_text("rate limit exceeded", now) is None
    assert reset_from_text("resets 6", now) is None
    assert reset_from_text(None, now) is None


def test_reset_from_info_only_when_rejected():
    assert reset_from_info({"status": "allowed", "resetsAt": 1792324800}) is None
    assert reset_from_info({"status": "rejected", "resetsAt": 1792324800}) == datetime.fromtimestamp(1792324800, tz=timezone.utc)
    w = {"status": "rejected", "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1792324800}, "seven_day": {"utilization": 0.3, "resetsAt": 1792900000}}}
    assert reset_from_info(w) == datetime.fromtimestamp(1792324800, tz=timezone.utc)
