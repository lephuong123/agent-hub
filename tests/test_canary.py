"""M5-A: pipeline L3 tầng team/global — eval trước (mẫu agent) → canary → promoted | tự rollback (FR-LN-05/16, Q-22).
Không gọi claude: gate giả, run/Signal giả."""

from datetime import datetime, timedelta, timezone

import pytest

from agent_hub.events import iso
from m5a_fake import FakeGate, add_run, make_hub, phash, prompt_of, team_memory, types

FACT = "The team release train leaves every Thursday at 15:00 Hanoi time."


@pytest.fixture
def hub(hub_home):
    h = make_hub(hub_home)
    yield h
    h.store.close()


def test_team_change_eval_canary_then_promoted_without_asking(hub):
    h_a2 = phash(hub, "a2")
    r = team_memory(hub, FACT)
    assert r["status"] == "canary" and r["canary_agent"] == "a1", r
    # eval trước áp dụng trên golden task của mọi agent bị ảnh hưởng (3 ≤ mẫu)
    assert sorted(c["agent_id"] for c in hub.evals.gate.calls) == ["a1", "a2", "a3"]
    live = (hub.paths.home / "teams" / "t1" / "memory.md").read_text()
    assert FACT in live
    assert FACT in prompt_of(hub, "a1")
    assert FACT not in prompt_of(hub, "a2") and FACT not in prompt_of(hub, "a3")
    assert phash(hub, "a2") == h_a2  # agent khác giữ nguyên prompt → giữ cache
    vm = {v["ref"]: v for v in hub.knowledge.versions.version_map("a2")}
    assert vm["memory.md"]["source"] == "pinned_before"
    assert {v["ref"]: v for v in hub.knowledge.versions.version_map("a1")}["memory.md"]["source"] == "canary"
    assert "learning.canary" in types(hub)
    c = hub.learning.canary.list("canary")[0]
    for i in range(4):
        add_run(hub, "a1", f"r{i}")
        assert hub.learning.canary.check(c["id"])["action"] == "watching"
    add_run(hub, "a1", "r4")
    res = hub.learning.canary.check(c["id"])
    assert res["action"] == "promoted"
    assert FACT in prompt_of(hub, "a2") and FACT in prompt_of(hub, "a3")
    assert hub.learning.pipeline.get(r["proposal_id"])["status"] == "promoted"
    ev = next(e for e in hub.events if e.type == "learning.promoted")
    assert ev.payload["agents"] == ["a1", "a2", "a3"] and ev.team_id == "t1"
    assert hub.learning.status()["pending_approval_queue"] == 0


def test_regression_on_canary_rolls_back_before_spreading(hub):
    now = datetime.now(timezone.utc)
    for i in range(5):  # mốc 7 ngày: 5 run sạch
        add_run(hub, "a1", f"b{i}", now - timedelta(days=1, minutes=i))
    h_a2 = phash(hub, "a2")
    r = team_memory(hub, FACT)
    cid = r["canary_id"]
    add_run(hub, "a1", "w1", correction=True)
    assert hub.learning.canary.check(cid)["action"] == "watching"  # 1 sự kiện < min_events
    add_run(hub, "a1", "w2", correction=True)  # Signal → listener kiểm canary ngay → rollback
    assert hub.learning.canary.check(cid)["status"] == "rolled_back"
    assert FACT not in (hub.paths.home / "teams" / "t1" / "memory.md").read_text()
    assert FACT not in prompt_of(hub, "a1") and FACT not in prompt_of(hub, "a2")
    assert phash(hub, "a2") == h_a2
    rb = next(e for e in hub.events if e.type == "learning.rolled_back")
    assert rb.payload["actor"] == "canary"
    c = hub.learning.canary.get(cid)
    assert c["status"] == "rolled_back" and "rollback trước khi lan" in c["reason"]
    assert hub.learning.pipeline.get(r["proposal_id"])["status"] == "rolled_back"
    assert "learning.promoted" not in types(hub)


def test_signal_event_triggers_check_automatically(hub):
    r = team_memory(hub, FACT)
    add_run(hub, "a1", "w1", correction=True)
    add_run(hub, "a1", "w2", correction=True)  # Signal → listener → kiểm canary (không cần tick)
    assert hub.learning.canary.get(r["canary_id"])["status"] == "rolled_back"


def test_deadline_promotes_and_tick(hub):
    r = team_memory(hub, FACT)
    hub.store.conn.execute("UPDATE tier_canaries SET deadline=? WHERE id=?", (iso(datetime.now(timezone.utc) - timedelta(minutes=1)), r["canary_id"]))
    out = hub.learning.tick()
    assert out["canary"][0]["action"] == "promoted"


