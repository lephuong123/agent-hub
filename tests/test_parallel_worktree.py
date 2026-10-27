"""Task song song cùng repo: `worktree` của task (hub tạo worktree riêng làm workspace → khoá repo theo gốc worktree),
cảnh báo khi task mới dùng chung khoá repo với task đang mở, và dòng "task chờ" trên bảng agent của `cfharness chat`
(task `ready` kẹt `workspace_locked` mà agent nhận vẫn idle — trước đây bảng chỉ hiện "1 agent đang chạy")."""

import subprocess
from pathlib import Path

import pytest

from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.manager import agentsview as G
from cfharness.tui import rpc as tui_rpc
from cfharness.tui.chat import panel_head
from m3_fake import ScriptedRuntime


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "ws" / "app"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    git(r, "branch", "feat/base")
    return r


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    h.config.set("tasks.tick_s", 3600)  # không dispatch tự động: test gọi thẳng _lock_block
    h.start()
    for aid in ("c1", "c2", "c3"):
        h.agents.create({"id": aid, "name": aid, "role": "coder"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


def test_worktree_task_gets_own_lock_and_runs_in_parallel(hub, repo):
    a = hub.tasks.create({"title": "A", "body": "nền", "assignee": "c1", "workspace": {"kind": "dir", "path": str(repo)}})
    assert not any("khoá repo" in w for w in a["warnings"])
    wt_dir = repo.parent / "_wt" / "b"
    # cách cũ: repo làm workspace + worktree chỉ trong extra_write_roots → cảnh báo, và vẫn chờ khoá repo
    b_old = hub.tasks.create({"title": "B cũ", "body": "x", "assignee": "c2", "workspace": {"kind": "dir", "path": str(repo)},
                              "extra_write_roots": [str(wt_dir)]})
    assert any(a["id"] in w and "workspace_locked" in w for w in b_old["warnings"])
    # cách mới: hub tạo worktree từ base, worktree là workspace
    c = hub.tasks.create({"title": "C", "body": "x", "assignee": "c3", "workspace": {"kind": "dir", "path": str(repo)},
                          "worktree": {"branch": "feat/c", "base": "feat/base"}})
    wt = Path(c["workspace"]["path"])
    assert wt == (repo.parent / "_wt" / "app-feat-c").resolve() and (wt / "a.txt").exists()
    assert subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip() == "feat/c"
    assert not any("khoá repo" in w for w in c["warnings"]) and any("đã tạo worktree" in w for w in c["warnings"])

    key = hub.runs.locks.key_for(str(repo), "dir")
    assert hub.runs.locks.try_acquire(key, "sess-a", "r_a")  # task A đang chạy, giữ khoá repo
    d = hub.dispatcher
    assert d._lock_block(hub.tasks.row(b_old["id"]), "c2", "sess-b", False) == "workspace_locked"
    assert d._lock_block(hub.tasks.row(c["id"]), "c3", "sess-c", False) is None


def test_worktree_reuse_and_refusals(hub, repo):
    t1 = hub.tasks.create({"title": "1", "body": "x", "assignee": "c1", "worktree": {"repo": str(repo), "branch": "feat/d"}})
    path = t1["workspace"]["path"]
    t2 = hub.tasks.create({"title": "2", "body": "x", "assignee": "c2", "worktree": {"repo": str(repo), "branch": "feat/d"}})
    assert t2["workspace"]["path"] == path and any("dùng lại worktree" in w for w in t2["warnings"])
    with pytest.raises(HubError, match="tên nhánh"):
        hub.tasks.create({"title": "3", "body": "x", "assignee": "c3", "worktree": {"repo": str(repo), "branch": "bad..name"}})
    with pytest.raises(HubError, match="base"):
        hub.tasks.create({"title": "3", "body": "x", "assignee": "c3", "worktree": {"repo": str(repo), "branch": "feat/e", "base": "nope"}})
    other = repo.parent / "busy"
    other.mkdir()
    with pytest.raises(HubError, match="đã tồn tại"):
        hub.tasks.create({"title": "3", "body": "x", "assignee": "c3", "worktree": {"repo": str(repo), "branch": "feat/f", "path": str(other)}})
    with pytest.raises(HubError, match="repo"):
        hub.tasks.create({"title": "3", "body": "x", "assignee": "c3", "worktree": {"branch": "feat/g"}})


def test_snapshot_and_panel_show_waiting_tasks(hub, repo):
    t = hub.tasks.create({"title": "Part B: field", "body": "x", "assignee": "c2", "workspace": {"kind": "dir", "path": str(repo)}})
    hub.tasks.update(t["id"], {"wait_reason": "workspace_locked"})
    snap = tui_rpc.snapshot(hub)
    assert [(w["id"], w["assignee"], w["wait_reason"]) for w in snap["waiting_tasks"]] == [(t["id"], "c2", "workspace_locked")]

    mon = G.AgentMonitor()
    mon.load_snapshot(snap)
    act = mon.active()
    assert [(s, r.id) for s, r in act] == [("waiting_task", "c2")]
    line = G.summary_line(mon, 200).plain
    assert "1 task chờ: thư mục đang bị run khác giữ" in line and "agent đang chạy" not in line
    assert "Part B: field" in G._task(mon, "waiting_task", act[0][1])
    head = panel_head(mon).plain
    assert head.startswith("0 agent đang chạy") and "1 task chờ: thư mục đang bị run khác giữ" in head
