"""Tự resume có giới hạn run ad-hoc dừng vì lỗi tạm thời + thống kê run treo (auto_resume.py, docs/dev-notes/run-auto-resume.md).
Không gọi claude: `ScriptedRuntime` (tests/m3_fake.py)."""

import asyncio
import time
import uuid

import pytest

from cfharness.auto_resume import chain_attempts, hang_stats, transient_cause
from cfharness.events import make_event
from cfharness.hub import Hub
from cfharness.store import now_iso
from m3_fake import ScriptedRuntime


async def settle(cond=None, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond is not None and cond():
            return True
    if cond is not None:
        raise AssertionError("hết giờ chờ điều kiện")
    return False


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script=None, **cfg):
        rt = ScriptedRuntime(script or (lambda spec: {"text": "ok"}))
        h = Hub(hub_home, runtime=rt)
        h.config.set("recovery.auto_resume_backoff_s", 0)
        for k, v in cfg.items():
            h.config.set(k, v)
        h.start()
        h.agents.create({"id": "a", "name": "A"})
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.auto_resume.stop()
        h.store.close()


def chain(hub):
    return sorted(hub.store.list_runs(agent_id="a", limit=100), key=lambda r: r["queued_at"])


def ar_events(hub, run_id=None):
    return [e.payload for e in hub.store.query_events(run_id=run_id) if e.type == "run.auto_resume"]


def audit_rows(hub):
    return [dict(r) for r in hub.store.conn.execute("SELECT * FROM audit_log WHERE kind='run.auto_resume' ORDER BY id").fetchall()]


def _row(**kw):
    return {"id": "r_" + uuid.uuid4().hex[:10], "agent_id": "a", "kind": "adhoc", "prompt": "p", "session_id": str(uuid.uuid4()), "queued_at": now_iso(), **kw}


# --------------------------------------------------------------------------- phân loại nguyên nhân (danh sách cho phép)
@pytest.mark.parametrize("run,cause", [
    ({"outcome": "crashed"}, "process_killed"),
    ({"outcome": "timed_out"}, "timeout"),
    ({"outcome": "rate_limited"}, "rate_limit"),
    ({"outcome": "interrupted"}, "daemon_restart"),
    ({"outcome": "failed", "error": "API Error: 529 overloaded_error"}, "network"),
    ({"outcome": "failed", "error": "request failed: ECONNRESET"}, "network"),
    ({"outcome": "failed", "error": "Connection error."}, "network"),
    ({"outcome": "budget_exceeded"}, None),
    ({"outcome": "cancelled"}, None),  # người dừng / ESTOP
    ({"outcome": "blocked"}, None),
    ({"outcome": "protocol_violation"}, None),
    ({"outcome": "completed"}, None),
    ({"outcome": "failed", "error": "guard: thao tác bị từ chối"}, None),
    ({"outcome": "failed", "error": "Invalid API key · Please run /login"}, None),
    ({"outcome": "failed", "error": "Reached max budget"}, None),
])
def test_transient_cause_allowlist(run, cause):
    assert transient_cause(run) == cause


# --------------------------------------------------------------------------- e2e
async def test_crashed_adhoc_run_resumes_same_session(make_hub):
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        return {"crash": n["i"] == 1, "text": "ok"}

    hub, rt = make_hub(script)
    r0 = hub.runs.submit("a", "làm việc")
    await settle(lambda: len(chain(hub)) == 2 and chain(hub)[1]["status"] == "completed")
    old, new = chain(hub)
    assert old["outcome"] == "crashed" and old["continued_in_run_id"] == new["id"] and old["id"] == r0["id"]
    assert new["parent_run_id"] == old["id"] and new["resume_reason"] == "auto_resume:process_killed"
    assert rt.specs[new["id"]].resume_session_id == old["session_id"]  # --resume session cũ
    assert [p["status"] for p in ar_events(hub, old["id"])] == ["scheduled", "started"]
    assert any(e.type == "run.resumed" and e.payload["reason"] == "auto_resume:process_killed" for e in hub.store.query_events(run_id=old["id"]))
    assert len(audit_rows(hub)) == 2 and all(a["run_id"] == old["id"] for a in audit_rows(hub))


