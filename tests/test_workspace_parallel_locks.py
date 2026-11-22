"""Workspace song song — luồng A "Khoá đúng & gỡ được" (docs/dev-notes/workspace-parallel.md, B1–B5).

B1 một hàm tính khoá cho run/task (kind `worktree` → thư mục worktree của `claude --worktree`, không khoá repo chính);
B2 checkpoint/diff trên thư mục worktree + cấm bypassPermissions khi cwd là checkout chính; B3 gỡ khoá của session không còn
run sống / quá `guard.lock_waiting_ttl_s` / recovery không resume, RPC+CLI `workspace.unlock`, event khoá; B4 nhiều khoá
nguyên tử (cwd + extra_write_roots); B5 thư mục không phải git xung đột với cha/con.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfharness.errors import Conflict, NotFound
from cfharness.guard.locks import RepoLocks
from cfharness.hub import Hub
from cfharness.oscompat import IS_WINDOWS
from cfharness.runtime import ArgvError, RunSpec, build_argv
from cfharness.store import now_iso
from cfharness.workspace import WorkspaceLocks, claude_worktree_dir, is_main_checkout, task_lock_target
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
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


async def settle(cond, timeout=8.0):
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

    def _make(script=lambda spec: {}, tick_s=0.05):
        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", tick_s)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def _live_run(hub, agent, session, status="queued", **kw):
    """Run giả của `session` (để người giữ khoá được coi là còn sống)."""
    rid = "r_" + uuid.uuid4().hex[:12]
    hub.store.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "x", "session_id": session, "status": status,
                          "queued_at": now_iso(), **kw})
    return rid


def _legacy_worktree_task(hub, assignee, repo, name):
    """Task ĐÃ LƯU với kind `worktree` cũ (cwd = repo, `claude --worktree <name>`): từ luồng B `tasks.create` đổi kind này sang worktree
    do hub tạo, nên task cũ trong DB được dựng bằng cách ghi thẳng workspace sau khi tạo (đồng bộ, trước khi dispatcher kịp tick)."""
    t = hub.tasks.create({"assignee": assignee, "title": f"legacy {name}", "body": "x", "workspace": "scratch"})
    hub.tasks.update(t["id"], {"workspace": {"kind": "worktree", "path": str(Path(repo).resolve()), "name": name}})
    return hub.tasks.row(t["id"])


class _Store:
    def __init__(self):
        self.d = {}

    def state_get(self, k, default=None):
        return self.d.get(k, default)

    def state_set(self, k, v):
        self.d[k] = v


def _events(hub, prefix="workspace.lock_"):
    return [e for e in hub.store.query_events() if e.type.startswith(prefix)]


# ============================================================================ B1: một hàm tính khoá
def test_b1_worktree_kind_key_is_worktree_dir_not_repo(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        ag = h.store.get_agent("c1")
        t = {"id": "t_x", "workspace": {"kind": "worktree", "path": str(repo), "name": "feat-x"}}
        primary, keys = h.runs.lock_keys(task_lock_target(t), ag)
        want = os.path.realpath(str(repo / ".claude" / "worktrees" / "feat-x"))
        assert primary == want and keys == [want] and claude_worktree_dir(str(repo), "feat-x") == want
        assert str(repo.resolve()) not in keys  # KHÔNG khoá repo chính
        # tên mặc định = id task (như dispatcher truyền --worktree)
        t2 = {"id": "t_y", "workspace": {"kind": "worktree", "path": str(repo)}}
        assert h.runs.lock_keys(task_lock_target(t2), ag)[1] == [os.path.realpath(str(repo / ".claude" / "worktrees" / "t_y"))]
        # run tương ứng (run_opts do dispatcher dựng) → CÙNG khoá
        run = {"cwd": str(repo), "run_opts": {"workspace_kind": "worktree", "worktree": "feat-x"}}
        assert h.runs.lock_keys(run, ag) == (want, [want]) and h.runs._lock_key(run, ag) == want
        # dir thường: khoá gốc repo; agent chỉ đọc → không khoá
        assert h.runs.lock_keys({"cwd": str(repo / "a.txt").rsplit(os.sep, 1)[0], "run_opts": {}}, ag)[1] == [str(repo.resolve())]
        h.agents.create({"id": "ro", "name": "RO", "tool_policy": {"permission_mode": "plan"}})
        assert h.runs.lock_keys(run, h.store.get_agent("ro")) == (None, [])
    finally:
        h.store.close()


def test_b1_dispatcher_and_same_lock_tasks_use_same_keys(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    h.config.set("tasks.tick_s", 3600)
    try:
        for a in ("c1", "c2", "c3"):
            h.agents.create({"id": a, "name": a})
        h.agents.set_paused("c1", True)
        h.agents.set_paused("c2", True)
        h.agents.set_paused("c3", True)
        wt = _legacy_worktree_task(h, "c1", repo, "w1")
        # task dir mới trên repo: KHÔNG bị coi là cùng khoá với task worktree (worktree có khoá riêng)
        d = h.tasks.create({"assignee": "c2", "title": "dir", "body": "x", "workspace": str(repo)})
        assert not any(wt["id"] in w for w in d["warnings"])
        d2 = h.tasks.create({"assignee": "c3", "title": "dir2", "body": "x", "workspace": str(repo)})
        assert any(d["id"] in w and "workspace_locked" in w for w in d2["warnings"])
        # repo chính bị session sống khác giữ: task worktree KHÔNG bị chặn, task dir bị chặn
        _live_run(h, "c2", "sess-a", status="running")
        assert h.runs.locks.try_acquire(str(repo.resolve()), "sess-a", "r_a")
        disp = h.dispatcher
        assert disp._lock_block(h.tasks.row(wt["id"]), "c1", None, False) is None
        assert disp._lock_block(h.tasks.row(d2["id"]), "c3", None, False) == "workspace_locked"
        # worktree đang bị giữ → task worktree cùng tên bị chặn
        _live_run(h, "c2", "sess-w", status="running")
        assert h.runs.locks.try_acquire(claude_worktree_dir(str(repo), "w1"), "sess-w", "r_w")
        assert disp._lock_block(h.tasks.row(wt["id"]), "c1", None, False) == "workspace_locked"
    finally:
        h.store.close()


async def test_b1_worktree_task_runs_in_parallel_with_repo_task(make_hub, repo):
    """Trước đây: dispatcher coi kind worktree không khoá nhưng RunManager khoá GỐC repo → task "running" mà run nằm hàng đợi."""
    hub, rt = make_hub(lambda spec: {"hold": True})
    hub.agents.create({"id": "c1", "name": "C1"})
    hub.agents.create({"id": "c2", "name": "C2"})
    t1 = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
    t2 = _legacy_worktree_task(hub, "c2", repo, "w2")
    await settle(lambda: all(hub.tasks.row(t["id"])["status"] == "running" for t in (t1, t2)))
    r1, r2 = hub.tasks.row(t1["id"])["current_run_id"], hub.tasks.row(t2["id"])["current_run_id"]
    await settle(lambda: r1 in rt.gates and r2 in rt.gates)  # CẢ HAI run đã spawn (không có run kẹt hàng đợi)
    held = hub.runs.locks.list()
    assert set(held) == {str(repo.resolve()), claude_worktree_dir(str(repo), "w2")}
    assert rt.specs[r2].worktree == "w2" and rt.specs[r2].cwd_main_checkout is True
    rt.release()
    await settle(lambda: all(hub.tasks.row(t["id"])["status"] == "done" for t in (t1, t2)) or (rt.release() and False))
    assert hub.runs.locks.list() == {}


# ============================================================================ B2: checkpoint worktree + bypass
def _spec(**kw):
    base = dict(run_id="r_1", agent_id="coder", prompt="x", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"))
    base.update(kw)
    return RunSpec(**base)


def test_b2_bypass_refused_when_worktree_kind_on_main_checkout():
    from cfharness.config import DEFAULTS

    cfg = {**DEFAULTS["runtime"]["claude"]}
    tp = {"permission_mode": "bypassPermissions", "allow_bypass": True}
    with pytest.raises(ArgvError, match="checkout chính"):
        build_argv(_spec(tool_policy=tp, workspace_kind="worktree", worktree="w", cwd_main_checkout=True), cfg)
    argv, _ = build_argv(_spec(tool_policy=tp, workspace_kind="worktree", worktree="w"), cfg)  # cwd là worktree liên kết
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    # người đặt trust full: không đổi (ngoài NFR-15)
    argv, _ = build_argv(_spec(tool_policy={"permission_mode": "bypassPermissions"}, workspace_kind="worktree", cwd_main_checkout=True,
                               trust_full=True), cfg)
    assert "bypassPermissions" in argv


def test_b2_is_main_checkout(repo, tmp_path):
    assert is_main_checkout(repo) is True
    wt = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "lk", str(wt))
    assert is_main_checkout(wt) is False
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_main_checkout(plain) is False


async def test_b2_checkpoint_and_diff_on_existing_worktree_dir(make_hub, repo):
    wt = Path(claude_worktree_dir(str(repo), "w3"))
    git(repo, "worktree", "add", "-q", "-b", "w3", str(wt))  # đã có từ lượt trước (resume/retry)
    rel = os.path.relpath(wt / "new.txt", repo)
    hub, rt = make_hub(lambda spec: {"files": {rel: "from agent\n"}})  # agent ghi trong worktree (cwd của tiến trình = repo)
    hub.agents.create({"id": "c1", "name": "C1"})
    t = _legacy_worktree_task(hub, "c1", repo, "w3")
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    run = hub.store.list_runs()[0]
    assert run["workspace_key"] == str(wt.resolve()) == run["checkpoint"]["root"]
    assert run["checkpoint"]["branch"] == "w3"
    assert [f["path"] for f in run["diff"]["files"]] == ["new.txt"] and run["diff"]["root"] == str(wt.resolve())
    assert git(repo, "status", "--porcelain", "--untracked-files=no").strip() == ""  # repo chính không bị đụng
    assert not (repo / "new.txt").exists()


async def test_b2_no_checkpoint_when_worktree_not_created_yet(make_hub, repo):
    hub, rt = make_hub(lambda spec: {})
    hub.agents.create({"id": "c1", "name": "C1"})
    t = _legacy_worktree_task(hub, "c1", repo, "w4")
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    run = hub.store.list_runs()[0]
    assert not run.get("checkpoint") and not run.get("diff")  # không chụp repo chính thay cho worktree


# ============================================================================ B3: dọn người giữ chết, TTL, recovery, unlock
def test_b3_sweep_releases_dead_holder_and_emits_event(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        key = str(repo.resolve())
        assert h.runs.locks.try_acquire(key, "dead-session", "r_dead")  # session không có run nào
        assert h.runs.locks.blocked_for(key, "other") is None  # bị dọn ngay khi có người hỏi
        assert h.runs.locks.list() == {}
        ev = _events(h)
        assert [e.type for e in ev] == ["workspace.lock_acquired", "workspace.lock_released"]
        assert ev[1].payload == {"key": key, "session_id": "dead-session", "run_id": "r_dead", "reason": "no_live_run"}
        # session còn run sống → giữ khoá; session khác bị chặn và có event lock_blocked (một lần)
        _live_run(h, "c1", "live", status="running")
        assert h.runs.locks.try_acquire(key, "live", "r_live")
        assert not h.runs.locks.try_acquire(key, "other", "r_o") and not h.runs.locks.try_acquire(key, "other", "r_o")
        blocked = [e for e in _events(h) if e.type == "workspace.lock_blocked"]
        assert len(blocked) == 1 and blocked[0].payload["key"] == key and blocked[0].payload["holder_session_id"] == "live"
    finally:
        h.store.close()


def test_b3_event_types_registered():
    from cfharness.events import EVENT_LEVELS, make_event

    for t in ("workspace.lock_acquired", "workspace.lock_released", "workspace.lock_blocked"):
        assert t in EVENT_LEVELS and make_event(t).level == EVENT_LEVELS[t]


def test_b3_waiting_human_ttl(hub_home, repo):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        key = str(repo.resolve())
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        _live_run(h, "c1", "wh", status="waiting_human", ended_at=old)
        assert h.config.get("guard.lock_waiting_ttl_s") == 14400
        h.runs.locks.try_acquire(key, "wh", "r_wh")
        h.config.set("guard.lock_waiting_ttl_s", None)  # tắt TTL → giữ khoá như cũ
        assert h.runs.locks.blocked_for(key, "other") is not None
        h.config.set("guard.lock_waiting_ttl_s", 14400)  # 5 giờ > 4 giờ → mất khoá
        assert h.runs.locks.blocked_for(key, "other") is None
        rel = [e for e in _events(h) if e.type == "workspace.lock_released"]
        assert rel and rel[-1].payload["reason"] == "waiting_ttl"
        # waiting_human mới (trong TTL) → vẫn giữ
        _live_run(h, "c1", "wh2", status="waiting_human", ended_at=now_iso())
        h.runs.locks.try_acquire(key, "wh2", "r_wh2")
        assert h.runs.locks.blocked_for(key, "other") is not None
    finally:
        h.store.close()


async def test_b3_resume_after_ttl_release_checks_conflicts(make_hub, repo):
    """waiting_human quá TTL mất khoá; người sửa file của run trong lúc đó → duyệt xong KHÔNG resume (resume_conflicts)."""
    hub, rt = make_hub(lambda spec: {"hold": True, "files": {"b.txt": "agent\n"}})
    hub.agents.create({"id": "c1", "name": "C1"})
    t = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "running" and hub.runs.active)
    rid = hub.tasks.row(t["id"])["current_run_id"]
    await settle(lambda: hub.store.get_run(rid)["status"] == "running" and rid in rt.gates)
    ap = hub.approvals.open(hub.store.get_run(rid), tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_use_id=None, fingerprint="fp",
                            verdict={"verdict": "destructive", "rule": "test"}, channel="hook")
    hub.runs.active[rid].pause_approval_id = ap["id"]
    await hub.runs.active[rid].runtime.stop(hub.runs.active[rid].handle)
    await settle(lambda: hub.store.get_run(rid)["status"] == "waiting_human" and rid not in hub.runs.active)
    assert str(repo.resolve()) in hub.runs.locks.list()
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    hub.store.update_run(rid, {"ended_at": old})
    assert hub.runs.locks.sweep() == [str(repo.resolve())]
    (repo / "b.txt").write_text("human edit while waiting\n")
    hub.approvals.resolve(ap["id"], "allow")
    await settle(lambda: hub.store.get_run(rid)["status"] == "failed")
    r = hub.store.get_run(rid)
    assert "b.txt" in r["error"] and not r.get("continued_in_run_id")


async def test_b3_recovery_releases_locks_of_runs_not_resumed(hub_home, repo):
    h1 = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    h1.agents.create({"id": "a", "name": "A"})
    sid = str(uuid.uuid4())
    rid = _live_run(h1, "a", sid, status="running", cwd=str(repo), started_at=now_iso())
    h1.runs.locks.try_acquire(str(repo.resolve()), sid, rid)  # daemon chết khi run còn giữ khoá
    h1.config.set("recovery.auto_resume", False)
    h1.store.close()
    h2 = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h2.start()
        assert h2.store.get_run(rid)["status"] == "interrupted"
        assert h2.runs.locks.list() == {}
        rec = [e for e in h2.store.query_events(types=["recovery.reconciled"])]
        assert rec and rec[-1].payload.get("locks_released") == [str(repo.resolve())]
    finally:
        h2.runs.shutting_down = True
        h2.store.close()


def test_b3_unlock_service(hub_home, repo, tmp_path):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        key = str(repo.resolve())
        _live_run(h, "c1", "run-s", status="running")
        h.runs.locks.try_acquire(key, "run-s", "r_run")
        with pytest.raises(Conflict, match="--force"):
            h.runs.unlock(path=str(repo / "sub-not-needed").rsplit(os.sep, 1)[0])
        assert key in h.runs.locks.list()
        r = h.runs.unlock(path=str(repo), force=True)
        assert r["key"] == key and r["forced"] and r["released"]["session_id"] == "run-s" and h.runs.locks.list() == {}
        assert [e.payload["reason"] for e in _events(h) if e.type == "workspace.lock_released"] == ["manual_force"]
        # waiting_human giữ khoá → gỡ không cần force (khoá theo key, chữ hoa/thường khác nhau trên Windows)
        _live_run(h, "c1", "wait-s", status="waiting_human", ended_at=now_iso())
        h.runs.locks.try_acquire(key, "wait-s", "r_wait")
        r = h.runs.unlock(key=key.upper() if IS_WINDOWS else key)
        assert not r["forced"] and h.runs.locks.list() == {}
        with pytest.raises(NotFound):
            h.runs.unlock(path=str(tmp_path))
    finally:
        h.store.close()


def test_b3_unlock_rpc_registration_and_cli():
    from cfharness.auth import HUMAN_ONLY
    from cfharness.dispatch.cli import add_parsers
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI
    from cfharness.mcp_server.rpc import MUTATING
    import argparse

    assert "workspace.unlock" in HUMAN_ONLY and "workspace.unlock" in MUTATING
    assert "unlock" in (HUB_HUMAN_ONLY_CLI.get("workspace") or set())
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    add_parsers(sub, argparse.ArgumentParser(add_help=False))
    a = p.parse_args(["workspace", "unlock", "/x/y", "--force"])
    assert a.action == "unlock" and a.path == "/x/y" and a.force is True
    assert p.parse_args(["workspace", "unlock", "/x/y"]).force is False


def test_b3_unlock_rpc_method(hub_home, repo):
    from cfharness.dispatch.rpc import OrchestrationRpc

    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        _live_run(h, "c1", "s1", status="waiting_human", ended_at=now_iso())
        h.runs.locks.try_acquire(str(repo.resolve()), "s1", "r1")

        class _D:
            pass

        d = _D()
        d.hub, d.methods = h, {}
        rpc = OrchestrationRpc(d)  # type: ignore[arg-type]
        out = rpc.methods()["workspace.unlock"]({"path": str(repo)})
        assert out["key"] == str(repo.resolve()) and rpc.methods()["workspace.locks"]({}) == {}
    finally:
        h.store.close()


# ============================================================================ B4: nhiều khoá nguyên tử
def test_b4_try_acquire_many_is_atomic(tmp_path):
    a, b, c = (str(tmp_path / n) for n in ("a", "b", "c"))
    for p in (a, b, c):
        os.makedirs(p)
    locks = RepoLocks(_Store())
    assert locks.try_acquire(b, "s2", "r2")
    assert not locks.try_acquire_many([a, b], "s1", "r1")
    assert set(locks.list()) == {b}  # không lấy `a` khi `b` bị giữ
    assert locks.try_acquire_many([a, c], "s1", "r1") and set(locks.list()) == {a, b, c}
    assert locks.try_acquire_many([a, c], "s1", "r1b")  # cùng session → kế thừa
    locks.release_many([a, c], "s1")
    assert set(locks.list()) == {b}
    # giao diện cũ vẫn chạy
    assert locks.holder(b)["session_id"] == "s2" and locks.blocked_for(b, "s1")["session_id"] == "s2" and locks.blocked_for(b, "s2") is None
    locks.release(b, "s1")
    assert b in locks.list()
    locks.release_session("s2")
    assert locks.list() == {}


async def test_b4_run_holds_cwd_and_extra_write_roots(make_hub, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    hub, rt = make_hub(lambda spec: {"hold": True})
    for a in ("c1", "c2"):
        hub.agents.create({"id": a, "name": a})
    t1 = hub.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": {"kind": "dir", "path": str(repo), "extra_write_roots": [str(other)]}})
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "running" and hub.tasks.row(t1["id"])["current_run_id"] in rt.gates)
    rid = hub.tasks.row(t1["id"])["current_run_id"]
    assert set(hub.runs.locks.list()) == {str(repo.resolve()), str(other.resolve())}
    assert hub.runs.active[rid].lock_keys == [str(repo.resolve()), str(other.resolve())] and hub.runs.active[rid].lock_key == str(repo.resolve())
    # task khác ghi vào repo phụ → chờ khoá
    t2 = hub.tasks.create({"assignee": "c2", "title": "b", "body": "b", "workspace": str(other)})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "workspace_locked")
    rt.release(rid)
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "running")
    rt.release()
    await settle(lambda: all(hub.tasks.row(t["id"])["status"] == "done" for t in (t1, t2)) or (rt.release() and False))
    assert hub.runs.locks.list() == {}


# ============================================================================ B5: thư mục không phải git — cha/con
def test_b5_non_git_parent_child_conflict(tmp_path, repo):
    parent = tmp_path / "plain"
    child = parent / "sub"
    child.mkdir(parents=True)
    locks = RepoLocks(_Store())
    assert locks.try_acquire(str(parent), "s1", "r1")
    assert not locks.try_acquire(str(child), "s2", "r2")  # con của thư mục không-git đang bị khoá
    assert locks.blocked_for(str(child), "s2")["key"] == str(parent)
    locks.release_session("s1")
    assert locks.try_acquire(str(child), "s2", "r2")
    assert not locks.try_acquire(str(parent), "s1", "r1")  # cha chứa khoá con đang giữ
    locks.release_session("s2")
    # anh em không xung đột; tiền tố chuỗi không phải cha ("plain" vs "plainx")
    sib = tmp_path / "plainx"
    sib.mkdir()
    assert locks.try_acquire(str(parent), "s1", "r1") and locks.try_acquire(str(sib), "s2", "r2")
    # gốc repo git KHÔNG khoá worktree bên trong nó (B1)
    assert locks.try_acquire(str(repo), "s3", "r3")
    assert locks.try_acquire(claude_worktree_dir(str(repo), "w"), "s4", "r4")


def test_b5_case_insensitive_on_windows(tmp_path):
    d = tmp_path / "Plain"
    (d / "Sub").mkdir(parents=True)
    locks = RepoLocks(_Store())
    assert locks.try_acquire(str(d), "s1", "r1")
    other = str(d / "Sub").upper() if IS_WINDOWS else str(d / "Sub")
    assert not locks.try_acquire(other, "s2", "r2")
    same = str(d).lower() if IS_WINDOWS else str(d)
    assert not locks.try_acquire(same, "s2", "r2")
    if IS_WINDOWS:
        assert locks.holder(str(d).upper())["session_id"] == "s1"


def test_b5_workspace_locks_keys_for_non_git(hub_home, tmp_path):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {}))
    try:
        locks: WorkspaceLocks = h.runs.locks
        parent = tmp_path / "docs"
        (parent / "x").mkdir(parents=True)
        assert locks.keys_for(str(parent / "x"), "dir") == [str((parent / "x").resolve())]
        assert locks.conflicts(str(parent.resolve()), str((parent / "x").resolve()))
        assert locks.keys_for(None, "scratch", extra_write_roots=[str(parent)]) == [str(parent.resolve())]
        assert locks.keys_for(str(parent), "scratch") == []
    finally:
        h.store.close()
