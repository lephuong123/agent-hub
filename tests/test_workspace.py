"""FR-RT-12: khoá theo repo, checkpoint trước run, diff sau run, `run revert` (repo git tạm), thư mục không phải git."""

import asyncio
import subprocess

import pytest

from cfharness.errors import Conflict, HubError
from cfharness.hub import Hub
from cfharness.workspace import WorkspaceLocks, compute_diff, git_root, revert, take_checkpoint
from m3_fake import ScriptedRuntime


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a1\n")
    (r / "b.txt").write_text("b1\n")
    (r / "d.txt").write_text("d1\n")
    (r / "sub").mkdir()
    (r / "sub" / "s.txt").write_text("s1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


async def settle(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond():
            return
    raise AssertionError("hết giờ")


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script):
        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


# ============================================================================ hàm thuần
def test_lock_key_is_git_root(repo, tmp_path):
    assert git_root(repo / "sub") == str(repo.resolve())
    assert WorkspaceLocks.key_for(str(repo / "sub"), "dir") == str(repo.resolve())
    plain = tmp_path / "plain"
    plain.mkdir()
    assert WorkspaceLocks.key_for(str(plain), "dir") == str(plain.resolve())
    assert WorkspaceLocks.key_for(str(plain), "scratch") is None


def test_checkpoint_diff_revert_keeps_user_changes(repo, tmp_path):
    (repo / "a.txt").write_text("a1\nuser edit\n")  # thay đổi chưa commit của NGƯỜI
    (repo / "notes.txt").write_text("user untracked\n")
    store = tmp_path / "cp"
    cp = take_checkpoint(repo, store)
    assert cp["kind"] == "git" and cp["branch"] == "main" and cp["head"]
    assert set(cp["dirty"]) == {"a.txt", "notes.txt"}
    status_before = git(repo, "status", "--porcelain")
    # run sửa 3 file: sửa b, tạo c, xoá d
    (repo / "b.txt").write_text("b1\nrun edit\n")
    (repo / "c.txt").write_text("new by run\n")
    (repo / "d.txt").unlink()
    diff = compute_diff(cp, store)
    files = {f["path"]: f["status"] for f in diff["files"]}
    assert files == {"b.txt": "modified", "c.txt": "added", "d.txt": "deleted"}
    assert diff["revertible"] and diff["stat"]["files"] == 3
    patch = (store / "diff.patch").read_text()
    assert "+run edit" in patch and "-d1" in patch
    res = revert(diff, store)
    assert sorted(res["reverted"]) == ["b.txt", "c.txt", "d.txt"]
    assert (repo / "b.txt").read_text() == "b1\n" and not (repo / "c.txt").exists() and (repo / "d.txt").read_text() == "d1\n"
    assert (repo / "a.txt").read_text() == "a1\nuser edit\n" and (repo / "notes.txt").exists()  # file của người không đổi
    assert git(repo, "status", "--porcelain") == status_before
    assert git(repo, "stash", "list") == ""  # không stash, không commit


def test_run_edits_user_dirty_file_reverts_to_user_version(repo, tmp_path):
    (repo / "a.txt").write_text("user wip\n")
    store = tmp_path / "cp"
    cp = take_checkpoint(repo, store)
    (repo / "a.txt").write_text("user wip\nrun line\n")
    diff = compute_diff(cp, store)
    assert [f["path"] for f in diff["files"]] == ["a.txt"]
    revert(diff, store)
    assert (repo / "a.txt").read_text() == "user wip\n"  # về bản của người, không phải HEAD


def test_revert_conflict_when_modified_after_run(repo, tmp_path):
    store = tmp_path / "cp"
    cp = take_checkpoint(repo, store)
    (repo / "b.txt").write_text("run\n")
    (repo / "c.txt").write_text("run new\n")
    diff = compute_diff(cp, store)
    (repo / "b.txt").write_text("run\nhuman touched later\n")
    with pytest.raises(Conflict) as e:
        revert(diff, store)
    assert e.value.data["conflicts"] == ["b.txt"]
    assert (repo / "c.txt").exists() and "human touched" in (repo / "b.txt").read_text()  # không ghi đè gì


def test_run_commit_detected(repo, tmp_path):
    store = tmp_path / "cp"
    cp = take_checkpoint(repo, store)
    (repo / "b.txt").write_text("committed by run\n")
    git(repo, "commit", "-qam", "run commit")
    diff = compute_diff(cp, store)
    assert diff["commits_made"] and [f["path"] for f in diff["files"]] == ["b.txt"]
    res = revert(diff, store)
    assert (repo / "b.txt").read_text() == "b1\n" and res["warnings"]


def test_non_git_dir_lists_files_no_revert(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "x.txt").write_text("x")
    store = tmp_path / "cp"
    cp = take_checkpoint(d, store)
    assert cp["kind"] == "dir"
    (d / "y.txt").write_text("y")
    (d / "x.txt").write_text("xx")
    diff = compute_diff(cp, store)
    assert {f["path"]: f["status"] for f in diff["files"]} == {"x.txt": "modified", "y.txt": "added"}
    with pytest.raises(HubError):
        revert(diff, store)


# ============================================================================ tích hợp với RunManager / dispatcher
async def test_task_run_checkpoint_diff_and_rpc_revert(make_hub, repo):
    hub, rt = make_hub(lambda spec: {"files": {"b.txt": "b1\nagent\n", "new.txt": "hello\n", "d.txt": None}, "text": "edited"})
    (repo / "a.txt").write_text("user wip\n")
    hub.agents.create({"id": "coder", "name": "Coder"})
    t = hub.tasks.create({"assignee": "coder", "title": "edit", "body": "edit files", "workspace": str(repo)})
    assert t["warnings"] == []
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    run = hub.store.list_runs()[0]
    assert run["workspace_key"] == str(repo.resolve()) and run["checkpoint"]["head"] and run["checkpoint"]["branch"] == "main"
    assert {f["path"] for f in run["diff"]["files"]} == {"b.txt", "new.txt", "d.txt"}
    assert hub.runs.locks.list() == {}  # khoá đã nhả
    from cfharness.dispatch.rpc import OrchestrationRpc

    class _D:
        pass

    d = _D()
    d.hub, d.methods = hub, {}
    rpc = OrchestrationRpc(d)  # type: ignore[arg-type]
    dry = rpc.run_revert({"id": run["id"], "dry_run": True})
    assert sorted(dry["reverted"]) == ["b.txt", "d.txt", "new.txt"] and (repo / "new.txt").exists()
    res = rpc.run_revert({"id": run["id"]})
    assert sorted(res["reverted"]) == ["b.txt", "d.txt", "new.txt"]
    assert (repo / "b.txt").read_text() == "b1\n" and (repo / "d.txt").exists() and not (repo / "new.txt").exists()
    assert (repo / "a.txt").read_text() == "user wip\n"
    with pytest.raises(Conflict):
        rpc.run_revert({"id": run["id"]})  # đã revert
    assert any(e.type == "run.reverted" for e in hub.store.query_events())


async def test_two_tasks_same_repo_serialized_and_readonly_not_locked(make_hub, repo):
    hub, rt = make_hub(lambda spec: {"hold": True})
    hub.agents.create({"id": "c1", "name": "C1"})
    hub.agents.create({"id": "c2", "name": "C2"})
    hub.agents.create({"id": "ro", "name": "RO", "tool_policy": {"permission_mode": "plan"}})
    t1 = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
    t2 = hub.tasks.create({"assignee": "c2", "title": "b", "body": "b", "workspace": str(repo / "sub")})  # cùng repo
    t3 = hub.tasks.create({"assignee": "ro", "title": "read", "body": "r", "workspace": str(repo)})
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "running" and hub.tasks.row(t3["id"])["status"] == "running")
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "workspace_locked")
    row2 = hub.tasks.row(t2["id"])
    assert row2["status"] == "ready"
    # H-21: task thứ hai KHÔNG có run nào trong lúc chờ khoá, không bị tính lỗi (khoá giữ đồng bộ trong tick)
    assert hub.store.conn.execute("SELECT count(*) FROM runs WHERE task_id=?", (t2["id"],)).fetchone()[0] == 0
    assert int(row2.get("consecutive_failures") or 0) == 0 and int(row2.get("attempts") or 0) == 0
    assert list(hub.runs.locks.list()) == [str(repo.resolve())]
    rt.release(hub.tasks.row(t1["id"])["current_run_id"])
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "running")
    rt.release()
    await settle(lambda: all(hub.tasks.row(t["id"])["status"] == "done" for t in (t1, t2, t3)) or (rt.release() and False))