async def test_timed_out_adhoc_run_resumes(make_hub):
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        return {"hold": n["i"] == 1, "text": "ok"}

    hub, _ = make_hub(script)
    hub.runs.submit("a", "chậm", max_runtime_s=0.3)
    await settle(lambda: [r["outcome"] for r in chain(hub)] == ["timed_out", "completed"], timeout=8)
    assert chain(hub)[1]["resume_reason"] == "auto_resume:timeout"


async def test_bounded_attempts_then_exhausted(make_hub):
    hub, _ = make_hub(lambda spec: {"crash": True}, **{"recovery.auto_resume_max_attempts": 2})
    hub.runs.submit("a", "luôn chết")
    await settle(lambda: any(p["status"] == "exhausted" for p in ar_events(hub)), timeout=8)
    await settle(timeout=0.3)
    runs = chain(hub)
    assert len(runs) == 3 and all(r["outcome"] == "crashed" for r in runs)  # 1 run gốc + đúng 2 lần resume
    assert chain_attempts(hub.store, runs[-1]) == 2
    ex = [p for p in ar_events(hub, runs[-1]["id"]) if p["status"] == "exhausted"]
    assert ex and ex[0]["max_attempts"] == 2
    assert not runs[-1].get("continued_in_run_id")


@pytest.mark.parametrize("behavior", [
    {"subtype": "error_max_budget_usd", "error_text": "Reached max budget"},
    {"subtype": "error_during_execution", "error_text": "something broke"},
])
async def test_non_transient_failures_not_resumed(make_hub, behavior):
    hub, _ = make_hub(lambda spec: behavior)
    hub.runs.submit("a", "x")
    await settle(lambda: chain(hub) and chain(hub)[0]["status"] not in ("queued", "running"))
    await settle(timeout=0.3)
    assert len(chain(hub)) == 1 and not ar_events(hub)


async def test_network_failure_resumed(make_hub):
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        return {"subtype": "error_during_execution", "error_text": "API Error: Connection error."} if n["i"] == 1 else {"text": "ok"}

    hub, _ = make_hub(script)
    hub.runs.submit("a", "x")
    await settle(lambda: [r["outcome"] for r in chain(hub)] == ["failed", "completed"])
    assert chain(hub)[1]["resume_reason"] == "auto_resume:network"


async def test_human_cancel_not_resumed(make_hub):
    hub, _ = make_hub(lambda spec: {"hold": True})
    r = hub.runs.submit("a", "x")
    await settle(lambda: hub.store.get_run(r["id"])["status"] == "running")
    await hub.runs.stop(r["id"])
    await settle(lambda: hub.store.get_run(r["id"])["status"] == "cancelled")
    await settle(timeout=0.3)
    assert len(chain(hub)) == 1 and not ar_events(hub)


async def test_disabled_and_task_runs_ignored(make_hub):
    hub, _ = make_hub(**{"recovery.auto_resume_enabled": False})
    row = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso())
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) is None
    hub.config.set("recovery.auto_resume_enabled", True)
    t = _row(status="crashed", outcome="crashed", task_id="t_x")
    hub.store.insert_run(t)
    assert hub.auto_resume.consider(t["id"]) is None  # dispatcher lo retry của task
    m = _row(status="crashed", outcome="crashed", kind="manager")
    hub.store.insert_run(m)
    assert hub.auto_resume.consider(m["id"]) is None


async def test_fire_is_idempotent_and_respects_estop(make_hub):
    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 60})
    row = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso())
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) == "scheduled"
    assert row["id"] in hub.store.state_get("auto_resume_pending")
    await hub.runs.set_estop(True)
    assert hub.auto_resume.fire(row["id"]) is None
    await hub.runs.set_estop(False)
    assert ar_events(hub, row["id"])[-1]["status"] == "skipped" and "ESTOP" in ar_events(hub, row["id"])[-1]["reason"]
    assert row["id"] not in (hub.store.state_get("auto_resume_pending") or {})
    nr = hub.auto_resume.fire(row["id"])
    assert nr is not None
    assert hub.auto_resume.fire(row["id"]) is None  # lần hai: đã continued_in_run_id → không chạy đôi
    assert hub.auto_resume.consider(row["id"]) is None
    assert sum(1 for r in chain(hub) if r.get("parent_run_id") == row["id"]) == 1


