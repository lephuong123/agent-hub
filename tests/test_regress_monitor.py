"""M4-B: theo dõi hồi quy + tự rollback (FR-LN-17). Không gọi claude: chèn run/signal trực tiếp + FakeLedger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cfharness.events import iso
from cfharness.hub import Hub
from cfharness.learning_ops.regression import evaluate
from m3_fake import ScriptedRuntime
from m4b_fake import attach_fake_ledger

CFG = {"threshold_pct": 50, "min_events": 2, "cost_threshold_pct": 50, "min_runs_cost": 3, "unevaluated_factor": 0.5}


def test_evaluate_pure():
    base = {"runs": 10, "correction": 0, "failure": 0, "cost_avg": 0.01}
    assert evaluate({"runs": 10, "correction": 1, "failure": 0, "cost_avg": 0.01}, base, CFG) == []  # dưới min_events
    v = evaluate({"runs": 10, "correction": 3, "failure": 0, "cost_avg": 0.01}, base, CFG)
    assert [x["metric"] for x in v] == ["correction"] and v[0]["window_rate"] == 0.3
    # mốc 20% → cần > 30%
    base2 = {"runs": 10, "correction": 2, "failure": 0, "cost_avg": 0.01}
    assert evaluate({"runs": 10, "correction": 3, "failure": 0}, base2, CFG) == []
    assert evaluate({"runs": 10, "correction": 4, "failure": 0}, base2, CFG)[0]["metric"] == "correction"
    # failure
    assert evaluate({"runs": 5, "correction": 0, "failure": 2}, base, CFG)[0]["metric"] == "failure"
    # cost +60% với ≥3 run mỗi phía
    assert evaluate({"runs": 3, "correction": 0, "failure": 0, "cost_avg": 0.016}, base, CFG)[0]["metric"] == "cost"
    assert evaluate({"runs": 2, "correction": 0, "failure": 0, "cost_avg": 0.03}, base, CFG) == []
    # unevaluated chặt hơn: mốc 20%, ngưỡng còn +25% → 3/10 (30%) > 25% → vi phạm
    assert evaluate({"runs": 10, "correction": 3, "failure": 0}, base2, CFG, unevaluated=True)[0]["metric"] == "correction"
    assert evaluate({"runs": 0}, base, CFG) == []


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "a", "name": "A", "model": "sonnet"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


def add_run(h, rid, when: datetime, status="completed", cost=0.01, kind="adhoc", correction=False):
    h.store.insert_run({"id": rid, "agent_id": "a", "kind": kind, "prompt": "x", "status": status, "outcome": status, "queued_at": iso(when),
                        "ended_at": iso(when + timedelta(seconds=5)), "cost_usd": cost})
    if correction:
        h.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?)",
                             ("sg_" + rid, "a", rid, "correction", "cli", "{}", iso(when + timedelta(seconds=10))))


async def test_correction_spike_rolls_back_latest_change(hub):
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    for i in range(6):
        add_run(hub, f"r_b{i}", now - timedelta(days=2, minutes=i))  # mốc: 6 run, 0 correction
    e = lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)))
    evs = []
    hub.bus.add_listener(lambda ev: evs.append(ev))
    add_run(hub, "r_w0", now - timedelta(minutes=50), correction=True)
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts == []  # mới 1 sự kiện (< min_events)
    add_run(hub, "r_w1", now - timedelta(minutes=40), correction=True)
    add_run(hub, "r_w2", now - timedelta(minutes=30))
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts and acts[0]["action"] == "rollback" and acts[0]["entry_id"] == e["id"]
    assert lg.rollbacks == [(e["id"], "regression_monitor", acts[0]["reason"])]
    assert "correction" in acts[0]["reason"] and hub.agents.get_row("a")["model"] == "sonnet"
    w = hub.learning_ops.regression.list("a")[0]
    assert w["status"] == "rolled_back" and w["observed"]["correction"] == 2
    det = [x for x in evs if x.type == "regression.detected"]
    assert det and det[0].payload["rolled_back"] is True
    assert await hub.learning_ops.regression.check_agent("a") == []  # đã rollback → không lặp


async def test_cancelled_runs_count_and_eval_runs_ignored(hub):
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    add_run(hub, "r_b0", now - timedelta(days=1))
    lg.apply_config("a", {"effort": "low"}, applied_at=iso(now - timedelta(hours=1)))
    for i in range(5):
        add_run(hub, f"r_e{i}", now - timedelta(minutes=50 - i), status="failed", kind="eval")  # run eval không tính
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts == []
    add_run(hub, "r_c0", now - timedelta(minutes=20), status="cancelled")
    add_run(hub, "r_c1", now - timedelta(minutes=10), status="cancelled")
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts and acts[0]["action"] == "rollback"


async def test_window_passes_without_regression(hub):
    hub.config.set("eval.regression", {**hub.config.get("eval.regression"), "watch_runs": 3})
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    for i in range(3):
        add_run(hub, f"r_b{i}", now - timedelta(days=1, minutes=i))
    e = lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)))
    for i in range(3):
        add_run(hub, f"r_w{i}", now - timedelta(minutes=30 - i))
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts == [{"entry_id": e["id"], "action": "passed"}] and not lg.rollbacks


async def test_rolled_back_elsewhere_is_gone(hub):
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    e = lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)))
    await hub.learning_ops.regression.sync_watches()
    lg.rollback(e["id"], actor="user", reason="tay")
    await hub.learning_ops.regression.sync_watches()
    assert hub.learning_ops.regression.list("a")[0]["status"] == "gone"


async def test_tick_runs_regression_on_pending(hub):
    lg = attach_fake_ledger(hub)
    hub.config.set("digest.enabled", False)
    hub.config.set("curator.enabled", False)
    hub.config.set("eval.batch_enabled", False)
    now = datetime.now(timezone.utc)
    lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)))
    add_run(hub, "r_w0", now - timedelta(minutes=20), status="failed")
    add_run(hub, "r_w1", now - timedelta(minutes=10), status="failed")
    from cfharness.events import make_event

    hub.publish(make_event("run.failed", "a", {}, run_id="r_w1"))
    out = await hub.learning_ops.tick()
    assert out["regression"][0]["action"] == "rollback"


async def test_human_change_is_never_auto_rolled_back(hub):
    """Thay đổi của NGƯỜI (ledger actor=user) gây hồi quy → chỉ cảnh báo (digest/Slack), không rollback."""
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    add_run(hub, "r_b0", now - timedelta(days=1))
    e = lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)), actor="user")
    evs = []
    hub.bus.add_listener(lambda ev: evs.append(ev))
    add_run(hub, "r_w0", now - timedelta(minutes=20), correction=True)
    add_run(hub, "r_w1", now - timedelta(minutes=10), correction=True)
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts[0]["action"] == "warned_human" and not lg.rollbacks and hub.agents.get_row("a")["model"] == "haiku"
    assert hub.learning_ops.regression.list("a")[0]["status"] == "warned"
    det = [x for x in evs if x.type == "regression.detected"][0]
    assert det.payload["human_change"] and not det.payload["rolled_back"] and det.payload["hint"] == f"cfharness learn rollback {e['id']}"
    assert await hub.learning_ops.regression.check_agent("a") == []  # cảnh báo một lần
    from cfharness.notify import format_message

    assert "KHÔNG tự rollback" in format_message(det) or "không tự rollback" in format_message(det)
    d = hub.learning_ops.digest.run(since="2000-01-01T00:00:00Z", send=False)
    assert "thay đổi của NGƯỜI" in d["text"] and d["counts"]["human_warnings"] == 1


async def test_human_change_skipped_but_learning_change_rolled_back(hub):
    lg = attach_fake_ledger(hub)
    now = datetime.now(timezone.utc)
    add_run(hub, "r_b0", now - timedelta(days=1))
    auto = lg.apply_config("a", {"effort": "low"}, applied_at=iso(now - timedelta(hours=2)), actor="review")
    lg.apply_config("a", {"model": "haiku"}, applied_at=iso(now - timedelta(hours=1)), actor="user")  # mới hơn nhưng của người
    add_run(hub, "r_w0", now - timedelta(minutes=20), status="failed")
    add_run(hub, "r_w1", now - timedelta(minutes=10), status="failed")
    acts = await hub.learning_ops.regression.check_agent("a")
    assert acts[0]["action"] == "rollback" and acts[0]["entry_id"] == auto["id"]
    assert [r[0] for r in lg.rollbacks] == [auto["id"]] and hub.agents.get_row("a")["model"] == "haiku"
