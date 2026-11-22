"""Luồng B — vòng đời worktree (docs/dev-notes/workspace-parallel.md, lỗi B6): metadata worktree lưu vào task, slug không trùng,
cảnh báo core.longpaths, liên kết node_modules (junction Windows / symlink POSIX), gỡ worktree an toàn junction, dọn qua
housekeeping + RPC/CLI `worktree list|clean`, đổi đường B cũ (kind `worktree` + path=repo) sang worktree do hub tạo.

Repo git tạm; không gọi claude (ScriptedRuntime)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cfharness import auth
from cfharness import housekeeping as HK
from cfharness.errors import BLOCKED, Conflict, HubError
from cfharness.hub import Hub
from cfharness.workspace import (
    WORKTREE_SLUG_MAX,
    _is_link,
    ensure_worktree,
    ensure_worktree_meta,
    link_deps,
    longpaths_warning,
    remove_worktree,
    worktree_slug,
)
from m3_fake import ScriptedRuntime

AGENT = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="c1", evidence=["env_run_id"])
META_KEYS = {"repo", "branch", "base", "path", "owned", "created_at", "branch_created", "deps"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit_all(r: Path, msg: str) -> None:
    git(r, "add", "-A")
    git(r, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "ws" / "app"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("a\n")
    (r / ".gitignore").write_text("node_modules/\n")
    (r / "package.json").write_text('{"name": "app"}\n')
    (r / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    _commit_all(r, "init")
    nm = r / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1\n")
    return r


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    h.config.set("tasks.tick_s", 3600)  # không dispatch tự động
    h.start()
    for aid in ("c1", "c2"):
        h.agents.create({"id": aid, "name": aid, "role": "coder"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


# ============================================================================ slug
def test_slug_is_collision_free_and_bounded():
    a, b = worktree_slug("app", "a/b"), worktree_slug("app", "a-b")
    assert a != b and a.startswith("app-a-b-") and b.startswith("app-a-b-")
    long1, long2 = "feat/" + "x" * 60 + "/one", "feat/" + "x" * 60 + "/two"
    s1, s2 = worktree_slug("app", long1), worktree_slug("app", long2)
    assert s1 != s2
    mid = s1[len("app-"):-7]
    assert len(mid) <= WORKTREE_SLUG_MAX and not mid.endswith(("-", "."))
    u = worktree_slug("app", "tính-năng/ở đây")
    assert u.isascii() and "--" not in u
    assert worktree_slug("app", "a/b") == a  # ổn định


def test_default_paths_differ_for_colliding_branches(hub, repo):
    m1 = ensure_worktree_meta(hub, str(repo), "a/b")
    m2 = ensure_worktree_meta(hub, str(repo), "a-b")
    assert m1["path"] != m2["path"]
    assert Path(m1["path"]).parent == (repo.parent / "_wt").resolve()
    assert git(m1["path"], "rev-parse", "--abbrev-ref", "HEAD") == "a/b"
    assert git(m2["path"], "rev-parse", "--abbrev-ref", "HEAD") == "a-b"


# ============================================================================ metadata
def test_meta_shape_reuse_and_tuple_wrapper(hub, repo):
    m = ensure_worktree_meta(hub, str(repo), "feat/x")
    assert set(m) == META_KEYS
    assert m["repo"] == os.path.realpath(repo) and m["branch"] == "feat/x" and m["base"] == "main"
    assert m["owned"] is True and m["branch_created"] is True and m["created_at"]
    again = ensure_worktree_meta(hub, str(repo), "feat/x")
    assert again["path"] == m["path"] and again["owned"] is False and again["branch_created"] is False
    # bản cũ trả tuple (đường dẫn, có tạo mới không)
    assert ensure_worktree(hub, str(repo), "feat/x") == (m["path"], False)
    p, made = ensure_worktree(hub, str(repo), "feat/y", base="main")
    assert made and Path(p).is_dir()
    # nhánh đã có: base mặc định = nhánh hiện tại của repo chính, hub không tạo nhánh
    git(repo, "branch", "feat/old")
    old = ensure_worktree_meta(hub, str(repo), "feat/old")
    assert old["owned"] and not old["branch_created"] and old["base"] == "main"


def test_longpaths_warning_only_on_windows_without_setting(repo):
    assert longpaths_warning(repo, windows=False) is None
    git(repo, "config", "core.longpaths", "false")
    w = longpaths_warning(repo, windows=True)
    assert w and "core.longpaths" in w and "git config --global core.longpaths true" in w
    git(repo, "config", "core.longpaths", "true")
    assert longpaths_warning(repo, windows=True) is None
    assert git(repo, "config", "--get", "core.longpaths") == "true"  # hub chỉ đọc, không đổi


# ============================================================================ phụ thuộc
def test_link_deps_links_node_modules_when_lockfile_identical(hub, repo):
    m = ensure_worktree_meta(hub, str(repo), "feat/link")
    wt = Path(m["path"])
    d = m["deps"]
    assert d["mode"] == "link" and d["linked"] and not d["needs_install"] and d["lockfile"] == "package-lock.json"
    assert _is_link(wt / "node_modules") and (wt / "node_modules" / "left-pad" / "index.js").is_file()
    if os.name == "nt":
        assert os.path.isjunction(wt / "node_modules")
    assert git(wt, "status", "--porcelain") == ""  # liên kết bị git bỏ qua → worktree vẫn sạch


def test_link_deps_needs_install_when_lockfile_differs(hub, repo):
    git(repo, "branch", "old")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}\n')
    _commit_all(repo, "lock mới")
    m = ensure_worktree_meta(hub, str(repo), "feat/old-lock", base="old")
    assert m["deps"]["needs_install"] and not m["deps"]["linked"] and "package-lock.json" in m["deps"]["reason"]
    assert not (Path(m["path"]) / "node_modules").exists()


def test_link_deps_modes_and_no_node_modules(hub, repo, tmp_path):
    hub.config.set("workspace.deps", "install")
    m = ensure_worktree_meta(hub, str(repo), "feat/inst")
    assert m["deps"]["mode"] == "install" and m["deps"]["needs_install"] and not (Path(m["path"]) / "node_modules").exists()
    m2 = ensure_worktree_meta(hub, str(repo), "feat/none", deps_mode="none")
    assert m2["deps"] == {"mode": "none", "node": True, "linked": False, "needs_install": False}
    with pytest.raises(ValueError):
        hub.config.set("workspace.deps", "copy")
    # repo chính chưa có node_modules → cần cài
    plain = tmp_path / "wt-plain"
    plain.mkdir()
    (plain / "package.json").write_text('{"name": "app"}\n')
    (plain / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    main = tmp_path / "main-plain"
    main.mkdir()
    (main / "package.json").write_text('{"name": "app"}\n')
    (main / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    d = link_deps(plain, main, "link")
    assert d["needs_install"] and "chưa có node_modules" in d["reason"]
    assert link_deps(tmp_path, main, "link") == {"mode": "link", "node": False, "linked": False, "needs_install": False}


# ============================================================================ gỡ worktree an toàn junction
def test_remove_worktree_unlinks_junction_first_target_survives(hub, repo):
    m = ensure_worktree_meta(hub, str(repo), "feat/rm")
    wt = Path(m["path"])
    target = repo / "node_modules" / "left-pad" / "index.js"
    plan = remove_worktree(m, dry_run=True)
    assert plan["dry_run"] and not plan["removed"] and wt.is_dir() and _is_link(wt / "node_modules")
    assert any("gỡ liên kết" in a for a in plan["actions"]) and any("git worktree remove" in a and "--force" not in a for a in plan["actions"])
    res = remove_worktree(m)
    assert res["removed"] and not wt.exists()
    assert target.is_file() and target.read_text() == "module.exports = 1\n"  # node_modules của repo chính còn nguyên
    assert res["branch_merged"] == "base" and res["branch_deleted"]
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/heads/feat/rm"]).returncode != 0


def test_remove_worktree_refuses_dirty_unless_force(hub, repo):
    m = ensure_worktree_meta(hub, str(repo), "feat/dirty")
    wt = Path(m["path"])
    (wt / "new.txt").write_text("x")
    with pytest.raises(Conflict, match="chưa commit"):
        remove_worktree(m)
    assert wt.is_dir() and _is_link(wt / "node_modules")
    res = remove_worktree(m, force=True)
    assert res["removed"] and not wt.exists() and (repo / "node_modules" / "left-pad" / "index.js").is_file()


def test_remove_worktree_keeps_unmerged_and_foreign_branch(hub, repo):
    m = ensure_worktree_meta(hub, str(repo), "feat/work")
    wt = Path(m["path"])
    (wt / "b.txt").write_text("b\n")
    _commit_all(wt, "việc")
    res = remove_worktree(m)
    assert res["removed"] and not res["branch_deleted"] and "chưa merge" in res["branch_kept"]
    assert git(repo, "rev-parse", "--verify", "-q", "refs/heads/feat/work")
    # nhánh có sẵn (hub không tạo) → không bao giờ xoá nhánh
    git(repo, "branch", "mine")
    m2 = ensure_worktree_meta(hub, str(repo), "mine")
    res2 = remove_worktree(m2)
    assert res2["removed"] and "không do hub tạo" in res2["branch_kept"] and git(repo, "rev-parse", "--verify", "-q", "refs/heads/mine")
    with pytest.raises(HubError, match="checkout chính"):
        remove_worktree({**m, "path": str(repo)})


# ============================================================================ tasks.create
def test_task_stores_worktree_metadata(hub, repo):
    t = hub.tasks.create({"title": "C", "body": "x", "assignee": "c1", "workspace": {"kind": "dir", "path": str(repo)},
                          "worktree": {"branch": "feat/c"}})
    ws = t["workspace"]
    assert ws["kind"] == "dir" and ws["path"] == ws["worktree"]["path"] and set(ws["worktree"]) == META_KEYS
    assert ws["worktree"]["deps"]["linked"] and any("đã tạo worktree" in w for w in t["warnings"])
    row = json.loads(hub.store.conn.execute("SELECT workspace FROM tasks WHERE id=?", (t["id"],)).fetchone()[0])
    assert row["worktree"]["branch"] == "feat/c" and row["worktree"]["owned"] is True
    # task con kế thừa workspace (kèm metadata) → được nhận; metadata giả → từ chối
    sub = hub.tasks.create({"title": "sub", "body": "x", "assignee": "c2", "workspace": ws})
    assert sub["workspace"]["worktree"]["path"] == ws["path"]
    with pytest.raises(HubError, match="không khớp"):
        hub.tasks.create({"title": "giả", "body": "x", "assignee": "c2",
                          "workspace": {"kind": "dir", "path": ws["path"], "worktree": {**ws["worktree"], "path": str(repo)}}})
    with pytest.raises(HubError, match="không khớp"):
        hub.tasks.create({"title": "giả2", "body": "x", "assignee": "c2",
                          "workspace": {"kind": "dir", "path": str(repo), "worktree": {**ws["worktree"], "path": str(repo)}}})


def test_task_needs_install_warning(hub, repo):
    hub.config.set("workspace.deps", "install")
    t = hub.tasks.create({"title": "C", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/i"}})
    assert any("npm ci" in w for w in t["warnings"])


def test_old_path_b_converted_to_hub_worktree(hub, repo):
    t = hub.tasks.create({"title": "B", "body": "x", "assignee": "c1", "workspace": {"kind": "worktree", "path": str(repo), "name": "fix-1"}})
    ws = t["workspace"]
    assert ws["kind"] == "dir" and ws["worktree"]["branch"] == "cfh/fix-1" and ws["worktree"]["owned"]
    assert git(ws["path"], "rev-parse", "--abbrev-ref", "HEAD") == "cfh/fix-1"
    t2 = hub.tasks.create({"title": "B2", "body": "x", "assignee": "c2", "workspace": {"kind": "worktree", "path": str(repo)}})
    assert t2["workspace"]["worktree"]["branch"] == f"cfh/{t2['id']}"
    # dispatcher thấy workspace dir thường (không còn `claude --worktree`), khoá theo gốc worktree
    path, kind = hub.dispatcher._workspace(hub.tasks.row(t2["id"]))
    assert kind == "dir" and path == t2["workspace"]["path"]
    # kind worktree không có repo (scratch) giữ nguyên
    t3 = hub.tasks.create({"title": "B3", "body": "x", "assignee": "c1", "workspace": {"kind": "worktree"}})
    assert t3["workspace"] == {"kind": "worktree"}


def test_decompose_root_keeps_path_b(hub, repo):
    hub.teams.create({"id": "web", "name": "web", "members": [{"agent_id": "c1", "role": "lead"}, {"agent_id": "c2"}]})
    assert hub.teams.lead("web") == "c1"
    t = hub.tasks.create({"title": "R", "body": "x", "team": "web", "decompose": True, "workspace": {"kind": "worktree", "path": str(repo)}})
    assert t["kind"] == "root" and t["workspace"]["kind"] == "worktree"
    assert not (repo.parent / "_wt").exists()


def test_manager_check_path_accepts_stored_metadata(hub, repo):
    t = hub.tasks.create({"title": "C", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/m"}})
    from cfharness.manager.permissions import ManagerPermissions

    perms = ManagerPermissions(hub)
    assert perms.check_path(t["workspace"], "workspace của task") is None


# ============================================================================ housekeeping + RPC + CLI
def _finish(hub, tid: str, status: str = "done") -> None:
    hub.store.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    hub.store.conn.commit()


def test_housekeeping_lists_and_cleans_only_finished_clean_merged(hub, repo):
    a = hub.tasks.create({"title": "A", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/a"}})
    b = hub.tasks.create({"title": "B", "body": "x", "assignee": "c2", "worktree": {"repo": str(repo), "branch": "feat/b"}})
    c = hub.tasks.create({"title": "C", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/c"}})
    d = hub.tasks.create({"title": "D", "body": "x", "assignee": "c2", "worktree": {"repo": str(repo), "branch": "feat/d"}})
    pa, pb, pc, pd = (Path(x["workspace"]["path"]) for x in (a, b, c, d))
    _finish(hub, a["id"])
    _finish(hub, b["id"], "archived")  # huỷ
    (pb / "wip.txt").write_text("x")  # bẩn
    _finish(hub, c["id"])
    (pc / "c.txt").write_text("c\n")
    _commit_all(pc, "chưa merge")
    # d còn mở
    items = {Path(x["path"]): x for x in HK.worktree_list(hub.store.conn)["items"]}
    assert items[pa]["cleanable"] and items[pa]["merged"] == "base" and items[pa]["deps"]["linked"]
    assert not items[pb]["cleanable"] and "chưa commit" in items[pb]["reason"]
    assert not items[pc]["cleanable"] and "chưa merge" in items[pc]["reason"]
    assert not items[pd]["cleanable"] and d["id"] in items[pd]["reason"]
    # merge c vào main → dọn được
    git(repo, "merge", "-q", "--no-edit", "feat/c")
    assert {Path(x["path"]): x for x in HK.worktree_list(hub.store.conn)["items"]}[pc]["cleanable"]

    dry = HK.worktree_clean(hub.store.conn, dry_run=True)
    assert {Path(x["path"]) for x in dry["removed"]} == {pa, pc} and pa.is_dir() and pc.is_dir()
    res = HK.worktree_clean(hub.store.conn, [str(pa), str(pb)])
    assert [Path(x["path"]) for x in res["removed"]] == [pa] and not pa.exists() and pc.is_dir()
    assert [Path(x["path"]) for x in res["skipped"]] == [pb]
    assert (repo / "node_modules" / "left-pad" / "index.js").is_file()
    # scan của hub doctor chỉ báo worktree của task đã xong còn trên đĩa
    rep = HK.scan(hub.paths.home, hub.store.conn)
    assert {Path(x["path"]) for x in rep["task_worktrees"]} == {pb, pc}


def test_housekeeping_sees_legacy_path_b(hub, repo):
    # task cũ (trước luồng B) còn kind `worktree` + path=repo trong DB; claude --worktree tạo <repo>/.claude/worktrees/<id>
    tid = "t_legacy01"
    wt = repo / ".claude" / "worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", f"worktree-{tid}", str(wt))
    hub.store.conn.execute("INSERT INTO tasks (id, title, status, workspace, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (tid, "x", "done", json.dumps({"kind": "worktree", "path": str(repo)}), "2026-11-01T00:00:00.000Z",
                            "2026-11-01T00:00:00.000Z"))
    hub.store.conn.commit()
    it = [x for x in HK.worktree_list(hub.store.conn)["items"] if x["legacy"]][0]
    assert Path(it["path"]) == wt and it["branch"] == f"worktree-{tid}" and it["base"] == "main" and it["cleanable"]
    res = HK.worktree_clean(hub.store.conn)
    assert [Path(x["path"]) for x in res["removed"]] == [wt] and not wt.exists()


async def test_rpc_worktree_human_only_audited_and_cli_blocked(hub_home, repo):
    from m7_fake import close, make_daemon, rpc

    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    d = await make_daemon(hub_home)
    try:
        h = d.hub
        h.agents.create({"id": "c1", "name": "c1", "role": "coder"})
        t = h.tasks.create({"title": "A", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/r"}})
        h.store.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (t["id"],))
        h.store.conn.commit()
        for m in ("worktree.list", "worktree.clean"):
            with pytest.raises(HubError) as ei:
                await rpc(d, m, {}, as_caller=AGENT)
            assert ei.value.code == BLOCKED and m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED
        assert "worktree.clean" in auth.all_mutating() and "worktree.list" not in auth.all_mutating()
        lst = await rpc(d, "worktree.list", {})
        assert lst["cleanable"] == 1
        res = await rpc(d, "worktree.clean", {"dry_run": True})
        assert len(res["removed"]) == 1 and Path(t["workspace"]["path"]).is_dir()
        res = await rpc(d, "worktree.clean", {"paths": [t["workspace"]["path"]]})
        assert len(res["removed"]) == 1 and not Path(t["workspace"]["path"]).exists()
        rows = h.store.conn.execute("SELECT COUNT(*) FROM audit WHERE action='worktree.clean'").fetchone()[0] \
            if "audit" in {r[0] for r in h.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} else None
        if rows is not None:
            assert rows >= 2
        ctx = GuardContext(cwd=h.paths.home, policy=PolicyStore(h.paths.home).load())
        for cmd in ("cfharness worktree clean --yes", "cfharness worktree list", "uv run cfharness worktree clean --dry-run"):
            assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    finally:
        await close(d)


def test_cli_text_helpers():
    from cfharness.worktree_cli import list_text, plan_text

    assert "không có worktree" in list_text({"items": []})
    txt = list_text({"items": [{"path": "/x/_wt/app-a-1", "cleanable": False, "reason": "task còn mở: t_1", "branch": "a", "base": "main",
                                "tasks": [{"id": "t_1", "status": "running"}], "deps": {"needs_install": True}}], "cleanable": 0})
    assert "lý do: task còn mở" in txt and "cần npm ci" in txt
    assert "git worktree remove" in plan_text({"removed": [{"path": "/x", "branch": "a", "tasks": ["t_1"],
                                                           "actions": ["git worktree remove /x"]}], "skipped": []})
