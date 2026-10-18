"""M5-A: tầng tri thức team/global — đọc theo phiên bản agent, ứng viên eval trên bản sao home (`.tier_overlay`),
prompt giữ thứ tự ổn định (FR-LN-11, FR-RT-10). Không gọi claude."""

import pytest
from m5a_fake import make_hub, phash, prompt_of, team_memory

from agent_hub.evals.overlay import make_home
from agent_hub.knowledge import build_knowledge_blocks, render_skill

FACT = "The team release train leaves every Thursday at 15:00 Hanoi time."


@pytest.fixture
def hub(hub_home):
    h = make_hub(hub_home)
    yield h
    h.store.close()


def test_prompt_order_global_team_agent(hub):
    (hub.paths.home / "global").mkdir(exist_ok=True)
    (hub.paths.home / "global" / "USER.md").write_text("# User profile\nThe user prefers concise answers.\n")
    (hub.paths.home / "teams" / "t1").mkdir(parents=True, exist_ok=True)
    (hub.paths.home / "teams" / "t1" / "memory.md").write_text("# Memory\nTeam repo is monorepo-x.\n")
    (hub.paths.home / "agents" / "a1" / "MEMORY.md").write_text("# Memory\nAgent note one.\n")
    p = prompt_of(hub, "a1")
    i_g, i_t, i_a = p.index("concise answers"), p.index("monorepo-x"), p.index("Agent note one")
    assert i_g < i_t < i_a
    assert "team t1" in p and "shared" in p


def test_materialize_team_candidate_only_in_eval_copy(hub, tmp_path):
    hub.evals.gate = None  # chỉ cần proposal được lưu: gate stub → deferred_no_eval
    r = team_memory(hub, FACT)
    assert r["status"] == "deferred_no_eval"
    copy = make_home(hub.paths.home / "agents" / "a2", tmp_path / "copy")
    cfg: dict = {}
    hub.learning.materialize({"id": r["proposal_id"]}, copy, cfg)
    assert (copy / ".tier_overlay" / "teams" / "t1" / "memory.md").exists()
    blocks, _ = build_knowledge_blocks(hub.knowledge, "a2", home=copy)
    assert any(FACT in b for b in blocks)
    blocks, _ = build_knowledge_blocks(hub.knowledge, "a2", home=hub.paths.home / "agents" / "a2")
    assert not any(FACT in b for b in blocks)
    assert FACT not in prompt_of(hub, "a2")


def test_materialize_team_skill_patch_and_archive(hub, tmp_path):
    sk = hub.paths.home / "teams" / "t1" / "skills" / "deploy-steps"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(render_skill("deploy-steps", "Deploy the service", "1. Build.\n"))
    (sk / "references").mkdir()
    (sk / "references" / "env.md").write_text("env notes")
    hub.evals.gate = None
    hub.learning.read_marks.setdefault("rv", set()).add(("team", "t1", "deploy-steps", "SKILL.md"))
    r = hub.learning.pipeline.propose(target="skill", op="patch", agent_id="a1", scope="team", created_by="review", run_id="rv",
                                      payload={"name": "deploy-steps", "old_string": "1. Build.", "new_string": "1. Build.\n2. Smoke test."})
    copy = make_home(hub.paths.home / "agents" / "a2", tmp_path / "c1")
    hub.learning.materialize({"id": r["proposal_id"]}, copy, {})
    v = hub.knowledge.skill_view("a2", "deploy-steps", home=copy)
    assert "Smoke test" in v["content"] and v["support_files"] == ["references/env.md"]
    assert "Smoke test" not in hub.knowledge.skill_view("a2", "deploy-steps")["content"]
    g = hub.paths.home / "teams" / "t1" / "skills" / "deploy-guide"
    g.mkdir(parents=True)
    (g / "SKILL.md").write_text(render_skill("deploy-guide", "Deploy guide", "Build, smoke test, release.\n"))
    hub.learning.read_marks["rv"].add(("team", "t1", "deploy-guide", "SKILL.md"))
    r2 = hub.learning.pipeline.propose(target="skill", op="archive", agent_id="a1", scope="team", created_by="review", run_id="rv",
                                       payload={"name": "deploy-steps", "absorbed_into": "deploy-guide"})
    assert r2["status"] != "blocked", r2
    copy2 = make_home(hub.paths.home / "agents" / "a2", tmp_path / "c2")
    hub.learning.materialize({"id": r2["proposal_id"]}, copy2, {})
    assert "deploy-steps" not in {s["name"] for s in hub.knowledge.effective_skills("a2", home=copy2)}
    assert "deploy-steps" in {s["name"] for s in hub.knowledge.effective_skills("a2")}


def test_agent_not_in_team_never_sees_team_tier(hub):
    hub.agents.create({"id": "solo", "name": "Solo", "model": "haiku"})
    r = team_memory(hub, FACT)
    hub.learning.canary.promote(r["canary_id"], "test")
    assert FACT in prompt_of(hub, "a2") and FACT not in prompt_of(hub, "solo")


def test_global_memory_canary_pins_every_other_agent(hub):
    hub.agents.create({"id": "solo", "name": "Solo", "model": "haiku"})
    before = phash(hub, "solo")
    r = hub.learning.pipeline.propose(target="memory", op="add", agent_id="a1", scope="global", created_by="review",
                                      payload={"store": "memory", "action": "add", "content": "The company VPN hostname is vpn.example.internal."})
    assert r["status"] == "canary" and r["mode"] == "canary"
    assert "vpn.example" in prompt_of(hub, "a1") and phash(hub, "solo") == before
    t = hub.knowledge.versions.tier_overview("global", None)
    assert t["canaries"][0]["canary_agent_id"] == "a1"


def test_review_scope_team_resolves_team_and_rejects_ambiguous(hub):
    hub.teams.create({"id": "t2", "name": "T2"})
    hub.teams.member_set("t2", "a1", "worker")
    from agent_hub.errors import HubError

    with pytest.raises(HubError):
        team_memory(hub, FACT, agent="a1")
    r = team_memory(hub, FACT, agent="a2")
    assert r["status"] == "canary"
