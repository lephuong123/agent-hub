"""Công tắc chung `gates.mode` (human | manager | off) — mọi cổng chờ người ở một chỗ.

- off: guard cho phép mọi thao tác lẽ ra phải hỏi (decided_by gates_off); forbidden + ESTOP vẫn chặn;
- manager: approval mọi lớp (kể cả push/deploy — danh sách "không bao giờ") giao điều phối viên và nó quyết được; approval của
  chính run điều phối viên vẫn chỉ người; câu hỏi ngoài task cũng qua điều phối viên; xin phép được áp dụng ngay; task bị chặn
  đánh thức điều phối viên ngay; prompt nhịp nói rõ quyền được giao;
- chỉ người đặt (config.set HUMAN_ONLY; classifier chặn `cfharness gates`), giá trị lạ bị từ chối.

Không gọi claude: Hub/Daemon in-process + FakeRuntime/ScriptedRuntime.
"""

from __future__ import annotations

import asyncio

import pytest

from cfharness import auth
from cfharness.errors import HubError
from cfharness.guard import profiles
from cfharness.guard.classifier import Verdict, classify
from cfharness.hub import Hub
from cfharness.manager import prompts
from cfharness.runtime import FakeRuntime
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    yield h
    rt.release()
    h.store.close()


async def _run(hub, agent="w"):
    if hub.store.get_agent(agent) is None:
        hub.agents.create({"id": agent, "name": agent.upper()})
    r = hub.runs.submit(agent, "x")
    for _ in range(500):
        cur = hub.store.get_run(r["id"])
        if cur and cur["status"] == "running":
            return cur
        await asyncio.sleep(0.01)
    raise AssertionError("run không chạy")


async def check(hub, run, ti, tool_name="Bash"):
    return await asyncio.wait_for(hub.guard.check({"run_id": run["id"], "tool_name": tool_name, "tool_input": ti, "channel": "hook"}), 20)


def test_mode_default_validation_and_human_only_cli(hub):
    assert profiles.gates_mode(hub.config) == "human"
    for bad in ("all", "", None, True):
        with pytest.raises(ValueError):
            hub.config.set("gates.mode", bad)
    hub.config.set("gates.mode", "manager")
    assert profiles.gates_mode(hub.config) == "manager"
    assert "config.set" in auth.HUMAN_ONLY
    for cmd in ("cfharness gates off", "cfharness gates"):
        assert classify("Bash", {"command": cmd}, hub.guard.context_for(None)).forbidden


