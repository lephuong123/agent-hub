"""Luồng C workspace song song (docs/dev-notes/workspace-parallel.md §C): khối "Workspace rules" trong system prompt theo
ngữ cảnh workspace của run, cờ `prompt.workspace_rules`, `isolate` trong kế hoạch lead. Không gọi claude."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cfharness.dispatch.plan import PLAN_SCHEMA, PlanError, validate_plan
from cfharness.hub import Hub
from cfharness.learning.runtime import system_prompt_for, workspace_context
from cfharness.prompt import build_system_prompt, prompt_hash, workspace_rules_block
from m3_fake import ScriptedRuntime, kind_of

META = {"repo": "/r/app", "branch": "cfh/root1234/sub5678", "base": "main", "path": "/r/_wt/app-x", "owned": True,
        "created_at": "2026-11-23T00:00:00Z", "deps": {"mode": "link", "needs_install": True}}


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    p = tmp_path / name
    (p / ".git").mkdir(parents=True)
    return p


# ---------------------------------------------------------------- khối thuần
def test_block_worktree_context():
    b = workspace_rules_block({"worktree": META, "locked": True})
    assert b.startswith("# Workspace rules")
    assert "take precedence over any conflicting instruction in your Persona (SOUL)" in b
    assert "`cfh/root1234/sub5678`" in b and "based on `main`" in b and "isolated" in b
    assert "npm ci" in b and "node_modules" not in b
    assert "git add -A" not in b  # không phải checkout dùng chung
    linked = workspace_rules_block({"worktree": {**META, "deps": {"mode": "link", "needs_install": False}}})
    assert "npm ci" not in linked and "never delete" in linked and "node_modules" in linked
    legacy = workspace_rules_block({"worktree": {}})  # kind `worktree` cũ (claude --worktree), không metadata
    assert "current branch" in legacy and "npm ci" not in legacy


def test_block_main_checkout_context():
    b = workspace_rules_block({"checkout": "cwd", "locked": True})
    for must in ("git worktree add", "create a task with `worktree`", "git add -A", "git add .", "git commit -a",
                 "Do not checkout, switch, reset", "repo lock"):
        assert must in b, must
    assert "repo lock" not in workspace_rules_block({"checkout": "cwd", "locked": False})
    extra = workspace_rules_block({"checkout": "extra", "locked": True})
    assert "extra write folders" in extra and "repo lock" not in extra
    assert len(b.splitlines()) <= 15 and len(workspace_rules_block({"worktree": META, "checkout": "extra"}).splitlines()) <= 15
    assert workspace_rules_block(None) == "" and workspace_rules_block({"locked": True}) == ""


def test_block_position_and_hash():
    rules = workspace_rules_block({"checkout": "cwd", "locked": True})
    base = build_system_prompt("a1", "Never create a git worktree. Always work in the main checkout.", [])
    text = build_system_prompt("a1", "Never create a git worktree. Always work in the main checkout.", [], workspace_rules=rules)
    assert text.index("# cfharness conventions") < text.index("# Workspace rules") < text.index("# Persona (SOUL)")
    assert prompt_hash(base) != prompt_hash(text)
    assert prompt_hash(text) == prompt_hash(build_system_prompt("a1", "Never create a git worktree. Always work in the main checkout.", [],
                                                                workspace_rules=rules))  # ổn định → giữ prompt cache
    wt = build_system_prompt("a1", "", [], workspace_rules=workspace_rules_block({"worktree": META}))
    assert prompt_hash(wt) != prompt_hash(text)


# ---------------------------------------------------------------- ngữ cảnh theo run
@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home)
    h.agents.create({"id": "c1", "name": "C1", "model": "haiku"})
    h.agents.create({"id": "ro", "name": "RO", "model": "haiku", "tool_policy": {"permission_mode": "plan"}})
    yield h
    h.store.close()


def test_context_per_run(hub, tmp_path):
    agent = hub.store.get_agent("c1")
    repo = _repo(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    ctx = workspace_context(hub, {"id": "r1", "cwd": str(repo)}, agent)
    assert ctx and ctx["checkout"] == "cwd" and "worktree" not in ctx
    sub = repo / "pkg"
    sub.mkdir()
    assert workspace_context(hub, {"id": "r1", "cwd": str(sub)}, agent)["checkout"] == "cwd"  # thư mục con của repo
    assert workspace_context(hub, {"id": "r1", "cwd": str(repo), "run_opts": {"read_only": True}}, agent) is None
    assert workspace_context(hub, {"id": "r1", "cwd": str(repo)}, hub.store.get_agent("ro")) is None  # agent chỉ đọc
    assert workspace_context(hub, {"id": "r1", "cwd": str(repo), "run_opts": {"learning_review": True}}, agent) is None
    assert workspace_context(hub, {"id": "r1", "cwd": str(plain)}, agent) is None  # không phải git
    assert workspace_context(hub, {"id": "r1"}, agent) is None  # scratch thuần
    assert workspace_context(hub, {"id": "r1", "run_opts": {"extra_write_roots": [str(tmp_path / "_wt" / "x")]}}, agent)["checkout"] == "extra"
    assert workspace_context(hub, {"id": "r1", "cwd": str(repo), "run_opts": {"workspace_kind": "worktree"}}, agent)["worktree"] == {}
    assert workspace_context(hub, {"id": "r1", "cwd": str(repo), "run_opts": {"workspace_kind": "filehub"}}, agent) is None


def test_context_reads_hub_worktree_metadata_from_task(hub, tmp_path):
    agent = hub.store.get_agent("c1")
    wt = _repo(tmp_path, "wt")
    (wt / ".git").rmdir()
    (wt / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")  # worktree liên kết: .git là file
    meta = {**META, "path": str(wt)}
    t = hub.tasks.create({"assignee": "c1", "title": "x", "body": "y", "workspace": {"kind": "dir", "path": str(wt), "worktree": meta}})
    ctx = workspace_context(hub, {"id": "r1", "cwd": str(wt), "task_id": t["id"]}, agent)
    assert ctx["worktree"]["branch"] == META["branch"] and "checkout" not in ctx
    # metadata của worktree khác (cwd không khớp) → không nhận, coi như checkout
    other = _repo(tmp_path, "other")
    ctx2 = workspace_context(hub, {"id": "r2", "cwd": str(other), "task_id": t["id"]}, agent)
    assert "worktree" not in ctx2 and ctx2["checkout"] == "cwd"
    text, _ = system_prompt_for(hub, {"id": "r3", "cwd": str(wt), "task_id": t["id"]}, agent)
    assert META["branch"] in text and "npm ci" in text


def test_system_prompt_for_and_config_off(hub, tmp_path):
    agent = hub.store.get_agent("c1")
    repo = _repo(tmp_path)
    on, _ = system_prompt_for(hub, {"id": "r1", "cwd": str(repo)}, agent)
    assert "# Workspace rules" in on and "git add -A" in on
    scratch, _ = system_prompt_for(hub, {"id": "r2"}, agent)
    assert "# Workspace rules" not in scratch
    hub.config.set("prompt.workspace_rules", False)
    off, _ = system_prompt_for(hub, {"id": "r3", "cwd": str(repo)}, agent)
    assert "# Workspace rules" not in off and prompt_hash(off) != prompt_hash(on)


# ---------------------------------------------------------------- isolate trong kế hoạch lead
def test_plan_schema_isolate():
    assert PLAN_SCHEMA["properties"]["subtasks"]["items"]["properties"]["isolate"] == {"type": "boolean"}
    plan = {"subtasks": [{"key": "a", "title": "A", "body": "a", "isolate": True}, {"key": "b", "title": "B", "body": "b"}]}
    out = {s["key"]: s for s in validate_plan(plan, [], lead=None)}
    assert out["a"]["isolate"] is True and out["b"]["isolate"] is False
    with pytest.raises(PlanError, match="isolate"):
        validate_plan({"subtasks": [{"key": "a", "title": "A", "body": "a", "isolate": "yes"}]}, [], lead=None)


async def test_lead_plan_isolate_stored_on_subtask_workspace(hub_home, tmp_path):
    repo = _repo(tmp_path)
    plan = {"summary": "two edits", "subtasks": [
        {"key": "a", "title": "Edit A", "body": "edit a", "role": "coder", "isolate": True},
        {"key": "b", "title": "Edit B", "body": "edit b", "role": "coder"},
    ]}
    rt = ScriptedRuntime(lambda spec: {"structured": plan, "text": "plan"} if kind_of(spec) == "lead" else {"text": "done"})
    h = Hub(hub_home, runtime=rt)
    h.config.set("tasks.tick_s", 0.05)
    h.start()
    try:
        h.agents.create({"id": "lead", "name": "Lead", "role": "planner", "description": "Plans"})
        h.agents.create({"id": "dev", "name": "Dev", "role": "coder", "description": "Writes code"})
        h.teams.create({"id": "eng", "name": "Eng", "members": [{"agent_id": "lead", "role": "lead"}, {"agent_id": "dev", "role": "worker"}]})
        root = h.tasks.create({"team": "eng", "title": "Two edits", "body": "x", "workspace": {"kind": "dir", "path": str(repo)}})
        loop = asyncio.get_running_loop()
        end = loop.time() + 5
        while loop.time() < end and len(h.tasks.subtasks(root["id"])) < 2:
            await asyncio.sleep(0.02)
        subs = {s["title"]: h.tasks.row(s["id"]) for s in h.tasks.subtasks(root["id"])}
        assert subs["Edit A"]["workspace"]["isolate"] is True
        assert subs["Edit A"]["workspace"]["path"] == subs["Edit B"]["workspace"]["path"]
        assert "isolate" not in subs["Edit B"]["workspace"]
        while loop.time() < end and not any(kind_of(s) != "lead" for s in rt.specs.values()):
            await asyncio.sleep(0.02)
        lead_spec = next(s for s in rt.specs.values() if kind_of(s) == "lead")
        assert "# Workspace rules" not in (lead_spec.system_prompt or "")  # run lead chỉ đọc
        assert "isolate: true" in lead_spec.prompt
        work = next(s for s in rt.specs.values() if kind_of(s) != "lead")
        assert "# Workspace rules" in (work.system_prompt or "")
    finally:
        h.runs.shutting_down = True
        h.store.close()
