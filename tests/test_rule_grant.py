"""Luôn cho phép theo agent + luật (xin một lần): `approval.approve{grant_rule}`, `guard.grant.set|rm|list`.

- người duyệt một lần kèm `grant_rule` → từ nay agent đó không bị hỏi lại với luật đó (kể cả destructive, lệnh khác cùng luật);
- agent khác / luật khác vẫn hỏi; forbidden, ESTOP vẫn chặn; run review/eval không hưởng;
- approval đang chờ cùng agent + luật được cho phép luôn;
- hết hạn, gỡ, chỉ người (HUMAN_ONLY + require_human), audit + event;
- web: nút → trang bước 2 → cho phép + grant.

Không gọi claude: Hub in-process + FakeRuntime.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from cfharness import auth
from cfharness.errors import Blocked, HubError
from cfharness.guard.rpc import register_rpc
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime



@pytest.fixture
def files(tmp_path, monkeypatch):
    """File có sẵn TRƯỚC run → xoá = destructive (delete_not_run_created). Mốc run có dung sai 1 s nên giả thời điểm tạo cũ."""
    from cfharness.guard import classifier

    real = classifier._birth
    root = os.path.normcase(str(tmp_path))  # Windows: classifier đưa đường dẫn dạng `C:/…` → so sau khi chuẩn hoá
    monkeypatch.setattr(classifier, "_birth", lambda p, st: 0.0 if os.path.normcase(os.path.abspath(str(p))).startswith(root) else real(p, st))
    for n in ("a", "b", "c"):
        (tmp_path / n).write_text("x")
    return {n: {"command": f"rm {tmp_path / n}"} for n in ("a", "b", "c")}


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    h.config.set("approval.profile", "strict")
    yield h
    rt.release()
    h.store.close()


def rpc(hub):
    d = SimpleNamespace(hub=hub, methods={})
    register_rpc(d)
    return d.methods


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


async def check(hub, run, ti, tool="Bash"):
    return await asyncio.wait_for(hub.guard.check({"run_id": run["id"], "tool_name": tool, "tool_input": ti, "channel": "hook"}), 20)


def verdict(hub, run, ti):
    from cfharness.guard.classifier import classify

    return classify("Bash", ti, hub.guard.context_for(run))


def open_ap(hub, run, ti):
    v = verdict(hub, run, ti)
    return hub.approvals.open(run, tool_name="Bash", tool_input=ti, tool_use_id=None, fingerprint=f"fp-{ti['command']}", verdict=v.to_dict(),
                              channel="hook", routed_to="human", route_note=None)


def as_agent():
    return auth.CURRENT_CALLER.set(auth.Caller(via="socket", pid=1, token_ok=True, run_id="r_x", evidence=["descendant_of_run_pid:1"]))


async def test_grant_from_approval_stops_asking_same_agent_rule(hub, files):
    DEL, DEL2 = files["a"], files["b"]
    run = await _run(hub, "w")
    v = verdict(hub, run, DEL)
    assert v.verdict == "destructive" and v.rule and verdict(hub, run, DEL2).rule == v.rule
    ap = open_ap(hub, run, DEL)
    other = open_ap(hub, run, DEL2)  # đang chờ, cùng agent + luật → được cho phép luôn
    res = rpc(hub)["approval.approve"]({"id": ap["id"], "grant_rule": True, "by": "user", "channel": "web"})
    assert res["status"] == "allowed"
    g = res["rule_grant"]
    assert g["agent_id"] == "w" and g["rule"] == v.rule and g["until"] is None and g["released"] == [other["id"]]
    assert hub.approvals.get(other["id"])["status"] == "allowed"
    r = await check(hub, run, files["c"])
    assert r["decision"] == "allow" and r["decided_by"] == "rule_grant"
    assert not hub.approvals.list("pending")
    row = hub.store.conn.execute("SELECT classifier, verdict FROM guard_decisions WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    assert row["classifier"] == "rule_grant" and row["verdict"] == "destructive"  # vẫn ghi đúng mức
    assert [e for e in hub.store.query_events() if e.type == "guard.rule_grant"]
    assert hub.guard.rule_grants() == {"w": {v.rule: {**{k: g[k] for k in ("until", "by", "channel", "at", "approval_id")}}}}


async def test_other_agent_forbidden_estop_and_review_still_held(hub, files):
    DEL = files["a"]
    run = await _run(hub, "w")
    rule = verdict(hub, run, DEL).rule
    hub.guard.rule_grant_set("w", rule)
    r2 = await _run(hub, "w2")
    r = await check(hub, r2, DEL)
    assert r["decision"] == "deny" and r.get("waiting_human")  # agent khác vẫn hỏi
    r = await check(hub, run, {"command": "cfharness config set approval.profile autonomous"})
    assert r["decision"] == "deny"  # forbidden không bao giờ qua grant
    hub.store.state_set("estop", {"on": True})
    try:
        assert (await check(hub, run, DEL))["decision"] == "deny"
    finally:
        hub.store.state_set("estop", {"on": False})
    v = verdict(hub, run, DEL)
    assert hub.guard.run_rule_grant({**run, "kind": "review"}, v) is None
    assert hub.guard.run_rule_grant({**run, "run_opts": {"eval_overlay": {"x": 1}}}, v) is None
    assert hub.guard.run_rule_grant(run, v) is not None


async def test_expiry_rm_and_human_only(hub):
    hub.agents.create({"id": "w", "name": "W"})
    g = hub.guard.rule_grant_set("w", "delete_not_run_created", for_s=3600)
    assert g["until"] and hub.guard.rule_grants()["w"]
    allv = hub.store.state_get("agent_rule_grants")
    allv["w"]["delete_not_run_created"]["until"] = "2020-01-01T00:00:00Z"
    hub.store.state_set("agent_rule_grants", allv)
    assert hub.guard.rule_grants() == {}
    hub.guard.rule_grant_set("w", "delete_not_run_created")
    hub.guard.rule_grant_set("w", "code_delete_api")
    assert hub.guard.rule_grant_rm("w", "code_delete_api")["removed"] == ["code_delete_api"]
    assert list(hub.guard.rule_grants()["w"]) == ["delete_not_run_created"]
    assert hub.guard.rule_grant_rm("w")["removed"] == ["delete_not_run_created"]
    with pytest.raises(HubError):
        hub.guard.rule_grant_rm("w", "x")
    for bad in (("w", "", None), ("w", "r", -1), ("w", "r", "abc")):
        with pytest.raises(HubError):
            hub.guard.rule_grant_set(bad[0], bad[1], for_s=bad[2])
    assert {"guard.grant.set", "guard.grant.rm", "approval.approve"} <= auth.HUMAN_ONLY
    tok = as_agent()
    try:
        with pytest.raises(Blocked):
            hub.guard.rule_grant_set("w", "delete_not_run_created")
        with pytest.raises(Blocked):
            hub.guard.rule_grant_rm("w")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    audits = [dict(x) for x in hub.store.conn.execute("SELECT * FROM audit_log")]
    assert any("guard.rule_grant" in str(x.get("kind") or x.get("action") or "") for x in audits)


async def test_approve_grant_by_agent_blocked_and_keeps_pending(hub, files):
    DEL = files["a"]
    run = await _run(hub, "w")
    ap = open_ap(hub, run, DEL)
    tok = as_agent()
    try:
        with pytest.raises(Blocked):
            rpc(hub)["approval.approve"]({"id": ap["id"], "grant_rule": True})
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert hub.approvals.get(ap["id"])["status"] in ("open", "waiting_human")
    assert hub.guard.rule_grants() == {}


def test_agent_cli_grant_is_forbidden(hub):
    from cfharness.guard.classifier import classify

    v = classify("Bash", {"command": "cfharness guard grant set w delete_not_run_created"}, hub.guard.context_for(None))
    assert v.forbidden
