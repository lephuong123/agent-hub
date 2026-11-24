"""Luồng D workspace song song — tự cô lập & tích hợp (docs/dev-notes/workspace-parallel.md §D).

Repo git tạm + `ScriptedRuntime` (không gọi claude): task con của task gốc tách việc được tách vào worktree `cfh/<gốc>/<task>` lúc
dispatch, xong thì ghép vào nhánh tích hợp `cfh/<gốc>/integration`; xung đột thật → task merge; lockfile → tự xử + cờ; task gốc xong →
để nhánh tích hợp, gỡ worktree con; `isolate=never` giữ hàng đợi khoá cũ; chống vướng ref."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from cfharness.dispatch.integrate import Integrator, is_lockfile
from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.workspace import ensure_worktree_meta, ref_collision
from m3_fake import ScriptedRuntime, kind_of


def git(cwd, *args, check=True) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "ws" / "app"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("line1\nline2\nline3\n")
    (r / "b.txt").write_text("b1\n")
    (r / "package-lock.json").write_text('{\n  "lockfileVersion": 3,\n  "v": "base"\n}\n')
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


async def settle(cond, timeout=15.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.03)
        if cond():
            return
    raise AssertionError("hết giờ")


def _plan(*subs):
    return {"summary": "song song", "subtasks": [{"key": k, "title": title, "body": title, "assignee": who, **extra}
                                                 for k, title, who, extra in subs]}


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(plan, work):
        """`work(title, spec) -> dict` = hành vi run task (ngoài run lead trả `plan`)."""
        def script(spec):
            if kind_of(spec) == "lead":
                return {"structured": plan, "text": "plan"}
            title = spec.prompt.split("## Task: ", 1)[1].splitlines()[0] if "## Task: " in spec.prompt else ""
            return work(title, spec) or {}

        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        h.start()
        h.agents.create({"id": "lead", "name": "Lead", "role": "planner"})
        for a in ("dev1", "dev2"):
            h.agents.create({"id": a, "name": a, "role": "coder"})
        h.teams.create({"id": "eng", "name": "Eng", "members": [{"agent_id": "lead", "role": "lead"}, {"agent_id": "dev1", "role": "worker"},
                                                                {"agent_id": "dev2", "role": "worker"}]})
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def _events(h, etype):
    return [e for e in h.store.query_events() if e.type == etype]


def _root(h, repo, title="Two edits"):
    return h.tasks.create({"team": "eng", "title": title, "body": "x", "workspace": {"kind": "dir", "path": str(repo)}})


# ============================================================================ ghép sạch
async def test_two_isolated_siblings_merge_cleanly_and_root_leaves_branch(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {}), ("b", "Edit B", "dev2", {}))
    files = {"Edit A": {"a.txt": "line1\nA\nline3\n"}, "Edit B": {"b.txt": "B\n", "new/c.txt": "c\n"}}
    h, rt = make_hub(plan, lambda title, spec: {"files": files.get(title, {}), "hold": True})
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    work = [s for s in rt.specs.values() if kind_of(s) == "task"]
    # cả hai chạy CÙNG LÚC, mỗi run một worktree riêng (không phải checkout chính)
    cwds = {str(Path(s.cwd).resolve()) for s in work}
    assert len(cwds) == 2 and str(repo.resolve()) not in cwds
    subs = {s["title"]: h.tasks.row(s["id"]) for s in h.tasks.subtasks(rid)}
    for title, s in subs.items():
        ws = s["workspace"]
        assert ws["worktree"]["branch"] == f"cfh/{rid}/{s['id']}" and ws["worktree"]["base"] == f"cfh/{rid}/integration"
        assert ws["isolation"]["root"] == rid and ws["isolation"]["reason"] in ("conflict", "integration")
        assert "# Workspace rules" in rt.specs[s["current_run_id"]].system_prompt and f"cfh/{rid}/{s['id']}" in rt.specs[s["current_run_id"]].system_prompt
    integ = h.tasks.row(rid)["workspace"]["integration"]
    assert integ["branch"] == f"cfh/{rid}/integration" and integ["base"] == "main" and Path(integ["path"]).is_dir()
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    r = h.tasks.row(rid)
    ib = f"cfh/{rid}/integration"
    # nhánh tích hợp có việc của cả hai; checkout chính không bị đụng
    assert git(repo, "show", f"{ib}:a.txt") == "line1\nA\nline3" and git(repo, "show", f"{ib}:b.txt") == "B"
    assert git(repo, "show", f"{ib}:new/c.txt") == "c"
    assert (repo / "a.txt").read_text() == "line1\nline2\nline3\n" and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s", "main") == "init"  # base không đổi (không push, không fast-forward)
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "main", ib]).returncode == 0
    # kết quả task gốc báo nhánh; worktree con đã gỡ, nhánh con (đã ghép) đã xoá; worktree tích hợp còn
    res = r["result_json"]["integration"]
    assert res["branch"] == ib and set(res["merged"]) == {s["id"] for s in subs.values()} and not res["fast_forward"]["done"]
    assert ib in r["result_summary"] and "KHÔNG push" in r["result_summary"]
    for s in subs.values():
        assert not Path(s["workspace"]["worktree"]["path"]).exists()
        assert git(repo, "branch", "--list", f"cfh/{rid}/{s['id']}") == ""
    assert Path(res["path"]).is_dir()
    assert len(_events(h, "task.isolated")) == 2 and {e.payload["how"] for e in _events(h, "task.integrated")} == {"clean"}
    assert h.runs.locks.list() == {}  # khoá tích hợp đã nhả
    # worktree tích hợp hiện trong `worktree list` (dọn được khi nhánh đã merge vào base)
    from cfharness import housekeeping as HK

    it = [x for x in HK.worktree_list(h.store.conn)["items"] if Path(x["path"]) == Path(res["path"])][0]
    assert not it["cleanable"] and "chưa merge" in it["reason"]


async def test_uncommitted_and_committed_child_work_both_merged(make_hub, repo):
    """Task con tự commit một phần, để phần còn lại chưa commit → hub auto-commit phần dư rồi ghép cả hai."""
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))

    def work(title, spec):
        cwd = Path(spec.cwd)
        (cwd / "b.txt").write_text("committed\n")
        git(cwd, "commit", "-qam", "agent commit")
        return {"files": {"a.txt": "dirty\n"}}

    h, rt = make_hub(plan, work)
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    ib = f"cfh/{root['id']}/integration"
    assert git(repo, "show", f"{ib}:a.txt") == "dirty" and git(repo, "show", f"{ib}:b.txt") == "committed"
    sub = h.tasks.subtasks(root["id"])[0]
    assert sub["workspace"]["isolation"]["reason"] == "requested"
    log = git(repo, "log", "--format=%s", ib)
    assert f"cfh: task {sub['id']}" in log and f"cfh: ghép task {sub['id']}" in log


# ============================================================================ xung đột thật → task merge
async def test_same_line_conflict_creates_merge_task_that_resolves_it(make_hub, repo):
    plan = _plan(("a", "Edit A one", "dev1", {"isolate": True}), ("b", "Edit A two", "dev2", {"isolate": True}))
    seen = {}

    def work(title, spec):
        if title.startswith("Merge: "):
            cwd = Path(spec.cwd)
            seen["merge_cwd"] = str(cwd.resolve())
            seen["merge_prompt"] = spec.prompt
            br = next(ln.split("`")[1] for ln in spec.prompt.splitlines() if ln.startswith("- Branch to merge:"))
            git(cwd, "merge", "--no-ff", br, check=False)
            (cwd / "a.txt").write_text("line1\nONE+TWO\nline3\n")
            git(cwd, "add", "a.txt")
            git(cwd, "commit", "-q", "--no-edit")
            return {}
        return {"files": {"a.txt": f"line1\n{'ONE' if title.endswith('one') else 'TWO'}\nline3\n"}, "hold": True}

    h, rt = make_hub(plan, work)
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: any(s["title"].startswith("Merge: ") for s in h.tasks.subtasks(rid)))
    mt = next(s for s in h.tasks.subtasks(rid) if s["title"].startswith("Merge: "))
    integ = h.tasks.row(rid)["workspace"]["integration"]
    conflict = _events(h, "task.integration_conflict")[0].payload
    assert conflict["files"] == ["a.txt"] and conflict["merge_task_id"] == mt["id"]
    child = h.tasks.row(conflict["task_id"])
    assert mt["assignee_agent_id"] == child["assignee_agent_id"] and mt["workspace"]["merge_for"] == child["id"]
    assert Path(mt["workspace"]["path"]) == Path(integ["path"])
    assert "a.txt" in mt["body"] and conflict["branch"] in mt["body"] and integ["branch"] in mt["body"]
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    assert seen["merge_cwd"] == str(Path(integ["path"]).resolve())
    ib = integ["branch"]
    assert git(repo, "show", f"{ib}:a.txt") == "line1\nONE+TWO\nline3"
    merged = h.tasks.row(rid)["workspace"]["integration"]["merged"]
    assert {m["how"] for m in merged} == {"clean", "merge_task"}
    assert git(Path(integ["path"]), "status", "--porcelain") == ""


async def test_merge_task_that_does_not_merge_blocks_root_until_human_merges(make_hub, repo):
    plan = _plan(("a", "Edit A one", "dev1", {"isolate": True}), ("b", "Edit A two", "dev2", {"isolate": True}))

    def work(title, spec):
        if title.startswith("Merge: "):
            return {}  # agent báo xong mà không ghép
        return {"files": {"a.txt": f"line1\n{'ONE' if title.endswith('one') else 'TWO'}\nline3\n"}, "hold": True}

    h, rt = make_hub(plan, work)
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "blocked")
    r = h.tasks.row(rid)
    integ = r["workspace"]["integration"]
    bad = [c for c in integ["conflicts"] if c["status"] == "failed"][0]
    assert bad["branch"] in r["block_reason"] and "unblock" in r["block_reason"] and integ["path"] in r["block_reason"]
    # người ghép tay trong worktree tích hợp rồi gỡ chặn → hub kiểm lại, task gốc xong
    ip = Path(integ["path"])
    git(ip, "merge", "--no-ff", bad["branch"], check=False)
    (ip / "a.txt").write_text("line1\nHUMAN\nline3\n")
    git(ip, "add", "a.txt")
    git(ip, "commit", "-q", "--no-edit")
    h.tasks.unblock(rid)
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    assert "manual" in {m["how"] for m in h.tasks.row(rid)["workspace"]["integration"]["merged"]}


# ============================================================================ xung đột tầm thường
async def test_lockfile_only_conflict_auto_resolved_and_flagged(make_hub, repo):
    plan = _plan(("a", "Dep A", "dev1", {"isolate": True}), ("b", "Dep B", "dev2", {"isolate": True}))
    files = {"Dep A": {"package-lock.json": '{\n  "lockfileVersion": 3,\n  "v": "A"\n}\n', "a.txt": "line1\nA\nline3\n"},
             "Dep B": {"package-lock.json": '{\n  "lockfileVersion": 3,\n  "v": "B"\n}\n', "b.txt": "B\n"}}
    h, rt = make_hub(plan, lambda title, spec: {"files": files[title], "hold": True})
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    r = h.tasks.row(rid)
    integ = r["workspace"]["integration"]
    assert not any(s["title"].startswith("Merge: ") for s in h.tasks.subtasks(rid))
    assert integ["needs_lockfile_refresh"] == ["package-lock.json"]
    assert {m["how"] for m in integ["merged"]} == {"clean", "lockfile"}
    ib = integ["branch"]
    assert git(repo, "show", f"{ib}:a.txt") == "line1\nA\nline3" and git(repo, "show", f"{ib}:b.txt") == "B"
    first = next(m for m in integ["merged"] if m["how"] == "clean")
    first_title = h.tasks.row(first["task"])["title"]
    assert f'"v": "{first_title[-1]}"' in git(repo, "show", f"{ib}:package-lock.json")  # lấy bản của nhánh tích hợp
    assert r["result_json"]["integration"]["needs_lockfile_refresh"] == ["package-lock.json"] and "npm install" in r["result_summary"]
    assert [e.payload["needs_lockfile_refresh"] for e in _events(h, "task.integrated") if e.payload["how"] == "lockfile"] == [["package-lock.json"]]


async def test_whitespace_only_conflict_retried_with_ignore_space_change(make_hub, repo):
    plan = _plan(("a", "Indent", "dev1", {"isolate": True}), ("b", "Edit", "dev2", {"isolate": True}))
    files = {"Indent": {"a.txt": "line1\nline2  \nline3\n"}, "Edit": {"a.txt": "line1\nline2 \nline3\n", "b.txt": "B\n"}}
    h, rt = make_hub(plan, lambda title, spec: {"files": files[title], "hold": True})
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    assert {m["how"] for m in h.tasks.row(rid)["workspace"]["integration"]["merged"]} == {"clean", "whitespace"}


# ============================================================================ fast-forward (tuỳ chọn)
async def test_fast_forward_only_when_enabled_and_main_clean_on_base(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"a.txt": "ff\n"}})
    h.config.set("workspace.integrate.fast_forward", True)
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    r = h.tasks.row(root["id"])
    assert r["result_json"]["integration"]["fast_forward"] == {"done": True}
    assert (repo / "a.txt").read_text() == "ff\n" and git(repo, "rev-parse", "main") == git(repo, "rev-parse", f"cfh/{root['id']}/integration")
    assert "fast-forward" in r["result_summary"]


async def test_fast_forward_skipped_when_main_checkout_dirty(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"b.txt": "ff\n"}})
    h.config.set("workspace.integrate.fast_forward", True)
    (repo / "a.txt").write_text("human wip\n")
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    ff = h.tasks.row(root["id"])["result_json"]["integration"]["fast_forward"]
    assert ff["done"] is False and "chưa commit" in ff["reason"]
    assert (repo / "a.txt").read_text() == "human wip\n" and (repo / "b.txt").read_text() == "b1\n"


# ============================================================================ never / ghi đè team / task
async def test_isolate_never_keeps_old_queueing(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}), ("b", "Edit B", "dev2", {}))
    h, rt = make_hub(plan, lambda title, spec: {"hold": True})
    h.config.set("workspace.isolate", "never")
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 1
                 and any(s["wait_reason"] == "workspace_locked" for s in h.tasks.subtasks(rid)))
    for s in h.tasks.subtasks(rid):
        assert "worktree" not in s["workspace"] and "isolation" not in s["workspace"]
    assert "integration" not in h.tasks.row(rid)["workspace"]
    assert git(repo, "branch", "--list", "cfh/*") == ""
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "done" or (rt.release() and False))
    assert not _events(h, "task.isolated")


async def test_team_policy_and_task_opt_out_override(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {})
    h.teams.update("eng", {"policies": {"isolate": "never"}})
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    assert "worktree" not in h.tasks.subtasks(root["id"])[0]["workspace"]
    integ = Integrator(h)
    t = {"id": "t_x", "kind": "task", "team_id": "eng", "workspace": {"kind": "dir", "path": str(repo)}}
    assert integ.mode(t) == "never"
    h.teams.update("eng", {"policies": {"isolate": "always"}})
    assert integ.mode(t) == "always"
    with pytest.raises(ValueError):
        h.config.set("workspace.isolate", "sometimes")


# ============================================================================ vướng ref
def test_ref_collision_guard_and_prefix_fallback(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        git(repo, "branch", "cfh/t_root1")  # nhánh đường B cũ `cfh/<task id>` trùng id task gốc
        assert ref_collision(str(repo), "cfh/t_root1/integration") == "refs/heads/cfh/t_root1"
        assert ref_collision(str(repo), "cfh/t_root2/integration") is None
        integ = Integrator(h)
        assert integ._prefix(str(repo.resolve()), "t_root1") == "cfh/t_root1-int"
        assert integ._prefix(str(repo.resolve()), "t_root2") == "cfh/t_root2"
        with pytest.raises(HubError, match="vướng ref"):
            ensure_worktree_meta(h, str(repo), "cfh/t_root1/integration")
        # chiều ngược: đã có `cfh/x/…` thì đường B cũ không tạo được `cfh/x`
        git(repo, "branch", "cfh/x/integration")
        assert ref_collision(str(repo), "cfh/x") == "refs/heads/cfh/x/integration"
        with pytest.raises(HubError, match="vướng ref"):
            ensure_worktree_meta(h, str(repo), "cfh/x")
        assert is_lockfile("web/package-lock.json") and not is_lockfile("src/lock.ts")
    finally:
        h.store.close()


async def test_isolation_uses_fallback_prefix_when_root_branch_exists(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"a.txt": "x\n"}})
    h.agents.set_paused("lead", True)
    root = _root(h, repo)
    rid = root["id"]
    git(repo, "branch", f"cfh/{rid}")  # vd task đường B cũ cùng tên
    h.agents.set_paused("lead", False)
    h.dispatcher.kick()
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    assert h.tasks.row(rid)["result_json"]["integration"]["branch"] == f"cfh/{rid}-int/integration"
    assert git(repo, "show", f"cfh/{rid}-int/integration:a.txt") == "x"


def test_internal_workspace_keys_not_accepted_from_outside(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    h.config.set("tasks.tick_s", 3600)
    try:
        h.agents.create({"id": "c1", "name": "c1"})
        t = h.tasks.create({"assignee": "c1", "title": "x", "body": "y",
                            "workspace": {"kind": "dir", "path": str(repo), "merge_for": "t_evil", "isolation": {"root": "t_r"},
                                          "integration": {"path": "/"}}})
        assert set(h.tasks.row(t["id"])["workspace"]) == {"kind", "path"}
    finally:
        h.store.close()


# ============================================================================ review: khoá, lỗi git, ghép tay, cô lập thất bại
def _spy_locks(h):
    calls = []
    orig = h.runs.locks.try_acquire_many

    def spy(keys, session_id, run_id, mode=None):
        keys = list(keys)
        calls.append((keys, session_id, mode))
        return orig(keys, session_id, run_id, mode=mode)

    h.runs.locks.try_acquire_many = spy
    return calls


async def test_integration_and_fast_forward_take_exclusive_locks_in_files_mode(make_hub, repo):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"a.txt": "ff\n"}})
    h.config.set("guard.repo_lock", "files")
    h.config.set("workspace.integrate.fast_forward", True)
    calls = _spy_locks(h)
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    mine = [c for c in calls if c[1] == f"integrate:{root['id']}"]
    assert mine and all(c[2] == "exclusive" for c in mine)
    assert any(os.path.realpath(repo) in c[0] for c in mine)  # fast-forward giữ khoá checkout chính
    assert h.tasks.row(root["id"])["result_json"]["integration"]["fast_forward"] == {"done": True}
    assert h.runs.locks.list() == {} and h.runs.locks.list_shared() == {}


async def test_fast_forward_skipped_when_main_checkout_locked(make_hub, repo):
    import uuid

    from cfharness.store import now_iso

    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"b.txt": "ff\n"}})
    h.config.set("workspace.integrate.fast_forward", True)
    rid_run = "r_" + uuid.uuid4().hex[:10]
    h.store.insert_run({"id": rid_run, "agent_id": "dev2", "kind": "adhoc", "prompt": "x", "session_id": "sess-main",
                        "status": "waiting_human", "queued_at": now_iso()})
    assert h.runs.locks.try_acquire(os.path.realpath(repo), "sess-main", rid_run, mode="exclusive")
    root = _root(h, repo)
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    ff = h.tasks.row(root["id"])["result_json"]["integration"]["fast_forward"]
    assert ff["done"] is False and "khoá" in ff["reason"] and rid_run in ff["reason"]
    assert (repo / "b.txt").read_text() == "b1\n"
    assert h.runs.locks.holder(os.path.realpath(repo))["session_id"] == "sess-main"  # khoá người khác còn nguyên


async def test_git_timeout_during_merge_blocks_root_and_aborts_merge(make_hub, repo, monkeypatch):
    plan = _plan(("a", "Edit A one", "dev1", {"isolate": True}), ("b", "Edit A two", "dev2", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"a.txt": f"line1\n{title}\nline3\n"}, "hold": True})
    orig = Integrator._do_merge
    n = {"calls": 0}

    def flaky(self, ip, branch, msg, *extra):
        n["calls"] += 1
        res = orig(self, ip, branch, msg, *extra)
        if n["calls"] >= 2:  # lần ghép thứ hai: để lại MERGE_HEAD rồi "git treo"
            raise subprocess.TimeoutExpired(["git", "merge"], 60)
        return res

    monkeypatch.setattr(Integrator, "_do_merge", flaky)
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "blocked")
    r = h.tasks.row(rid)
    integ = r["workspace"]["integration"]
    failed = [c for c in integ["conflicts"] if c["status"] == "failed"]
    assert len(failed) == 1 and "TimeoutExpired" in failed[0]["reason"] and "TimeoutExpired" in r["block_reason"]
    ip = Path(integ["path"])
    assert subprocess.run(["git", "-C", str(ip), "rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode != 0  # đã merge --abort
    assert h.runs.locks.list() == {}
    # tick tiếp không ném lỗi, task gốc vẫn blocked rõ ràng
    h.dispatcher.tick()
    assert h.tasks.row(rid)["status"] == "blocked"


async def test_manual_merge_in_progress_is_never_auto_committed(make_hub, repo):
    plan = _plan(("a", "Edit A one", "dev1", {"isolate": True}), ("b", "Edit A two", "dev2", {"isolate": True}))

    def work(title, spec):
        if title.startswith("Merge: "):
            return {}  # agent báo xong mà không ghép
        return {"files": {"a.txt": f"line1\n{'ONE' if title.endswith('one') else 'TWO'}\nline3\n"}, "hold": True}

    h, rt = make_hub(plan, work)
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: len([s for s in rt.specs.values() if kind_of(s) == "task"]) == 2)
    rt.release()
    await settle(lambda: h.tasks.row(rid)["status"] == "blocked")
    integ = h.tasks.row(rid)["workspace"]["integration"]
    bad = [c for c in integ["conflicts"] if c["status"] == "failed"][0]
    ip = Path(integ["path"])
    head0 = git(ip, "rev-parse", "HEAD")
    # người đang ghép: đã giải + stage nhưng CHƯA commit → hub không được commit hộ
    git(ip, "merge", "--no-ff", bad["branch"], check=False)
    (ip / "a.txt").write_text("line1\nHUMAN\nline3\n")
    git(ip, "add", "a.txt")
    for _ in range(3):
        h.dispatcher.tick()
    assert git(ip, "rev-parse", "HEAD") == head0 and h.tasks.row(rid)["status"] == "blocked"
    h.tasks.unblock(rid)  # gỡ chặn khi merge còn dở → chặn lại, nói rõ
    await settle(lambda: h.tasks.row(rid)["status"] == "blocked")
    assert "dở" in h.tasks.row(rid)["block_reason"]
    assert git(ip, "rev-parse", "HEAD") == head0
    assert subprocess.run(["git", "-C", str(ip), "rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode == 0
    # người commit → còn blocked tới khi gỡ chặn; gỡ chặn → hub kiểm lại, task gốc xong
    git(ip, "commit", "-q", "--no-edit")
    h.dispatcher.tick()
    assert h.tasks.row(rid)["status"] == "blocked"
    h.tasks.unblock(rid)
    await settle(lambda: h.tasks.row(rid)["status"] == "done")
    assert "manual" in {m["how"] for m in h.tasks.row(rid)["workspace"]["integration"]["merged"]}


async def test_isolation_failure_blocks_requested_task_instead_of_main_checkout(make_hub, repo, monkeypatch):
    plan = _plan(("a", "Edit A", "dev1", {"isolate": True}))
    h, rt = make_hub(plan, lambda title, spec: {"files": {"a.txt": "x\n"}})

    def boom(self, t, reason):
        raise HubError("git worktree add thất bại: giả lập")

    monkeypatch.setattr(Integrator, "isolate", boom)
    root = _root(h, repo)
    rid = root["id"]
    await settle(lambda: any(s["status"] == "blocked" for s in h.tasks.subtasks(rid)))
    sub = h.tasks.subtasks(rid)[0]
    assert "worktree" in sub["block_reason"] and "giả lập" in sub["block_reason"]
    assert not [s for s in rt.specs.values() if kind_of(s) == "task"]  # không chạy trên checkout chính
    await settle(lambda: h.tasks.row(rid)["status"] == "blocked")


def test_ensure_integration_prunes_stale_worktree_before_recreating(hub_home, repo):
    import shutil

    from cfharness.workspace import registered_worktree

    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    h.config.set("tasks.tick_s", 3600)
    try:
        h.agents.create({"id": "c1", "name": "c1"})
        t = h.tasks.create({"assignee": "c1", "title": "x", "body": "y", "workspace": {"kind": "dir", "path": str(repo)}})
        integ = Integrator(h)
        st = integ.ensure_integration(h.tasks.row(t["id"]), str(repo.resolve()), {"kind": "dir", "path": str(repo)})
        p = Path(st["path"])
        shutil.rmtree(p)  # người xoá tay thư mục (git vẫn ghi worktree đó)
        st2 = integ.ensure_integration(h.tasks.row(t["id"]), str(repo.resolve()), {"kind": "dir", "path": str(repo)})
        assert Path(st2["path"]).is_dir() and git(st2["path"], "rev-parse", "--abbrev-ref", "HEAD") == st["branch"]
        assert registered_worktree(h.store, st2["path"])["branch_created"] is True  # dựng lại giữ "hub tạo nhánh"
    finally:
        h.store.close()