async def test_off_allows_destructive_but_not_forbidden_or_estop(hub):
    run = await _run(hub)
    push = {"command": "git push --force origin main"}
    r = await check(hub, run, push)
    assert r["decision"] == "deny"  # human: giữ chờ
    hub.approvals.cancel_for_run(run["id"], "test")
    run = await _run(hub, "w2")
    hub.config.set("gates.mode", "off")
    r = await check(hub, run, push)
    assert r["decision"] == "allow" and r["decided_by"] == "gates_off"
    row = hub.store.conn.execute("SELECT classifier FROM guard_decisions WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    assert row["classifier"] == "gates_off"
    assert (await check(hub, run, {"command": "cfharness config set approval.profile strict"}))["decision"] == "deny"
    hub.store.state_set("estop", {"on": True})
    try:
        assert (await check(hub, run, push))["decision"] == "deny"
    finally:
        hub.store.state_set("estop", {"on": False})


def test_manager_mode_makes_never_list_delegable(hub):
    for v in (Verdict("destructive", "git_push_force_or_delete", "git", "x", ["/x"]), Verdict("destructive", "schedule_persistence", "exec", "x", []),
              Verdict("uncertain", "hub_secret_read", "hub", "x", []), Verdict("destructive", "sql_drop", "db", "x", [])):
        assert profiles.delegable(v, hub.config) is not None
        hub.config.set("gates.mode", "manager")
        assert profiles.delegable(v, hub.config) is None
        hub.config.set("gates.mode", "human")


# ============================================================================ với điều phối viên thật (daemon in-process)
class Script(MgrScript):
    def __call__(self, spec):
        if spec.prompt.startswith("HOLD"):
            self.specs.append(spec)
            return {"text": "x", "cost": 0.001, "hold": True}
        return super().__call__(spec)


@pytest.fixture
async def mgr_env(hub_home, tmp_path):
    d = await make_daemon(hub_home, Script())
    d.hub.config.set("approval.timeout_s", 20)
    d.hub.config.set("guard.git_probe", False)
    d.hub.config.set("approval.profile", "strict")  # strict: không giao gì — gates.mode phải tự mở
    (tmp_path / "work").mkdir()
    yield d, tmp_path
    await close(d)


async def _worker_run(d, agent="w1", cwd=None):
    if d.hub.store.get_agent(agent) is None:
        d.hub.agents.create({"id": agent, "name": agent, "role": "dev", "model": "haiku", "tool_policy": {"tools": ["Bash", "Read"]}})
    r = d.hub.runs.submit(agent, "HOLD work", cwd=str(cwd) if cwd else None)
    return await wait_status(d, r["id"])


async def test_manager_decides_push_but_not_its_own(mgr_env):
    d, tmp = mgr_env
    d.hub.config.set("gates.mode", "manager")
    m = await manager_run(d)
    run = await _worker_run(d, cwd=tmp / "work")
    task = asyncio.create_task(d.hub.guard.check({"run_id": run["id"], "tool_name": "Bash", "tool_input": {"command": "git push --force origin main"},
                                                  "channel": "hook"}))
    for _ in range(300):
        aps = d.hub.approvals.list("pending", run_id=run["id"])
        if aps:
            break
        await asyncio.sleep(0.01)
    ap = aps[0]
    assert ap["routed_to"] == "manager"
    res = await tool(d, m["run_id"], "approval_decide", {"approval_id": ap["id"], "decision": "allow", "reason": "owner goal: ship the fix"})
    assert res["status"] == "allowed"
    r = await asyncio.wait_for(task, 10)
    # push còn nằm trong mẫu --disallowedTools tĩnh → hub cho phép bằng cách khởi động lại run kèm vé một lần (cơ chế có sẵn)
    assert r["decision"] == "allow" or (r["approval_id"] == ap["id"] and "restart" in r["reason"].lower())
    assert d.hub.approvals.get(ap["id"])["resolved_by"] == "manager:boss"
    mrun = d.hub.store.get_run(m["run_id"])
    v = Verdict("destructive", "git_push_force_or_delete", "git", "x", [str(tmp / "work")])
    assert d.hub.guard.route_for(mrun, "Bash", {}, v, d.hub.guard.context_for(mrun))[0] == "human"


async def test_question_outside_task_goes_to_manager(mgr_env):
    d, tmp = mgr_env
    await manager_run(d)
    run = await _worker_run(d)
    q = d.hub.questions.open(run, "Which port?", ["8080", "9090"])
    assert q["status"] == "open" and q["routed_to"] == "human"
    d.hub.config.set("gates.mode", "manager")
    q = d.hub.questions.open(run, "Which host?", ["a", "b"])
    assert q["status"] == "with_manager" and q["routed_to"] == "manager"


async def test_owner_permission_applied_directly(mgr_env):
    d, tmp = mgr_env
    d.hub.config.set("gates.mode", "manager")
    m = await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "ops", "model": "haiku"})
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1", "reason": "dev server"})
    assert out["applied"] is True and "question_id" not in out
    assert d.hub.guard.sandbox_overrides("w1")["allow_localhost"] is True
    assert not d.hub.questions.list("open")
    # đích là agent điều phối viên vẫn bị `build` từ chối
    with pytest.raises(HubError):
        await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "boss", "reason": "x"})


async def test_blocked_task_wakes_manager_and_prompt_says_delegated(mgr_env):
    d, tmp = mgr_env
    await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "dev", "model": "haiku"})
    mgr = d.hub.manager
    t = d.hub.tasks.create({"title": "deploy", "assignee_agent_id": "w1", "prompt": "HOLD x"})
    d.hub.config.set("gates.mode", "manager")
    mgr.pending.clear()
    mgr._pending_keys.clear()
    mgr._first_pending = None
    d.hub.tasks.set_status(t["id"], "blocked", reason="GATE: need owner")
    await asyncio.sleep(0.05)
    assert any(e["kind"] == "task_blocked" for e in mgr.pending)
    import time

    assert time.monotonic() - mgr._first_pending > float(mgr.cfg("event_debounce_s", 45))  # bỏ thời gian gom
    assert "DELEGATED TO YOU" in prompts.tick_prompt([], dropped=0, reason="x", open_work=0, gates="manager")
    assert "DELEGATED" not in prompts.tick_prompt([], dropped=0, reason="x", open_work=0)
