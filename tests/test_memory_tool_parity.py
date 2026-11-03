"""Tool `memory` ngang Hermes (plan-memory-hermes-parity.md, PR 2): không giới hạn số lần ghi (suất riêng, mặc định 0 = không
giới hạn, không ăn suất review); ghi tầng team/global cho lead (team mình) / điều phối viên / policy `shared_memory_write` —
áp dụng ngay (watch), hồi quy thì tự rollback THEO MỤC. Không gọi claude."""

from datetime import datetime, timedelta, timezone

import pytest

from cfharness.errors import Blocked, HubError
from cfharness.mcp_server.hub_self import memory_tool_def, server_entry
from m5a_fake import add_run, make_hub, prompt_of

FACT = "The team release train leaves every Thursday at 15:00 Hanoi time."
FACT2 = "The team staging cluster is called kestrel-stg."


@pytest.fixture
def hub(hub_home):
    h = make_hub(hub_home)
    for a in ("a1", "a2", "a3"):
        h.store.insert_run({"id": f"run_{a}", "agent_id": a, "kind": "chat", "status": "running", "queued_at": "2026-10-30T00:00:00.000Z"})
    yield h
    h.store.close()


def mem(h, agent="a1", **args):
    return h.learning.learnself_call(f"run_{agent}", "memory", args)


def team_file(h):
    return (h.paths.home / "teams" / "t1" / "memory.md").read_text(encoding="utf-8")


# ============================================================================ không giới hạn số lần
def test_tool_writes_are_not_rate_limited_and_do_not_use_review_quota(hub):
    pol = hub.learning.policy
    assert pol.get("rate")["agent_tool_max_per_day"] == 0
    for i in range(8):  # > max_changes_per_agent_per_day (5)
        r = mem(hub, target="memory", action="add", content=f"Fact {i}: the build cache lives in bucket b{i}.")
        assert r["status"] == "applied", r
    assert hub.learning.pipeline.agent_quota_full("a1") is None  # review của a1 vẫn còn suất
    pol.set("rate.agent_tool_max_per_day", 9)
    assert mem(hub, target="memory", action="add", content="Fact 8: the cdn is fastly.")["status"] == "applied"
    r = mem(hub, target="memory", action="add", content="Fact 9: the queue is sqs.")
    assert r["status"] == "deferred_rate_limited" and "agent_tool_max_per_day" in r["reason"]


# ============================================================================ quyền ghi tầng chung
def test_scopes_by_role_and_policy(hub):
    lr = hub.learning
    assert lr.memory_scopes("a1") == ["agent"]
    with pytest.raises(Blocked):
        mem(hub, target="memory", action="add", content=FACT, scope="team")
    hub.teams.member_set("t1", "a1", "lead")
    assert lr.memory_scopes("a1") == ["agent", "team"]
    with pytest.raises(Blocked):
        mem(hub, target="memory", action="add", content=FACT, scope="global")
    lr.policy.set("agents.a2.shared_memory_write", True)
    assert lr.memory_scopes("a2") == ["agent", "team", "global"]
    with pytest.raises(HubError):
        lr.policy.set("agents.a2.shared_memory_write", "yes")


def test_lead_only_writes_team_it_leads(hub):
    hub.teams.create({"id": "t2", "name": "Other"})
    hub.teams.member_set("t2", "a1", "worker")
    hub.teams.member_set("t1", "a1", "lead")
    with pytest.raises(Blocked):
        mem(hub, target="memory", action="add", content=FACT, scope="team", team_id="t2")
    r = mem(hub, target="memory", action="add", content=FACT, scope="team")  # lead đúng một team → tự chọn
    assert r["status"] == "applied" and r["team_id"] == "t1"


# ============================================================================ tầng team: áp dụng ngay, watch
def test_lead_team_write_applies_now_to_every_member(hub):
    hub.teams.member_set("t1", "a1", "lead")
    r = mem(hub, target="memory", action="add", content=FACT, scope="team")
    assert r["status"] == "applied" and r["scope"] == "team" and r["watch"] and FACT in r["entries"] and "rolled back" in r["note"]
    assert FACT in team_file(hub)
    for a in ("a1", "a2", "a3"):  # không ghim — mọi thành viên thấy ngay từ session mới
        assert FACT in prompt_of(hub, a)
    assert hub.evals.gate.calls == []  # không eval trước
    c = hub.learning.canary.get(r["watch"])
    assert c["mode"] == "watch" and c["created_by"] == "agent" and sorted(c["watch_agents"]) == ["a1", "a2", "a3"]
    # ghi tiếp cùng đích trong lúc watch → không bị hoãn (nhiều watch song song)
    r2 = mem(hub, target="memory", action="add", content=FACT2, scope="team")
    assert r2["status"] == "applied" and r2["watch"] != r["watch"]