async def test_lock_held_through_waiting_human(make_hub, repo):
    """Q-21: run waiting_human giữ khoá repo — task khác cùng repo phải chờ."""
    hub, rt = make_hub(lambda spec: {"hold": True})
    hub.agents.create({"id": "c1", "name": "C1"})
    hub.agents.create({"id": "c2", "name": "C2"})
    t1 = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "running" and hub.runs.active)
    rid = hub.tasks.row(t1["id"])["current_run_id"]
    await settle(lambda: hub.store.get_run(rid)["status"] == "running")
    ap = hub.approvals.open(hub.store.get_run(rid), tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_use_id=None, fingerprint="fp",
                            verdict={"verdict": "destructive", "rule": "test"}, channel="hook")
    hub.runs.active[rid].pause_approval_id = ap["id"]
    await hub.runs.active[rid].runtime.stop(hub.runs.active[rid].handle)
    await settle(lambda: hub.store.get_run(rid)["status"] == "waiting_human")
    t2 = hub.tasks.create({"assignee": "c2", "title": "b", "body": "b", "workspace": str(repo)})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "workspace_locked")
    assert hub.tasks.row(t1["id"])["status"] == "running"  # lease task giữ trong lúc chờ người


def test_non_git_workspace_warns(hub_home, tmp_path):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "a", "name": "A"})
        h.agents.set_paused("a", True)
        d = tmp_path / "plain"
        d.mkdir()
        t = h.tasks.create({"assignee": "a", "title": "x", "body": "x", "workspace": str(d)})
        assert t["warnings"] and "không phải repo git" in t["warnings"][0]
    finally:
        h.store.close()