async def test_pending_survives_daemon_restart(make_hub, hub_home):
    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 3600})
    row = _row(status="timed_out", outcome="timed_out", started_at=now_iso(), ended_at=now_iso())
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) == "scheduled"
    hub.store.state_set("auto_resume_pending", {row["id"]: time.time()})  # tới hạn trong lúc daemon tắt
    hub.runs.shutting_down = True
    hub.auto_resume.stop()
    hub.store.close()
    h2 = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    try:
        h2.start()
        await settle(lambda: (h2.store.get_run(row["id"]) or {}).get("continued_in_run_id"))
        new = h2.store.get_run(h2.store.get_run(row["id"])["continued_in_run_id"])
        assert new["resume_reason"] == "auto_resume:timeout" and new["session_id"] == row["session_id"]
    finally:
        h2.runs.shutting_down = True
        h2.auto_resume.stop()
        h2.store.close()


async def test_daemon_restart_resume_shares_bound(hub_home):
    """recovery.resume_interrupted: chuỗi đã hết lượt (daemon chết lặp lại) → không resume nữa, event exhausted."""
    h1 = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    h1.config.set("recovery.auto_resume_max_attempts", 1)
    h1.agents.create({"id": "a", "name": "A"})
    root = _row(status="interrupted", outcome="interrupted", started_at=now_iso(), ended_at=now_iso())
    h1.store.insert_run(root)
    child = _row(status="running", started_at=now_iso(), session_id=root["session_id"], parent_run_id=root["id"], resume_reason="daemon_restart")
    h1.store.insert_run(child)
    h1.store.close()
    h2 = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    try:
        h2.start()
        await settle(timeout=0.3)
        c = h2.store.get_run(child["id"])
        assert c["status"] == "interrupted" and not c.get("continued_in_run_id")
        assert [p["status"] for p in ar_events(h2, child["id"])] == ["exhausted"]
    finally:
        h2.runs.shutting_down = True
        h2.auto_resume.stop()
        h2.store.close()


# --------------------------------------------------------------------------- thống kê
def test_hang_stats_groups(hub_home):
    hub = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {}))
    try:
        hub.agents.create({"id": "a", "name": "A"})
        hub.agents.create({"id": "b", "name": "B"})
        t0 = "2026-10-20T10:00:00.000Z"
        rows = [
            _row(status="timed_out", outcome="timed_out", started_at=t0, ended_at="2026-10-20T10:20:00.000Z"),
            _row(status="timed_out", outcome="timed_out", kind="task", task_id="t1", started_at=t0, ended_at="2026-10-20T10:20:30.000Z"),
            _row(agent_id="b", status="interrupted", outcome="interrupted", started_at=t0, ended_at="2026-10-20T10:00:30.000Z"),
            _row(status="completed", outcome="completed", started_at=t0, ended_at=t0),
            _row(status="queued", resume_reason="auto_resume:timeout"),
        ]
        for r in rows:
            r["queued_at"] = t0
            hub.store.insert_run(r)
        for tid, name, typ in (("x1", "Bash", "tool.start"), ("x1", "Bash", "tool.complete"), ("x2", "WebFetch", "tool.start")):
            hub.publish(make_event(typ, "a", {"tool_id": tid, "name": name}, run_id=rows[0]["id"]))
        s = hang_stats(hub.store.conn, "2026-10-01T00:00:00.000Z")
        assert s["hung"] == 3 and s["runs_total"] == 5
        assert {x["key"]: x["count"] for x in s["by_agent"]} == {"a": 2, "b": 1}
        assert {x["key"]: x["count"] for x in s["by_kind"]} == {"adhoc": 2, "task": 1}
        assert {x["key"]: x["count"] for x in s["by_outcome"]} == {"timed_out": 2, "interrupted": 1}
        assert {"key": "WebFetch", "count": 1} in s["by_tool"]  # tool đang dở (Bash đã xong)
        assert {x["key"]: x["count"] for x in s["by_duration"]} == {"10–30 phút": 2, "<1 phút": 1}
        assert s["auto_resumed"] == {"auto_resume:timeout": 1}
        assert hang_stats(hub.store.conn, "2026-10-25T00:00:00.000Z")["hung"] == 0
    finally:
        hub.store.close()


