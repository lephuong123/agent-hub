"""M4-A kho tri thức tầng agent (FR-LN-11) + lắp prompt giữ cache (FR-RT-10): memory có hạn mức, skill index ≤ 60 ký tự,
che khuất theo tầng, pin, xoá mềm skill, prompt frozen theo session. Không gọi claude."""

import pytest

from agent_hub.hub import Hub
from agent_hub.knowledge import (
    KnowledgeError,
    apply_memory_op,
    check_skill_name,
    parse_memory,
    parse_skill,
    render_memory,
    render_skill,
)
from agent_hub.learning.runtime import system_prompt_for
from agent_hub.prompt import prompt_hash


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home)
    h.agents.create({"id": "r1", "name": "R1", "model": "haiku"})
    yield h
    h.store.close()


def test_memory_roundtrip_and_ops():
    text = render_memory(["The repo uses pnpm.", "CI runs on GitHub Actions."], "memory")
    assert text.startswith("# Memory\n") and "\n§\n" in text
    assert parse_memory(text) == ["The repo uses pnpm.", "CI runs on GitHub Actions."]
    assert parse_memory("# Memory\n") == []
    e = apply_memory_op(["a fact here"], "add", "another fact", None)
    assert e == ["a fact here", "another fact"]
    assert apply_memory_op(e, "patch", "first", "a fact") == ["first here", "another fact"]
    assert apply_memory_op(e, "remove", None, "another") == ["a fact here"]
    with pytest.raises(KnowledgeError):
        apply_memory_op(e, "add", "another fact", None)  # trùng
    with pytest.raises(KnowledgeError):
        apply_memory_op(e, "patch", "x", "fact")  # khớp nhiều mục


def test_skill_names_class_level():
    check_skill_name("daily-report")
    for bad in ("fix-login-bug", "pr-1234-notes", "report-2026-10", "Weekly", "a"):
        with pytest.raises(KnowledgeError):
            check_skill_name(bad)
    fm, body = parse_skill(render_skill("x-y", "Short desc", "# Body\n"))
    assert fm == {"name": "x-y", "description": "Short desc"} and body.startswith("# Body")


def test_prompt_has_memory_snapshot_and_skill_index(hub):
    P = hub.learning.pipeline
    long_desc = "Write the daily status report for the team with dates and blockers"
    r = P.propose(target="skill", op="add", agent_id="r1", payload={"name": "daily-report", "description": long_desc[:60], "body": "# Daily\n1. Dates as dd/mm/yyyy.\n"}, created_by="user")
    assert r["status"] == "applied", r
    r = P.propose(target="memory", op="add", agent_id="r1", payload={"store": "memory", "action": "add", "content": "The team standup is at 09:00 Hanoi time."}, created_by="user")
    assert r["status"] == "applied", r
    agent = hub.store.get_agent("r1")
    text, info = system_prompt_for(hub, {"id": "r_x"}, agent)
    assert info["skills"] == 1
    i_soul, i_mem, i_idx = text.index("# Persona (SOUL)"), text.index("MEMORY (your notes)"), text.index("# Skills index")
    assert i_soul < i_mem < i_idx  # thứ tự ổn định FR-RT-10
    line = next(ln for ln in text.splitlines() if ln.startswith("- daily-report:"))
    assert len(line.split(": ", 1)[1]) <= 60
    assert "skill_view" in text and "dd/mm/yyyy" not in text  # chỉ mục, không chép thân skill
    assert (hub.paths.home / "prompts" / f"{prompt_hash(text)}.md").exists()


def test_resumed_session_keeps_prompt_new_session_changes(hub):
    agent = hub.store.get_agent("r1")
    t1, _ = system_prompt_for(hub, {"id": "r_1"}, agent)
    hub.store.insert_run({"id": "r_1", "agent_id": "r1", "kind": "adhoc", "session_id": "s-1", "status": "completed", "queued_at": "2026-10-18T00:00:00.000Z",
                          "system_prompt_hash": prompt_hash(t1)})
    hub.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"store": "memory", "action": "add", "content": "Deploys happen on Fridays only."}, created_by="user")
    resumed, _ = system_prompt_for(hub, {"id": "r_2", "resumed_from_session": "s-1"}, agent)
    fresh, _ = system_prompt_for(hub, {"id": "r_3"}, agent)
    assert prompt_hash(resumed) == prompt_hash(t1)  # session cũ: hash giống (cache)
    assert prompt_hash(fresh) != prompt_hash(t1) and "Deploys happen on Fridays" in fresh