def test_eval_failed_on_one_agent_blocks_change(hub):
    hub.evals.gate = FakeGate({"a3": "eval_failed"})
    r = team_memory(hub, FACT)
    assert r["status"] == "eval_failed"
    assert FACT not in (hub.paths.home / "teams" / "t1" / "memory.md").read_text()
    assert hub.learning.canary.list() == []


def test_no_golden_defers(hub_home):
    h = make_hub(hub_home, golden=())
    r = team_memory(h, FACT)
    assert r["status"] == "deferred_no_eval" and "golden" in r["reason"]
    h.store.close()


def test_sample_at_least_three_includes_origin(hub_home):
    ags = ("a1", "a2", "a3", "a4", "a5", "a6")
    h = make_hub(hub_home, agents=ags, golden=ags)
    r = team_memory(h, FACT, agent="a4")
    called = [c["agent_id"] for c in h.evals.gate.calls]
    assert r["status"] == "canary" and len(called) == 3 and "a4" in called
    h.store.close()


def test_same_target_deferred_while_canary_active(hub):
    team_memory(hub, FACT)
    r2 = team_memory(hub, "The team demo happens on the last Friday of each month.", agent="a2")
    assert r2["status"] == "deferred_rate_limited" and "canary" in r2["reason"]


def test_entry_rolled_back_elsewhere_ends_canary(hub):
    r = team_memory(hub, FACT)
    hub.learning.ledger.rollback(r["ledger_entry_id"], actor="user")
    assert hub.learning.canary.check(r["canary_id"])["action"] == "rolled_back_elsewhere"
    assert hub.learning.canary.get(r["canary_id"])["status"] == "rolled_back"


def test_team_skill_canary_hides_new_skill_and_pins_old_version(hub):
    P = hub.learning.pipeline
    r = P.propose(target="skill", op="add", agent_id="a1", scope="team", created_by="review",
                  payload={"name": "release-notes", "description": "Write team release notes", "body": "# Release notes\n\n1. List merged PRs.\n"})
    assert r["status"] == "canary", r
    names = lambda a: {s["name"] for s in hub.knowledge.effective_skills(a)}  # noqa: E731
    assert "release-notes" in names("a1") and "release-notes" not in names("a2")
    from agent_hub.errors import NotFound

    with pytest.raises(NotFound):
        hub.knowledge.skill_view("a2", "release-notes")
    hub.learning.canary.promote(r["canary_id"], "test")
    assert "release-notes" in names("a2")
    hub.learning.policy.set("rate.cooldown_per_target_h", 0)
    # patch: agent khác thấy ĐÚNG bản cũ (mô tả + nội dung) trong lúc canary
    hub.learning.read_marks.setdefault("rv", set()).add(("team", "t1", "release-notes", "SKILL.md"))
    r2 = P.propose(target="skill", op="patch", agent_id="a1", scope="team", created_by="review", run_id="rv",
                   payload={"name": "release-notes", "old_string": "1. List merged PRs.", "new_string": "1. List merged PRs.\n2. Group them by area."})
    assert r2["status"] == "canary", r2
    assert "Group them" in hub.knowledge.skill_view("a1", "release-notes")["content"]
    v2 = hub.knowledge.skill_view("a2", "release-notes")["content"]
    assert "Group them" not in v2 and "List merged PRs" in v2
    assert hub.knowledge.skill_view("a2", "release-notes")["support_files"] == []


def test_human_team_change_needs_team_and_resolves_single_team(hub):
    r = hub.learning.pipeline.propose(target="memory", op="add", scope="team", team_id="t1", payload={"action": "add", "content": FACT}, created_by="user")
    # hardening (quyết định coordinator): thay đổi của NGƯỜI áp dụng ngay cho cả team, không eval trước, không ghim — chỉ theo dõi
    assert r["status"] == "canary" and r["mode"] == "watch"
    c = hub.learning.canary.get(r["canary_id"])
    assert c["canary_agent_id"] is None and sorted(c["watch_agents"]) == ["a1", "a2", "a3"]
    assert hub.evals.gate.calls == []


def test_tui_canary_head_lists_running(hub):
    from agent_hub.tui.learnings import canary_head

    assert "không có" in canary_head([]).plain
    r = team_memory(hub, FACT)
    txt = canary_head(hub.learning.canary.list("canary")).plain
    assert r["canary_id"] in txt and "trên a1" in txt and "0/5 run" in txt
