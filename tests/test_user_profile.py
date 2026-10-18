"""M5-A: hồ sơ người dùng dùng chung (FR-LN-14) — một hồ sơ (global/USER.md, L3 eval-shadow → áp dụng cho mọi agent +
theo dõi), quan sát riêng mỗi agent (peer), pin từng dòng. Không gọi claude."""

import pytest
from m5a_fake import add_run, make_hub, prompt_of

PREF = "The user prefers dates written as dd/mm/yyyy in every report."


@pytest.fixture
def hub(hub_home):
    h = make_hub(hub_home)
    yield h
    h.store.close()


def user_change(h, content=None, action="add", old=None, agent="a1"):
    return h.learning.pipeline.propose(target="user_profile", op="add" if action == "add" else "patch", agent_id=agent, created_by="review",
                                       payload={"store": "user", "action": action, "content": content, "old_text": old})


def test_user_profile_shadow_eval_then_applies_to_all_with_watch(hub):
    r = user_change(hub, PREF)
    assert r["status"] == "canary" and r["mode"] == "shadow", r
    assert sorted(c["agent_id"] for c in hub.evals.gate.calls) == ["a1", "a2", "a3"]  # shadow = eval trên bản sao
    for a in ("a1", "a2", "a3"):
        assert PREF in prompt_of(hub, a)  # không ghim — một hồ sơ cho mọi agent
    c = hub.learning.canary.get(r["canary_id"])
    assert c["canary_agent_id"] is None and sorted(c["watch_agents"]) == ["a1", "a2", "a3"]
    for i in range(5):
        add_run(hub, ["a1", "a2", "a3"][i % 3], f"r{i}")
    assert hub.learning.canary.check(c["id"])["action"] == "promoted"
    assert hub.learning.pipeline.get(r["proposal_id"])["status"] == "promoted"


def test_user_profile_regression_on_any_watched_agent_rolls_back(hub):
    r = user_change(hub, PREF)
    add_run(hub, "a2", "w1", correction=True)
    add_run(hub, "a3", "w2", correction=True)
    assert hub.learning.canary.get(r["canary_id"])["status"] == "rolled_back"
    assert PREF not in prompt_of(hub, "a1")


def test_line_pin_blocks_learning_patch(hub):
    r = user_change(hub, PREF)
    hub.learning.canary.promote(r["canary_id"], "test")
    from agent_hub.learning.canary import entry_hash

    hub.knowledge.set_pin("global", None, "memory_entry", entry_hash(PREF), True)
    r2 = user_change(hub, "The user prefers dates written as yyyy-mm-dd.", action="patch", old="dd/mm/yyyy in every report")
    assert r2["status"] == "blocked" and r2["rule"] == "pinned"
    view = hub.knowledge.versions.user_profile()
    assert view["shared"][0]["pinned"] is True


def test_peer_observations_are_private_and_not_duplicated(hub):
    r = user_change(hub, PREF)
    hub.learning.canary.promote(r["canary_id"], "test")
    P = hub.learning.pipeline
    ok = P.propose(target="memory", op="add", agent_id="a2", created_by="review",
                   payload={"store": "user", "action": "add", "content": "The user reviews pull requests in the morning."})
    assert ok["status"] == "applied"
    dup = P.propose(target="memory", op="add", agent_id="a3", created_by="review",
                    payload={"store": "user", "action": "add", "content": "The user prefers dates written as dd/mm/yyyy in every report."})
    assert dup["status"] == "blocked" and dup["rule"] == "one_fact_one_store"
    assert "pull requests in the morning" in prompt_of(hub, "a2")
    assert "pull requests in the morning" not in prompt_of(hub, "a1")  # quan sát của peer không nhiễm chéo
    v = hub.knowledge.versions.user_profile()
    assert list(v["peers"]) == ["a2"] and v["shared"][0]["text"] == PREF