def test_tier_shadowing_and_global_skills(hub):
    g = hub.paths.home / "global" / "skills" / "daily-report"
    g.mkdir(parents=True)
    (g / "SKILL.md").write_text(render_skill("daily-report", "Global version", "global body"))
    (hub.paths.home / "global" / "skills" / "shared-tool").mkdir()
    (hub.paths.home / "global" / "skills" / "shared-tool" / "SKILL.md").write_text(render_skill("shared-tool", "Shared", "body"))
    hub.learning.pipeline.propose(target="skill", op="add", agent_id="r1", payload={"name": "daily-report", "description": "Agent version", "body": "agent body"}, created_by="user")
    eff = {s["name"]: s for s in hub.knowledge.effective_skills("r1")}
    assert eff["daily-report"]["scope"] == "agent" and eff["daily-report"]["shadows"] == ["global:global"]
    assert eff["shared-tool"]["scope"] == "global"
    v = hub.knowledge.skill_view("r1", "daily-report")
    assert "agent body" in v["content"]
    row = hub.store.conn.execute("SELECT use_count, created_by FROM skills WHERE scope='agent' AND owner_id='r1' AND name='daily-report'").fetchone()
    assert row[0] == 1 and row[1] == "user"  # provenance created_by


def test_pin_blocks_learning_but_not_user(hub):
    P = hub.learning.pipeline
    P.propose(target="skill", op="add", agent_id="r1", payload={"name": "daily-report", "description": "Report", "body": "step one"}, created_by="user")
    hub.knowledge.set_pin("agent", "r1", "skill", "daily-report", True)
    hub.learning.read_marks["r_rev"] = {("agent", "r1", "daily-report", "SKILL.md")}
    r = P.propose(target="skill", op="patch", agent_id="r1", payload={"name": "daily-report", "old_string": "step one", "new_string": "step 1"}, created_by="review", run_id="r_rev")
    assert r["status"] == "blocked" and r["rule"] == "pinned"
    r = P.propose(target="skill", op="patch", agent_id="r1", payload={"name": "daily-report", "old_string": "step one", "new_string": "step 1"}, created_by="user")
    assert r["status"] == "applied"


def test_archive_is_soft_and_rollback_restores(hub):
    P = hub.learning.pipeline
    P.propose(target="skill", op="add", agent_id="r1", payload={"name": "old-flow", "description": "Old", "body": "old content\n"}, created_by="user")
    P.propose(target="skill", op="add", agent_id="r1", payload={"name": "new-flow", "description": "New", "body": "new content incl old\n"}, created_by="user")
    r = P.propose(target="skill", op="archive", agent_id="r1", payload={"name": "old-flow", "absorbed_into": "new-flow"}, created_by="user")
    assert r["status"] == "applied" and r["risk_class"] == "L2"
    home = hub.paths.agent_home("r1")
    assert not (home / "skills" / "old-flow").exists()
    arch = list((home / ".archive" / "skills").iterdir())
    assert len(arch) == 1 and (arch[0] / "SKILL.md").read_text().endswith("old content\n")  # xoá mềm (FR-SG-06)
    assert "old-flow" not in {s["name"] for s in hub.knowledge.effective_skills("r1")}
    hub.learning.ledger.rollback(r["ledger_entry_id"])
    assert (home / "skills" / "old-flow" / "SKILL.md").read_text().endswith("old content\n")
    assert "old-flow" in {s["name"] for s in hub.knowledge.effective_skills("r1")}


def test_prompt_and_skill_view_read_from_agent_home_path_copy(hub, tmp_path):
    """Hợp đồng M4-B: eval chạy agent trên BẢN SAO home → tri thức phải đọc từ `agent["home_path"]` (không từ HUB_HOME/agent_id)."""
    import shutil

    hub.learning.pipeline.propose(target="skill", op="add", agent_id="r1", payload={"name": "daily-report", "description": "Real", "body": "REAL BODY"}, created_by="user")
    hub.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"action": "add", "content": "Real memory fact for the agent."}, created_by="user")
    copy = tmp_path / "evalhome"
    shutil.copytree(hub.paths.agent_home("r1"), copy)
    (copy / "MEMORY.md").write_text("# Memory\nCandidate memory fact from the proposal.\n")
    (copy / "skills" / "daily-report" / "SKILL.md").write_text(render_skill("daily-report", "Candidate", "CANDIDATE BODY"))
    (copy / "skills" / "extra-skill").mkdir()
    (copy / "skills" / "extra-skill" / "SKILL.md").write_text(render_skill("extra-skill", "Only in candidate", "x"))
    agent = dict(hub.store.get_agent("r1"), home_path=str(copy))
    text, info = system_prompt_for(hub, {"id": "r_eval"}, agent)
    assert "Candidate memory fact" in text and "Real memory fact" not in text
    assert "- daily-report: Candidate" in text and "- extra-skill:" in text and info["skills"] == 2
    real, _ = system_prompt_for(hub, {"id": "r_real"}, hub.store.get_agent("r1"))
    assert "Real memory fact" in real and "extra-skill" not in real
    # skill_view trong run eval (run_opts.eval_overlay.home) đọc bản sao
    hub.store.insert_run({"id": "r_eval", "agent_id": "r1", "kind": "eval", "status": "running", "queued_at": "2026-10-18T00:00:00.000Z",
                          "run_opts": {"eval_overlay": {"home": str(copy)}}})
    v = hub.learning.learnself_call("r_eval", "skill_view", {"name": "daily-report"})
    assert "CANDIDATE BODY" in v["content"]
    assert "REAL BODY" in (hub.paths.agent_home("r1") / "skills" / "daily-report" / "SKILL.md").read_text()