async def test_interrupted_run_not_resumed_when_human_edited_files(make_hub, repo):
    """FR-RT-19 + FR-RT-12: run gián đoạn, người sửa file của run → task blocked, không tự chạy lại."""
    hub, rt = make_hub(lambda spec: {"hold": True, "files": {"b.txt": "agent edit\n"}})
    hub.agents.create({"id": "c1", "name": "C1"})
    t = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "running" and hub.runs.active)
    rid = hub.tasks.row(t["id"])["current_run_id"]
    await settle(lambda: hub.store.get_run(rid)["status"] == "running" and rid in hub.runs.active)
    hub.runs.shutting_down = True  # như daemon dừng: run → interrupted (diff vẫn được tính), task chờ recovery
    hub.runs.active[rid].task.cancel()
    await settle(lambda: hub.store.get_run(rid)["status"] == "interrupted" and hub.store.get_run(rid).get("diff"))
    (repo / "b.txt").write_text("human edit after interruption\n")
    hub.runs.shutting_down = False
    from cfharness.recovery import resume_interrupted

    assert resume_interrupted(hub, hub.store.get_run(rid)) is None  # recovery không tự resume
    hub.dispatcher.on_run_terminal(rid)
    row = hub.tasks.row(t["id"])
    assert row["status"] == "blocked" and "b.txt" in row["block_reason"]
