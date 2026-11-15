"""Hồi quy QA vòng 1 (dispatcher claim, auto_resume chuỗi, chi phí session, cron không bao giờ chạy, khôi phục, resume sau duyệt,
submit lỗi giữa chừng). Không gọi `claude` thật: FakeRuntime + store giả."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from cfharness import auto_resume as ar
from cfharness.errors import Blocked, HubError
from cfharness.hub import Hub
from cfharness.runs import RunManager
from cfharness.runtime import FakeRuntime
from cfharness.schedules import cron
from cfharness.schedules.rpc import preview
from cfharness.store import now_iso


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- 6. dispatcher: claim '__claiming__'
async def test_dispatcher_rolls_back_claim_on_unexpected_error(hub_home, monkeypatch):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    h.agents.create({"id": "w", "name": "W"})

    def boom(*a, **k):
        raise OSError("disk I/O error giả lập")

    orig = h.runs.submit
    monkeypatch.setattr(h.runs, "submit", boom)
    t = h.tasks.create({"assignee": "w", "title": "x", "body": "y"})
    with pytest.raises(OSError):
        h.dispatcher.tick()
    row = h.tasks.row(t["id"])
    assert row["current_run_id"] is None and row["status"] == "ready"  # trả claim ngay, không kẹt
    monkeypatch.setattr(h.runs, "submit", orig)
    h.dispatcher.tick()
    assert h.tasks.row(t["id"])["current_run_id"] not in (None, "__claiming__")  # tick sau claim + submit bình thường
    h.store.close()


async def test_dispatcher_reconciles_stale_claim(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    h.agents.create({"id": "w", "name": "W"})
    t = h.tasks.create({"assignee": "w", "title": "x", "body": "y"})
    # daemon chết giữa claim và submit: task ở running + '__claiming__', không có run
    h.store.conn.execute("UPDATE tasks SET status='running', current_run_id='__claiming__', claimed_at=? WHERE id=?", (now_iso(), t["id"]))
    h.dispatcher._reconcile_claims()
    row = h.tasks.row(t["id"])
    assert row["current_run_id"] is None and row["status"] == "ready"
    h.store.close()


async def test_dispatcher_stale_claim_attaches_live_run(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime(hold=True))
    h.start()
    h.agents.create({"id": "w", "name": "W"})
    t = h.tasks.create({"assignee": "w", "title": "x", "body": "y"})
    h.dispatcher.tick()
    await settle()
    rid = h.tasks.row(t["id"])["current_run_id"]
    assert rid and rid != "__claiming__"
    # submit xong nhưng chưa kịp ghi run vào task → không được trả task về ready (sẽ tạo run trùng)
    h.store.conn.execute("UPDATE tasks SET current_run_id='__claiming__' WHERE id=?", (t["id"],))
    h.dispatcher._reconcile_claims()
    assert h.tasks.row(t["id"])["current_run_id"] == rid
    await h.stop()


# ---------------------------------------------------------------- 7. auto_resume: chuỗi
class _S:
    def __init__(self, d):
        self.d = d

    def get_run(self, i):
        return self.d.get(i)


def test_chain_budget_left_when_root_missing_is_limited():
    r1 = {"id": "r1", "parent_run_id": "r0", "resume_reason": "auto_resume:crash", "cost_usd": 0.5, "max_budget_usd": 1.0}
    left = ar.chain_budget_left(_S({}), r1)
    assert left is not None and left == pytest.approx(0.5)
    root = {"id": "r0", "max_budget_usd": 1.0, "cost_usd": 0.9}
    assert ar.chain_budget_left(_S({"r0": root}), r1) == pytest.approx(-0.4)


def test_chain_attempts_not_capped_at_50():
    d = {}
    for i in range(80):
        d[f"r{i}"] = {"id": f"r{i}", "parent_run_id": f"r{i-1}" if i else None, "resume_reason": "auto_resume:crash" if i else None}
    assert ar.chain_attempts(_S(d), d["r79"]) == 79


def test_chain_runs_stops_on_cycle():
    d = {"a": {"id": "a", "parent_run_id": "b", "resume_reason": "auto_resume:crash"},
         "b": {"id": "b", "parent_run_id": "a", "resume_reason": "auto_resume:crash"}}
    assert ar.chain_attempts(_S(d), d["a"]) == 1


# ---------------------------------------------------------------- 8. chi phí luỹ kế session
class _CostStore:
    def __init__(self):
        self.sess = {"s1": {"cumulative_cost_usd": 0.0}}

    def get_session(self, i):
        return self.sess.get(i)

    def set_session_cost(self, i, c):
        self.sess[i]["cumulative_cost_usd"] = c


def _rec(st, total, resume=True):
    m = NS(store=st)
    h = NS(spec=NS(resume_session_id="s1" if resume else None, session_id="s1", fork=False, run_id="r"))
    return RunManager._record_cost(m, h, {"cost_usd": total})


def test_record_cost_missing_total_does_not_reset_or_double_count():
    st = _CostStore()
    a = _rec(st, 5.0, resume=False)["cost_usd"]
    b = _rec(st, None)["cost_usd"]
    assert st.sess["s1"]["cumulative_cost_usd"] == 5.0
    c = _rec(st, 5.5)["cost_usd"]
    assert (a, b) == (5.0, 0.0) and round(a + b + c, 6) == 5.5


def test_record_cost_smaller_total_keeps_cumulative():
    st = _CostStore()
    _rec(st, 5.0, resume=False)
    out = _rec(st, 0.2)
    assert out["cost_usd"] == 0.0 and st.sess["s1"]["cumulative_cost_usd"] == 5.0
    assert _rec(st, 5.3)["cost_usd"] == pytest.approx(0.3)


# ---------------------------------------------------------------- 9. cron không bao giờ chạy
def test_cron_never_firing_rejected_in_preview_but_old_rows_still_load():
    svc = NS(now=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc))
    with pytest.raises(HubError, match="không bao giờ chạy"):
        preview(svc, "0 0 30 2 *")
    assert preview(svc, "0 0 29 2 *")["next"]  # 29/2 chạy vào năm nhuận
    assert preview(svc, "0 0 30 2 mon")["next"]  # ngày HOẶC thứ: thứ hai vẫn chạy
    assert cron.parse("0 0 30 2 *", strict=False).expr == "0 0 30 2 *"  # bản ghi cũ trong DB không làm hỏng list/tick


# ---------------------------------------------------------------- S4. khôi phục: pending_resume
def _insert_running(h, agent, **kw):
    rid = "r_" + uuid.uuid4().hex[:12]
    h.store.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "do it", "session_id": str(uuid.uuid4()), "status": "running",
                        "queued_at": now_iso(), "started_at": now_iso(), **kw})
    return rid


def test_recovery_without_loop_keeps_pending_resume(hub_home):
    from cfharness.recovery import reconcile

    h = Hub(hub_home, runtime=FakeRuntime())
    h.agents.create({"id": "a", "name": "A"})
    rid = _insert_running(h, "a")
    reconcile(h)  # không có event loop
    assert h.store.get_run(rid)["status"] == "interrupted"
    assert rid in (h.store.state_get("pending_resume", []) or [])  # không bị xoá mất
    h.store.close()


async def test_recovery_resumes_once_when_run_also_pending(hub_home):
    from cfharness.recovery import reconcile

    h = Hub(hub_home, runtime=FakeRuntime(hold=True))
    h.agents.create({"id": "a", "name": "A"})
    rid = _insert_running(h, "a")
    h.store.state_set("pending_resume", [rid])
    out = reconcile(h)
    assert len(out["resumed"]) == 1
    kids = h.store.conn.execute("SELECT COUNT(*) FROM runs WHERE parent_run_id=?", (rid,)).fetchone()[0]
    assert kids == 1
    await h.stop()


# ---------------------------------------------------------------- S5. resume sau duyệt bị chặn
async def test_approval_resolved_resume_blocked_does_not_leave_waiting_human(hub_home, monkeypatch):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    h.agents.create({"id": "a", "name": "A"})
    rid = _insert_running(h, "a")
    h.approvals.open(h.store.get_run(rid), tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_use_id="t", fingerprint="fp",
                     verdict={"verdict": "destructive"}, channel="hook")
    h.store.update_run(rid, {"status": "waiting_human"})
    ap = h.approvals.all_for_run(rid)[0]
    h.approvals._update(ap["id"], {"status": "allowed"})

    def blocked(*a, **k):
        raise Blocked("ESTOP đang bật")

    monkeypatch.setattr(h.runs, "submit", blocked)
    assert h.runs.on_approval_resolved(rid) is None
    r = h.store.get_run(rid)
    assert r["status"] == "failed" and "ESTOP" in (r["error"] or "")
    h.store.close()


# ---------------------------------------------------------------- S6. submit lỗi sau insert_run
async def test_submit_failure_after_insert_leaves_no_ghost_queued_run(hub_home, monkeypatch):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    h.agents.create({"id": "a", "name": "A"})
    real = h.publish

    def pub(ev):
        if ev.type == "run.queued":
            raise RuntimeError("bus lỗi giả lập")
        return real(ev)

    monkeypatch.setattr(h, "publish", pub)
    with pytest.raises(RuntimeError):
        h.runs.submit("a", "x", budget_usd=1.0)
    assert not h.store.list_runs(status="queued", limit=100)
    assert h.runs.reserved_budget("agent", "a") == 0
    h.store.close()