# --------------------------------------------------------------------------- sửa theo review PR #42
def test_network_pattern_ignores_agent_summary():
    """`summary` có thể là chữ của agent → không được kích hoạt tự resume; chỉ `error` (hub/CLI ghi)."""
    assert transient_cause({"outcome": "failed", "summary": "curl: connection refused (ECONNRESET)"}) is None
    assert transient_cause({"outcome": "failed", "error": "stderr: socket hang up", "summary": "xong"}) == "network"


async def test_fire_skips_when_human_already_continued_session(make_hub):
    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 60})
    row = _row(status="crashed", outcome="crashed", started_at="2026-10-30T10:00:00.000Z", ended_at="2026-10-30T10:05:00.000Z")
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) == "scheduled"
    # người `run continue` session đó, run đã xong trước lúc tới hạn (không gắn continued_in_run_id vào run cũ)
    hub.store.insert_run(_row(status="completed", outcome="completed", session_id=row["session_id"], queued_at="2026-10-30T10:06:00.000Z",
                              started_at="2026-10-30T10:06:00.000Z", ended_at="2026-10-30T10:07:00.000Z"))
    assert hub.auto_resume.fire(row["id"]) is None
    last = ar_events(hub, row["id"])[-1]
    assert last["status"] == "skipped" and "đã được tiếp tục" in last["reason"]
    assert not any(r.get("parent_run_id") == row["id"] for r in chain(hub))


async def test_older_run_in_session_does_not_block_resume(make_hub):
    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 60})
    sid = str(uuid.uuid4())
    hub.store.insert_run(_row(status="completed", outcome="completed", session_id=sid, queued_at="2026-10-30T09:00:00.000Z"))
    row = _row(status="crashed", outcome="crashed", session_id=sid, queued_at="2026-10-30T10:00:00.000Z",
               started_at="2026-10-30T10:00:00.000Z", ended_at="2026-10-30T10:05:00.000Z")
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) == "scheduled"
    assert hub.auto_resume.fire(row["id"]) is not None


async def test_human_stop_cancels_scheduled_resume(make_hub):
    from cfharness.errors import HubError

    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 60})
    row = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso())
    hub.store.insert_run(row)
    assert hub.auto_resume.consider(row["id"]) == "scheduled"
    out = await hub.runs.stop(row["id"])  # trước đây: HubError "không đang chạy" → người không huỷ được
    assert out["id"] == row["id"]
    assert row["id"] not in (hub.store.state_get("auto_resume_pending") or {}) and row["id"] not in hub.auto_resume._handles
    last = ar_events(hub, row["id"])[-1]
    assert last["status"] == "skipped" and "người huỷ" in last["reason"]
    a = audit_rows(hub)[-1]
    assert a["run_id"] == row["id"] and a["actor"] == "human" and "người huỷ" in a["data"]
    with pytest.raises(HubError):  # không còn lịch hẹn → như cũ
        await hub.runs.stop(row["id"])
    await settle(timeout=0.2)
    assert len(chain(hub)) == 1


async def test_chain_shares_one_budget(make_hub):
    hub, _ = make_hub(**{"recovery.auto_resume_backoff_s": 60})
    root = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso(), max_budget_usd=1.0, cost_usd=0.4)
    hub.store.insert_run(root)
    child = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso(), session_id=root["session_id"],
                 parent_run_id=root["id"], resume_reason="auto_resume:process_killed", max_budget_usd=0.6, cost_usd=0.25)
    hub.store.insert_run(child)
    hub.store.update_run(root["id"], {"continued_in_run_id": child["id"]})
    assert hub.auto_resume.consider(child["id"]) == "scheduled"
    nr = hub.auto_resume.fire(child["id"])
    assert nr is not None and nr["max_budget_usd"] == pytest.approx(0.35)  # 1.0 − 0.4 − 0.25, không phải 0.6/1.0 đầy
    spent = _row(status="crashed", outcome="crashed", started_at=now_iso(), ended_at=now_iso(), max_budget_usd=0.5, cost_usd=0.5)
    hub.store.insert_run(spent)
    assert hub.auto_resume.consider(spent["id"]) == "scheduled"
    assert hub.auto_resume.fire(spent["id"]) is None
    assert "ngân sách" in ar_events(hub, spent["id"])[-1]["reason"]
