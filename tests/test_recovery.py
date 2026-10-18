"""FR-RT-19 / Q-09 — khôi phục khi daemon khởi động lại; FR-SG-06 purge (xoá cứng là thao tác của người)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid

import pytest

from agent_hub.errors import HubError
from agent_hub.hub import Hub
from agent_hub.recovery import process_matches, purge_expired
from agent_hub.runtime import FakeRuntime
from agent_hub.store import now_iso


async def settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0.01)


@pytest.fixture
def sleeper():
    p = subprocess.Popen(["sleep", "60"], start_new_session=True)
    yield p
    try:
        p.kill()
    except OSError:
        pass


def _insert_running(hub: Hub, agent: str, pid: int | None, start: float | None, **kw) -> str:
    rid = "r_" + uuid.uuid4().hex[:12]
    hub.store.insert_run(
        {
            "id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "do it", "session_id": str(uuid.uuid4()), "status": "running",
            "queued_at": now_iso(), "started_at": now_iso(), "pid": pid, "pid_start_time": start, **kw,
        }
    )
    return rid


def test_process_matches_pid_and_start_time(sleeper):
    now = time.time()
    assert process_matches(sleeper.pid, now, 5)
    assert not process_matches(sleeper.pid, now - 3600, 5)  # pid tái sử dụng → không phải tiến trình của run
    assert not process_matches(None, now)


async def test_reconcile_kills_orphan_marks_interrupted_and_resumes(hub_home, sleeper):
    h1 = Hub(hub_home, runtime=FakeRuntime())
    h1.agents.create({"id": "a", "name": "A"})
    h1.agents.create({"id": "b", "name": "B"})
    orphan = _insert_running(h1, "a", sleeper.pid, time.time())
    reused = _insert_running(h1, "b", 999_999, time.time() - 3600)  # pid không còn / không khớp
    waiting = _insert_running(h1, "b", None, None)
    h1.approvals.open(h1.store.get_run(waiting), tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_use_id="t", fingerprint="fp", verdict={"verdict": "destructive"}, channel="hook")
    queued_id = "r_q" + uuid.uuid4().hex[:8]
    h1.store.insert_run({"id": queued_id, "agent_id": "a", "kind": "adhoc", "prompt": "later", "session_id": str(uuid.uuid4()), "status": "queued", "queued_at": now_iso()})
    h1.store.close()

    rt = FakeRuntime()
    t0 = time.monotonic()
    h2 = Hub(hub_home, runtime=rt)
    h2.start()
    assert time.monotonic() - t0 < 30  # NFR-06
    for _ in range(100):
        sleeper.poll()
        if sleeper.returncode is not None:
            break
        await asyncio.sleep(0.05)
    assert sleeper.returncode is not None  # tiến trình mồ côi đã bị dừng
    o = h2.store.get_run(orphan)
    assert o["status"] == "interrupted" and o["continued_in_run_id"]
    assert h2.store.get_run(reused)["status"] == "interrupted"
    w = h2.store.get_run(waiting)
    assert w["status"] == "waiting_human" and not w.get("continued_in_run_id")  # không resume khi chờ người
    assert h2.approvals.list("pending", run_id=waiting)[0]["status"] == "waiting_human"
    new = h2.store.get_run(o["continued_in_run_id"])
    assert new["resumed_from_session"] == o["session_id"] and new["session_id"] == o["session_id"] and new["resume_reason"] == "daemon_restart"
    await h2.runs.wait(new["id"], 10)
    await h2.runs.wait(queued_id, 10)
    assert h2.store.get_run(new["id"])["outcome"] == "completed"
    assert h2.store.get_run(queued_id)["outcome"] == "completed"  # run trong hàng đợi được xếp lại
    specs = {s.run_id: s for s in rt.started}
    assert specs[new["id"]].resume_session_id == o["session_id"]
    ev = [e.type for e in h2.store.query_events(types=["recovery.reconciled", "run.resumed"])]
    assert "recovery.reconciled" in ev and "run.resumed" in ev
    h2.store.close()


async def test_graceful_shutdown_then_auto_resume(hub_home):
    h1 = Hub(hub_home, runtime=FakeRuntime(hold=True))
    h1.start()
    h1.agents.create({"id": "a", "name": "A"})
    r = h1.runs.submit("a", "long task")
    await settle()
    assert h1.store.get_run(r["id"])["status"] == "running"
    await h1.stop()
    st = Hub(hub_home, runtime=FakeRuntime())
    st.start()
    old = st.store.get_run(r["id"])
    assert old["status"] == "interrupted" and old["continued_in_run_id"]
    final = await st.runs.wait(old["continued_in_run_id"], 10)
    assert final["outcome"] == "completed" and final["session_id"] == r["session_id"]
    st.store.close()


async def test_no_auto_resume_when_disabled_or_estop(hub_home, sleeper):
    h1 = Hub(hub_home, runtime=FakeRuntime())
    h1.agents.create({"id": "a", "name": "A"})
    rid = _insert_running(h1, "a", None, None)
    h1.config.set("recovery.auto_resume", False)
    h1.store.close()
    h2 = Hub(hub_home, runtime=FakeRuntime())
    h2.start()
    assert h2.store.get_run(rid)["status"] == "interrupted" and not h2.store.get_run(rid)["continued_in_run_id"]
    h2.store.close()


def test_purge_requires_soft_delete_and_retention(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.agents.create({"id": "a", "name": "A"})
    from agent_hub.recovery import purge_agent

    with pytest.raises(HubError):
        purge_agent(h, "a", by="user", channel="cli")  # chưa xoá mềm
    row = h.agents.remove("a")
    trash = row["trash_path"]
    assert os.path.isdir(trash)
    assert purge_expired(h) == []  # chưa hết hạn
    from datetime import datetime, timedelta, timezone

    assert purge_expired(h, now=datetime.now(timezone.utc) + timedelta(days=31)) == ["a"]
    assert not os.path.exists(trash) and h.store.get_agent("a")["status"] == "purged"
    assert [e.type for e in h.store.query_events(types=["agent.purged"])] == ["agent.purged"]
    h.store.close()