def test_review_canary_on_same_target_defers_tool_write(hub):
    from m5a_fake import team_memory

    assert team_memory(hub, FACT)["status"] == "canary"  # review → canary (ghim)
    hub.teams.member_set("t1", "a2", "lead")
    r = mem(hub, "a2", target="memory", action="add", content=FACT2, scope="team")
    assert r["status"] == "deferred_rate_limited" and "canary" in r["reason"]


def test_regression_rolls_back_only_that_entry(hub):
    now = datetime.now(timezone.utc)
    for a in ("a1", "a2", "a3"):
        for i in range(5):
            add_run(hub, a, f"b{a}{i}", now - timedelta(days=1, minutes=i))
    hub.teams.member_set("t1", "a1", "lead")
    bad = mem(hub, target="memory", action="add", content=FACT, scope="team")
    good = mem(hub, target="memory", action="add", content=FACT2, scope="team")
    assert bad["status"] == good["status"] == "applied"
    add_run(hub, "a2", "w1", correction=True)
    add_run(hub, "a2", "w2", correction=True)
    st = {hub.learning.canary.get(x)["status"] for x in (bad["watch"], good["watch"])}
    assert st == {"rolled_back"}  # cả hai watch thấy hồi quy trên a2 — mỗi cái gỡ ĐÚNG mục của nó
    live = team_file(hub)
    assert FACT not in live and FACT2 not in live
    rb = [e for e in hub.events if e.type == "learning.rolled_back"]
    assert len(rb) == 2 and {e.payload["actor"] for e in rb} == {"canary"}


def test_watch_rollback_keeps_entries_written_later(hub):
    hub.teams.member_set("t1", "a1", "lead")
    first = mem(hub, target="memory", action="add", content=FACT, scope="team")
    later = hub.learning.pipeline.propose(target="memory", op="add", agent_id="a1", scope="team", created_by="user",
                                          payload={"store": "memory", "action": "add", "content": FACT2})
    assert later["status"] in ("applied", "canary")
    c = hub.learning.canary.get(first["watch"])
    hub.learning.ledger.rollback(c["ledger_entry_id"], actor="canary", reason="test")
    live = team_file(hub)
    assert FACT not in live and FACT2 in live  # theo mục: không kéo mục người ghi sau


# ============================================================================ global + USER dùng chung
def test_policy_agent_writes_global_and_shared_user_profile(hub):
    hub.learning.policy.set("agents.a3.shared_memory_write", True)
    r = mem(hub, "a3", target="memory", action="add", content="Every repo uses conventional commits.", scope="global")
    assert r["status"] == "applied" and r["scope"] == "global"
    for a in ("a1", "a2"):
        assert "conventional commits" in prompt_of(hub, a)
    u = mem(hub, "a3", target="user", action="add", content="The owner reads replies on a phone; keep them short.", scope="global")
    assert u["status"] == "applied" and u["target"] == "user"
    with pytest.raises(HubError):
        mem(hub, "a3", target="user", action="add", content="The owner likes tables.", scope="team")


# ============================================================================ schema hub-self
def test_schema_shows_scope_only_when_allowed():
    plain = memory_tool_def(["agent"])
    assert "scope" not in plain["inputSchema"]["properties"]
    lead = memory_tool_def(["agent", "team"])
    assert lead["inputSchema"]["properties"]["scope"]["enum"] == ["agent", "team"] and "team_id" in lead["inputSchema"]["properties"]
    assert "'global'" not in lead["description"]
    full = memory_tool_def(["agent", "team", "global"])
    assert full["inputSchema"]["properties"]["scope"]["enum"] == ["agent", "team", "global"]
    from pathlib import Path

    e = server_entry("r1", Path("s"), Path("h"), "worker", memory_scopes=["agent", "team"])
    assert e["args"][-2:] == ["--memory-scopes", "agent,team"]
    assert "--memory-scopes" not in server_entry("r1", Path("s"), Path("h"), "worker", memory_scopes=["agent"])["args"]
