"""M5-B: overhead học NFR-19 (báo cáo + bench FakeRuntime) và báo cáo KPI G-4. Không gọi claude."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from cfharness.curation.kpi import kpi_report, overhead_report, render_kpi, render_overhead
from cfharness.curation.overhead import bench
from cfharness.events import iso
from cfharness.hub import Hub
from m3_fake import ScriptedRuntime

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def hub(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}}))
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "a", "name": "A"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


def _run(h, kind, cost, ms, at, agent="a"):
    rid = "r_" + uuid.uuid4().hex[:10]
    h.store.insert_run({"id": rid, "agent_id": agent, "kind": kind, "prompt": "x", "status": "completed", "outcome": "completed", "queued_at": iso(at),
                        "started_at": iso(at), "ended_at": iso(at), "cost_usd": cost, "duration_ms": ms})
    return rid


def _sig(h, kind, at, run_id=None, agent="a"):
    h.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?)",
                         ("sg_" + uuid.uuid4().hex[:10], agent, run_id, kind, "test", "{}", iso(at)))


def test_overhead_report(hub):
    for i in range(10):
        _run(hub, "adhoc", 0.10, 10_000, T0)
    _run(hub, "review", 0.08, 900, T0)
    _run(hub, "eval", 0.03, 300, T0)
    _run(hub, "eval_judge", 0.01, 100, T0)
    _run(hub, "curator", 0.02, 200, T0)
    _run(hub, "human_takeover", 5.0, 1, T0)  # không tính vào cả hai phía
    r = overhead_report(hub)
    t = r["total"]
    assert t["work_runs"] == 10 and t["learn_runs"] == 4
    assert t["cost_overhead_pct"] == pytest.approx(14.0) and t["time_overhead_pct"] == pytest.approx(1.5) and t["met"] is True
    _run(hub, "review", 0.5, 1, T0)
    assert overhead_report(hub)["total"]["met"] is False
    assert "VƯỢT" in render_overhead(overhead_report(hub))


def test_kpi_g4_repeat_corrections_drop(hub):
    # tuần 1-2: 10 run/tuần, 4 correction cùng (agent, adhoc, correction) mỗi tuần → lặp nhiều
    for w in range(6):
        base = T0 + timedelta(days=7 * w)
        runs = [_run(hub, "adhoc", 0.01, 1000, base + timedelta(hours=i)) for i in range(10)]
        n = 4 if w < 2 else (1 if w >= 4 else 2)
        for i in range(n):
            _sig(hub, "correction", base + timedelta(hours=i, minutes=30), runs[i])
    # mốc mặc định = ledger/Signal đầu tiên
    assert kpi_report(hub, now=T0 + timedelta(days=42))["start"].startswith("2026-08-01T09:30")
    r = kpi_report(hub, since=iso(T0), weeks=6)
    assert r["start"].startswith("2026-08-01") and len(r["weeks"]) == 6
    w1, w2 = r["weeks"][0], r["weeks"][1]
    assert w1["corrections"] == 4 and w1["repeated"] == 3 and w2["repeated"] == 4
    rc = r["repeat_correction"]
    assert rc["baseline_weeks_1_2"] == pytest.approx(7 / 20) and rc["after_week_4"] == pytest.approx(2 / 20)
    assert rc["reduction_pct"] == pytest.approx(71.4, abs=0.1) and rc["met"] is True
    m = r["metrics"]
    assert m["pending_approval_proposals"]["value"] == 0 and m["pending_approval_proposals"]["met"]
    txt = render_kpi(r)
    assert "giảm 71.4%" in txt and "proposal chờ duyệt: 0" in txt


def test_kpi_rollback_metrics(hub):
    # 2 thay đổi học, 1 bị người rollback
    from cfharness.learning.ledger import Change

    led = hub.learning.ledger
    p = hub.knowledge.memory_path("agent", "a")
    e1 = led.apply(Change(scope="agent", owner_id="a", target="memory", target_ref="MEMORY.md", op="add", actor="review", kind="file", path=p, after=b"# Memory\nx\n", agent_id="a"))
    led.apply(Change(scope="agent", owner_id="a", target="memory", target_ref="USER.md", op="add", actor="review", kind="file",
                     path=hub.knowledge.memory_path("agent", "a", "user"), after=b"# User\ny\n", agent_id="a"))
    led.rollback(e1["id"], actor="user")
    m = kpi_report(hub)["metrics"]
    assert m["learning_changes"] == 2 and m["rollback_rate_pct"]["value"] == 50.0 and m["manual_rollback_rate_pct"]["value"] == 50.0
    assert m["rollback_rate_pct"]["met"] is False


def test_kpi_empty(hub):
    r = kpi_report(hub)
    assert r["start"] is None and "chưa có" in render_kpi(r)


async def test_bench_fake_runtime(tmp_path):
    r = await bench(tmp_path / "bench", n=12, line_delay_s=0.002)
    assert r["off"]["learn_runs"] == 0 and r["on"]["runs"] == 12
    # CPU của listener học trên bus ≤ 15% thời gian tường (NFR-19, phần hub)
    assert r["hub_learning_cpu_pct"] is not None and r["hub_learning_cpu_pct"] <= 15.0, json.dumps(r)
