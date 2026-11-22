"""Workspace song song — luồng E "Khoá theo file" (docs/dev-notes/workspace-parallel.md §E, B7).

`guard.repo_lock: files`: run giữ khoá repo CHIA SẺ; Edit/Write… lấy lease theo file trong `GuardService.check` (trước lối tắt trust);
Bash đổi trạng thái chung cần độc quyền; diff/revert của run giới hạn theo file đã lease. Chế độ `repo` (mặc định) giữ nguyên hành vi.
Không gọi claude: Hub in-process, run giả chèn thẳng vào DB.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from cfharness.errors import Conflict
from cfharness.guard import leases as L
from cfharness.guard.locks import RepoLocks, normalize_repo_lock, repo_lock_mode
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.store import now_iso
from cfharness.workspace import compute_diff, revert, take_checkpoint


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    for n in ("a.txt", "b.txt", "c.txt"):
        (r / n).write_text(n + "1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("guard.repo_lock", "files")
    for a in ("a1", "a2", "a3"):
        h.agents.create({"id": a, "name": a.upper()})
    yield h
    rt.release()
    h.store.close()


def key(repo: Path) -> str:
    return str(repo.resolve())


def start(hub, agent, session, repo, status="running"):
    """Run giả đang chạy của `session` trong `repo` + khoá repo (chia sẻ ở chế độ files) như tick/_run lấy."""
    rid = "r_" + uuid.uuid4().hex[:12]
    hub.store.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "x", "session_id": session, "status": status,
                          "queued_at": now_iso(), "cwd": str(repo)})
    assert hub.runs.locks.try_acquire_many([key(repo)], session, rid)
    return hub.store.get_run(rid)


def end(hub, run, status="completed"):
    hub.store.update_run(run["id"], {"status": status, "ended_at": now_iso()})


async def check(hub, run, tool, ti, repo):
    return await asyncio.wait_for(hub.guard.check({"run_id": run["id"], "tool_name": tool, "tool_input": ti, "channel": "hook",
                                                   "cwd": str(repo)}), 20)


def edit(path: Path) -> dict:
    return {"file_path": str(path), "old_string": "x", "new_string": "y"}


def post(hub, run, tool, path=None):
    """PostToolUse qua RPC thật của hook quan sát."""
    from cfharness.mcp_server.surface_rpc import SurfaceRpc

    data = {"tool_name": tool, "session_id": run["session_id"]}
    if path is not None:
        data["file_path"] = str(path)
    s = SurfaceRpc.__new__(SurfaceRpc)
    s.hub = hub
    assert s.hook_observe({"run_id": run["id"], "event": "PostToolUse", "data": data})["ok"]


def lease_events(hub):
    return [e for e in hub.store.query_events() if e.type.startswith("workspace.lease_")]


# ============================================================================ cấu hình
def test_repo_lock_mode_normalisation(hub):
    assert [normalize_repo_lock(v) for v in (True, None, False, "repo", "files", "off", "FILES", "true", "false", "bogus")] == \
        ["repo", "repo", "off", "repo", "files", "off", "files", "repo", "off", "repo"]
    assert repo_lock_mode(hub.config) == "files" and hub.runs.locks.default_mode() == "shared"
    from cfharness.config import validate_setting

    for ok in (True, False, "repo", "files", "off"):
        validate_setting("guard.repo_lock", ok)
    with pytest.raises(ValueError):
        validate_setting("guard.repo_lock", "bogus")
    hub.config.set("guard.repo_lock", "off")
    ag = hub.store.get_agent("a1")
    assert hub.runs.lock_keys({"cwd": "/x", "run_opts": {}}, ag) == (None, [])
    hub.config.set("guard.repo_lock", True)
    assert hub.runs.locks.default_mode() == "exclusive"


def test_shared_and_exclusive_lock_semantics(tmp_path):
    a = str(tmp_path / "r")
    (tmp_path / "r" / ".git").mkdir(parents=True)
    locks = RepoLocks(_Store())
    assert locks.try_acquire(a, "s1", "r1", mode="shared") and locks.try_acquire(a, "s2", "r2", mode="shared")
    assert set(locks.shared_holders(a)) == {"s1", "s2"} and locks.list() == {}
    assert not locks.blockers([a], "s3", mode="shared")
    assert locks.blockers([a], "s3", mode="exclusive")  # chia sẻ chặn độc quyền
    assert not locks.try_acquire(a, "s3", "r3", mode="exclusive")
    locks.release_session("s1")
    locks.release_many([a], "s2")
    assert locks.list_shared() == {}
    assert locks.try_acquire(a, "s3", "r3", mode="exclusive")
    assert not locks.try_acquire(a, "s4", "r4", mode="shared")  # độc quyền chặn chia sẻ
    out = locks.force_release(a)
    assert out["session_id"] == "s3" and locks.list() == {}


class _Store:
    def __init__(self):
        self.d = {}

    def state_get(self, k, default=None):
        return self.d.get(k, default)

    def state_set(self, k, v):
        self.d[k] = v


def test_files_mode_runs_share_repo_lock_and_queue_does_not_block(hub, repo):
    r1 = start(hub, "a1", "s1", repo)
    r2 = start(hub, "a2", "s2", repo)  # lấy được: khoá chia sẻ
    assert set(hub.runs.locks.shared_holders(key(repo))) == {"s1", "s2"}
    assert not hub.runs.locks.blockers([key(repo)], "s3")  # dispatcher (_lock_block) không chặn
    assert hub.runs.lock_pending([key(repo)], "s3") is False
    assert hub.runs._lost_locks(r1) == [] and hub.runs._lost_locks(r2) == []


# ============================================================================ Edit/Write: lease
async def test_two_sessions_edit_different_files_concurrently(hub, repo):
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    d1 = await check(hub, r1, "Edit", edit(repo / "a.txt"), repo)
    d2 = await check(hub, r2, "Write", {"file_path": str(repo / "b.txt"), "content": "z"}, repo)
    assert d1["decision"] == "allow" and d2["decision"] == "allow", (d1, d2)
    rows = {(x["rel"], x["session_id"]) for x in hub.runs.locks.leases.list()}
    assert rows == {("a.txt", "s1"), ("b.txt", "s2")}
    assert {e.type for e in lease_events(hub)} == {"workspace.lease_acquired"}


async def test_same_file_second_session_denied_with_holder(hub, repo):
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    d = await check(hub, r2, "MultiEdit", {"file_path": str(repo / "a.txt"), "edits": []}, repo)
    assert d["decision"] == "deny" and d["lease_denied"] == "file_lease_held"
    assert "a1" in d["reason"] and r1["id"] in d["reason"] and "worktree" in d["reason"]
    # đường dẫn tương đối + khác hoa/thường (Windows) vẫn là CÙNG file
    rel = "A.TXT" if L.IS_WINDOWS else "a.txt"
    d = await check(hub, r2, "Edit", {"file_path": rel, "old_string": "x", "new_string": "y"}, repo)
    assert d["decision"] == "deny"
    ev = [e for e in lease_events(hub) if e.type == "workspace.lease_denied"]
    assert ev and ev[-1].payload["holder_session_id"] == "s1" and ev[-1].payload["path"] in ("a.txt", "A.TXT") and ev[-1].run_id == r2["id"]
    row = hub.store.conn.execute("SELECT action, matched_rule FROM guard_decisions WHERE run_id=? ORDER BY id DESC LIMIT 1", (r2["id"],)).fetchone()
    assert tuple(row) == ("deny", "file_lease_held")
    # session giữ lease vẫn ghi tiếp được
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"


async def test_trust_full_agent_still_subject_to_leases(hub, repo):
    hub.guard.trust_set("a2", "full")
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    d = await check(hub, r2, "Edit", edit(repo / "a.txt"), repo)
    assert d["decision"] == "deny" and d["lease_denied"] == "file_lease_held"
    assert (await check(hub, r2, "Edit", edit(repo / "b.txt"), repo)).get("decided_by") == "trust"


async def test_external_modification_detected_then_reread_allows(hub, repo):
    r1 = start(hub, "a1", "s1", repo)
    f = repo / "a.txt"
    assert (await check(hub, r1, "Edit", edit(f), repo))["decision"] == "allow"
    # PostToolUse chưa về (pending) → thay đổi coi là của chính session (hook quan sát fail-open không chặn nhầm)
    f.write_text("mine\n")
    assert (await check(hub, r1, "Edit", edit(f), repo))["decision"] == "allow"
    f.write_text("mine2\n")
    post(hub, r1, "Edit", f)  # hash sau khi ghi được ghi nhận
    assert (await check(hub, r1, "Edit", edit(f), repo))["decision"] == "allow"
    post(hub, r1, "Edit", f)
    f.write_text("someone else\n")  # người/agent khác sửa ngoài lease
    d = await check(hub, r1, "Edit", edit(f), repo)
    assert d["decision"] == "deny" and d["lease_denied"] == "file_changed_externally" and "Read the file again" in d["reason"]
    assert (await check(hub, r1, "Edit", edit(f), repo))["decision"] == "allow"  # đã báo đọc lại → lần sau qua


async def test_file_outside_shared_repo_not_leased(hub, repo, tmp_path):
    r1 = start(hub, "a1", "s1", repo)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert L.check(hub, r1, "Write", {"file_path": str(other / "x.txt")}, str(repo)) is None
    assert hub.runs.locks.leases.list() == []


# ============================================================================ Bash
async def test_exclusive_bash_denied_while_others_work(hub, repo):
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    assert (await check(hub, r2, "Edit", edit(repo / "b.txt"), repo))["decision"] == "allow"
    for cmd in ("git checkout -b feat", "git stash", "git add -A", "git commit -am x", "npm install", "git pull"):
        d = await check(hub, r1, "Bash", {"command": cmd}, repo)
        assert d["decision"] == "deny" and d["lease_denied"] == "repo_exclusive_needed", cmd
        assert "a2" in d["reason"] and "b.txt" in d["reason"]
    # chỉ giữ khoá chia sẻ (chưa lease file nào) cũng chặn
    r3 = start(hub, "a3", "s3", repo)
    end(hub, r2)
    hub.runs.locks.sweep()
    assert hub.runs.locks.leases.rows(session_id="s2") == []  # lease của session chết nhả cùng khoá
    d = await check(hub, r1, "Bash", {"command": "git switch main"}, repo)
    assert d["decision"] == "deny" and "a3" in d["reason"]
    end(hub, r3)
    # một mình → được, đặt dấu độc quyền; Edit của session khác bị "repo bận" tới khi PostToolUse của Bash về
    assert L.check(hub, r1, "Bash", {"command": "git checkout -b feat"}, str(repo)) is None
    r4 = start(hub, "a2", "s4", repo)
    d = await check(hub, r4, "Edit", edit(repo / "c.txt"), repo)
    assert d["decision"] == "deny" and d["lease_denied"] == "repo_exclusive_busy" and "git checkout" in d["reason"]
    post(hub, r1, "Bash")
    assert (await check(hub, r4, "Edit", edit(repo / "c.txt"), repo))["decision"] == "allow"


async def test_explicit_path_git_ops(hub, repo):
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    assert (await check(hub, r2, "Edit", edit(repo / "b.txt"), repo))["decision"] == "allow"
    # file chính mình lease → được (lease không chặn; guard thường vẫn phân loại)
    assert L.check(hub, r1, "Bash", {"command": "git add a.txt && git commit -m x -- a.txt"}, str(repo)) is None
    # file session khác lease → deny
    d = await check(hub, r1, "Bash", {"command": "git add b.txt"}, repo)
    assert d["decision"] == "deny" and d["lease_denied"] == "file_lease_held" and "b.txt" in d["reason"]
    assert L.check(hub, r1, "Bash", {"command": "git restore b.txt"}, str(repo))["rule"] == "file_lease_held"
    # `git commit` không đường dẫn commit cả index: index đang có file session khác lease → deny
    (repo / "b.txt").write_text("b2\n")
    git(repo, "add", "b.txt")
    hit = L.check(hub, r1, "Bash", {"command": "git commit -m mine"}, str(repo))
    assert hit and hit["rule"] == "file_lease_held" and "--" in hit["message"]
    git(repo, "reset", "-q", "b.txt")
    assert L.check(hub, r1, "Bash", {"command": "git commit -m mine"}, str(repo)) is None


# ============================================================================ chế độ repo giữ nguyên
async def test_repo_mode_unchanged(hub, repo):
    hub.config.set("guard.repo_lock", "repo")
    r1 = start(hub, "a1", "s1", repo)
    assert hub.runs.locks.holder(key(repo))["session_id"] == "s1" and hub.runs.locks.list_shared() == {}
    assert not hub.runs.locks.try_acquire_many([key(repo)], "s2", "rX")  # độc quyền như cũ
    assert hub.runs.locks.blockers([key(repo)], "s2")
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    assert L.check(hub, r1, "Bash", {"command": "git checkout -b x"}, str(repo)) is None
    assert hub.runs.locks.leases.list() == [] and lease_events(hub) == []


# ============================================================================ vòng đời + quét
async def test_leases_released_with_repo_lock_and_swept_by_liveness(hub, repo):
    r1, r2 = start(hub, "a1", "s1", repo), start(hub, "a2", "s2", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    # người giữ chết (run kết thúc mà không nhả) → session khác lấy được lease ngay
    end(hub, r1, "failed")
    assert (await check(hub, r2, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    assert {(x["rel"], x["session_id"]) for x in hub.runs.locks.leases.list()} == {("a.txt", "s2")}
    # đường nhả khoá của luồng A (hết run / stop / recovery) nhả luôn lease
    hub.runs.locks.release_many([key(repo)], "s2")
    assert hub.runs.locks.leases.list() == []
    r3 = start(hub, "a3", "s3", repo)
    assert (await check(hub, r3, "Edit", edit(repo / "b.txt"), repo))["decision"] == "allow"
    hub.runs.locks.release_session("s3", reason="cancelled")
    assert hub.runs.locks.leases.list() == []
    assert any(e.type == "workspace.lease_released" for e in lease_events(hub))
    # gỡ tay khoá chia sẻ (workspace.unlock) → gỡ mọi người giữ + lease của họ
    r4 = start(hub, "a1", "s4", repo, status="waiting_human")
    assert (await check(hub, start(hub, "a2", "s5", repo), "Edit", edit(repo / "c.txt"), repo))["decision"] == "allow"
    end(hub, hub.store.conn.execute("SELECT * FROM runs WHERE session_id='s5'").fetchone())
    out = hub.runs.unlock(path=str(repo))
    assert out["key"] == key(repo) and hub.runs.locks.list_shared() == {} and hub.runs.locks.leases.list() == []
    assert r4


async def test_workspace_leases_rpc_and_cli(hub, repo, capsys):
    from cfharness.dispatch.cli import cmd_workspace
    from cfharness.dispatch.rpc import OrchestrationRpc

    r1 = start(hub, "a1", "s1", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"

    class _D:
        pass

    d = _D()
    d.hub, d.methods = hub, {}
    m = OrchestrationRpc(d).methods()  # type: ignore[arg-type]
    out = m["workspace.leases"]({})
    assert out["mode"] == "files" and key(repo) in out["shared"] and [x["rel"] for x in out["leases"]] == ["a.txt"]
    assert m["workspace.locks"]({}) == {}  # giao diện cũ: chỉ khoá độc quyền

    class _Ctx:
        json = False
        args = type("A", (), {"action": "locks"})()

        def call(self, method, params):
            return m[method](params)

    assert cmd_workspace(_Ctx()) == 0
    txt = capsys.readouterr().out
    assert "chế độ khoá: files" in txt and "[chia sẻ]" in txt and "a.txt" in txt and "agent a1" in txt


# ============================================================================ B7: diff/revert theo file đã lease
def test_diff_and_revert_scoped_to_leased_files(repo, tmp_path):
    cp = take_checkpoint(repo, tmp_path / "cp")
    (repo / "a.txt").write_text("a2\n")  # của run
    (repo / "b.txt").write_text("b2\n")  # của session khác
    full = compute_diff(cp, tmp_path / "cp")
    assert {f["path"] for f in full["files"]} == {"a.txt", "b.txt"} and "scope" not in full
    d = compute_diff(cp, tmp_path / "cp", paths=["A.txt" if L.IS_WINDOWS else "a.txt"])
    assert [f["path"] for f in d["files"]] == ["a.txt"] and d["scope"] == "leases"
    with pytest.raises(Conflict) as ei:
        revert(d, tmp_path / "cp", leased_by_others=["a.txt"])
    assert ei.value.data["conflicts"] == ["a.txt"]
    res = revert(d, tmp_path / "cp", leased_by_others=["b.txt"])
    assert res["reverted"] == ["a.txt"]
    assert (repo / "a.txt").read_text() == "a.txt1\n" and (repo / "b.txt").read_text() == "b2\n"  # file người khác KHÔNG bị đụng
    # revert(paths=…) lọc tiếp trên diff đầy đủ
    (repo / "a.txt").write_text("a3\n")
    full = compute_diff(cp, tmp_path / "cp2")
    assert revert(full, tmp_path / "cp2", paths=["b.txt"], dry_run=True)["reverted"] == ["b.txt"]


async def test_run_diff_scope_uses_session_leases(hub, repo, tmp_path):
    from cfharness.runs import ActiveRun

    r1 = start(hub, "a1", "s1", repo)
    assert (await check(hub, r1, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"
    ar = ActiveRun(r1["id"], "a1")
    ar.lock_key = key(repo)
    ar.checkpoint = take_checkpoint(repo, hub.runs.checkpoint_dir(r1["id"]))
    (repo / "a.txt").write_text("a2\n")
    (repo / "b.txt").write_text("b2\n")
    assert hub.runs._lease_scope(ar) == ["a.txt"]
    await hub.runs._diff(ar)
    diff = hub.store.get_run(r1["id"])["diff"]
    assert [f["path"] for f in diff["files"]] == ["a.txt"] and diff["scope"] == "leases"
    # `run revert`: file của run đang bị session khác lease → Conflict; nhả xong → chỉ hoàn tác a.txt, b.txt (của người khác) giữ nguyên
    from cfharness.dispatch.rpc import OrchestrationRpc

    class _D:
        pass

    dd = _D()
    dd.hub, dd.methods = hub, {}
    rpc = OrchestrationRpc(dd).methods()  # type: ignore[arg-type]
    hub.store.update_run(r1["id"], {"workspace_key": key(repo)})
    end(hub, r1)
    r2 = start(hub, "a2", "s2", repo)
    assert (await check(hub, r2, "Edit", edit(repo / "a.txt"), repo))["decision"] == "allow"  # người giữ cũ đã chết → lấy được
    with pytest.raises(Conflict):
        rpc["run.revert"]({"id": r1["id"]})
    hub.runs.locks.release_session("s2")
    end(hub, r2)
    out = rpc["run.revert"]({"id": r1["id"]})
    assert out["reverted"] == ["a.txt"] and (repo / "a.txt").read_text() == "a.txt1\n" and (repo / "b.txt").read_text() == "b2\n"
    hub.config.set("guard.repo_lock", "repo")
    assert hub.runs._lease_scope(ar) is None


# ============================================================================ hook quan sát gửi đường dẫn (không nội dung)
def test_observe_hook_sends_file_path_for_writes(monkeypatch):
    import json

    from cfharness import observe_hook

    sent = []
    monkeypatch.setattr(observe_hook, "_send", lambda sock, msg, t, wait_reply: sent.append(msg) or {"result": {"ok": True}})
    stdin = json.dumps({"session_id": "s", "tool_name": "Edit", "cwd": "/w", "tool_input": {"file_path": "/w/a.py", "new_string": "SECRET"},
                        "tool_response": {"x": 1}})
    assert observe_hook.observe(["PostToolUse", "--run", "r1", "--socket", "/tmp/x.sock"], stdin)["ok"]
    data = sent[0]["params"]["data"]
    assert data == {"session_id": "s", "tool_name": "Edit", "file_path": "/w/a.py", "cwd": "/w"}
    sent.clear()
    observe_hook.observe(["PostToolUse", "--run", "r1", "--socket", "/tmp/x.sock"], json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert "file_path" not in sent[0]["params"]["data"] and "command" not in json.dumps(sent[0])
