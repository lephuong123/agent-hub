"""Dung lượng HUB_HOME + dọn thư mục làm việc mồ côi (housekeeping.py, `hub doctor`, RPC `hub.housekeeping|cleanup`, thẻ Sức khoẻ web).

Không gọi claude: daemon in-process với ScriptedRuntime (m7_fake), dữ liệu run/task/hội thoại chèn thẳng vào DB.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfharness import auth
from cfharness import housekeeping as HK
from cfharness import hub_doctor as D
from cfharness.errors import BLOCKED, HubError

AGENT = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mk(p: Path, size: int = 100) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    (p / "f.txt").write_bytes(b"x" * size)
    return p


def _run(conn, rid: str, agent: str, status: str, cwd: str | None = None, ended: datetime | None = None) -> None:
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO runs (id, agent_id, kind, status, queued_at, ended_at, cwd) VALUES (?,?,?,?,?,?,?)",
                 (rid, agent, "adhoc", status, _iso(ended or now), _iso(ended) if ended else None, cwd))


def _setup(d, tmp_path: Path) -> dict[str, Path]:
    h = d.hub
    h.agents.create({"id": "w1", "name": "W1", "model": "haiku"})
    h.agents.create({"id": "w2", "name": "W2", "model": "haiku"})
    home = h.paths.home
    ws1 = home / "agents" / "w1" / "workspaces"
    c = h.store.conn
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    p: dict[str, Path] = {}
    # hội thoại: còn mở (giữ), đã lưu trữ (dọn), đã lưu trữ nhưng có run sống trong đó (giữ — busy), không còn (dọn)
    for cid, arch in (("c_open", None), ("c_arch", _iso(old)), ("c_busy", _iso(old))):
        c.execute("INSERT INTO manager_conversations (id, channel, agent_id, created_at, updated_at, archived_at) VALUES (?,?,?,?,?,?)",
                  (cid, "web", "w1", _iso(now), _iso(now), arch))
        p[cid] = _mk(ws1 / f"conv-{cid}")
    p["c_gone"] = _mk(ws1 / "conv-c_gone", 5000)
    _run(c, "r_busy", "w1", "running", cwd=str(p["c_busy"]))
    # scratch: mồ côi (dọn), run xong cũ (chỉ dọn khi --older-than), run xong mới (giữ), run đang chạy (giữ), workspace_default (giữ)
    p["orphan"] = _mk(ws1 / "r_ghost", 2000)
    p["old"] = _mk(ws1 / "r_old")
    _run(c, "r_old", "w1", "completed", ended=old)
    t = time.time() - 60 * 86400
    os.utime(p["old"], (t, t))
    p["recent"] = _mk(ws1 / "r_recent")
    _run(c, "r_recent", "w1", "completed", ended=now)
    p["live"] = _mk(home / "agents" / "w2" / "workspaces" / "r_live")
    _run(c, "r_live", "w2", "interrupted")  # interrupted = có thể tự resume → coi như sống
    p["default"] = _mk(ws1 / "mydir")
    c.execute("UPDATE agents SET workspace_default=? WHERE id='w1'", (str(p["default"]),))
    # continuation: run khác dùng cwd của r_old2 (scratch của run không còn) → không mồ côi
    p["cont"] = _mk(ws1 / "r_parent")
    _run(c, "r_child", "w1", "completed", cwd=str(p["cont"] / "sub"), ended=now)
    # worktrees/ trong HUB_HOME: mồ côi (dọn) + được task mở tham chiếu (giữ)
    p["wt_orphan"] = _mk(home / "worktrees" / "wt-x")
    p["wt_task"] = _mk(home / "worktrees" / "wt-y")
    c.execute("INSERT INTO tasks (id, title, status, workspace, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              ("t_open", "x", "running", json.dumps({"kind": "worktree", "path": str(p["wt_task"])}), _iso(now), _iso(now)))
    # git worktree NGOÀI HUB_HOME của task đã xong → chỉ báo
    p["ext_wt"] = _mk(tmp_path / "repo" / "_wt" / "repo-feat")
    c.execute("INSERT INTO tasks (id, title, status, workspace, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              ("t_done", "y", "done", json.dumps({"kind": "worktree", "path": str(p["ext_wt"])}), _iso(now), _iso(now)))
    # symlink trong workspaces trỏ ra ngoài: không bao giờ là ứng viên
    p["outside"] = _mk(tmp_path / "outside")
    (ws1 / "evil").symlink_to(p["outside"], target_is_directory=True)
    c.commit()
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "big.log").write_bytes(b"y" * 50_000)
    return p


async def test_scan_doctor_and_cleanup_only_orphans_human_only_audited(hub_home, tmp_path):
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        h = d.hub
        p = _setup(d, tmp_path)
        home = h.paths.home
        rep = HK.scan(home, h.store.conn)
        kinds = {x["rel"]: x for x in rep["items"]}
        rel = lambda k: p[k].relative_to(home).as_posix()  # noqa: E731
        assert kinds[rel("c_arch")]["kind"] == "conv_archived" and kinds[rel("c_arch")]["cleanable"]
        assert kinds[rel("c_gone")]["kind"] == "conv_missing" and kinds[rel("c_gone")]["cleanable"]
        assert kinds[rel("c_busy")]["busy"] == ["r_busy"] and not kinds[rel("c_busy")]["cleanable"]
        assert kinds[rel("orphan")]["kind"] == "scratch_orphan" and kinds[rel("orphan")]["cleanable"]
        assert kinds[rel("wt_orphan")]["kind"] == "worktree_orphan"
        assert kinds[rel("old")]["kind"] == "scratch_stale" and not kinds[rel("old")]["cleanable"]  # chỉ đếm khi không có --older-than
        for k in ("c_open", "recent", "live", "default", "cont", "wt_task"):
            assert rel(k) not in kinds, k
        assert not any("evil" in r for r in kinds)
        assert rep["stale"]["count"] == 1 and rep["cleanable"]["count"] == 4
        assert [t["task"] for t in rep["task_worktrees"]] == ["t_done"] and not rep["task_worktrees"][0]["cleanable"]
        rep30 = HK.scan(home, h.store.conn, older_than_days=30)
        assert {x["rel"]: x for x in rep30["items"]}[rel("old")]["cleanable"]

        # dung lượng theo mục cấp 1
        du = HK.disk_usage(home)
        names = {x["name"]: x for x in du["entries"]}
        assert names["logs"]["bytes"] >= 50_000 and du["total"] >= 50_000 and du["agent_workspaces"]["bytes"] > 7000

        # doctor (chỉ đọc, chạy được ngoài daemon) có kiểm disk + cleanup + gợi ý lệnh
        doc = D.run(h.paths)
        by = {x["id"]: x for x in doc["checks"]}
        assert by["disk"]["level"] == "info" and "logs" in by["disk"]["detail"]
        assert by["cleanup"]["level"] == "warning" and "--cleanup" in by["cleanup"]["fix"]
        assert by["cleanup_stale"]["fix"] == "cfharness hub doctor --cleanup --older-than 30"
        assert any(k.startswith("task_worktree:") and "git worktree remove" in v["fix"] for k, v in by.items())
        assert "cleanup" in D.text(doc) and "→ cfharness hub doctor --cleanup" in D.text(doc)
        assert all(p[k].is_dir() for k in ("c_arch", "c_gone", "orphan", "wt_orphan"))  # doctor không xoá gì
        assert "conv-c_arch" in D.cleanup_text(rep)

        # agent không đọc/dọn được
        for m in ("hub.housekeeping", "hub.cleanup"):
            with pytest.raises(HubError) as ei:
                await rpc(d, m, {"paths": [rel("orphan")]}, as_caller=AGENT)
            assert ei.value.code == BLOCKED and m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED
        assert p["orphan"].is_dir()

        hk = await rpc(d, "hub.housekeeping", {})
        assert hk["cleanup"]["cleanable"]["count"] == 4 and hk["disk"]["total"] > 0
        runs_before = h.store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        req = [x["rel"] for x in hk["cleanup"]["items"]] + ["agents/w1/workspaces/evil", "agents/w1/workspaces/../../../outside",
                                                             rel("recent"), "hub.db"]
        res = await rpc(d, "hub.cleanup", {"paths": req, "_audit": {"channel": "cli"}})
        removed = {x["rel"] for x in res["removed"]}
        assert removed == {rel("c_arch"), rel("c_gone"), rel("orphan"), rel("wt_orphan")}
        assert res["freed_bytes"] >= 7000
        skipped = {x["rel"]: x["reason"] for x in res["skipped"]}
        assert "run/task" in skipped[rel("c_busy")] and rel("old") in skipped and rel("recent") in skipped
        for k in ("c_open", "c_busy", "old", "recent", "live", "default", "cont", "wt_task", "ext_wt", "outside"):
            assert p[k].is_dir(), k
        assert (p["outside"] / "f.txt").is_file() and h.paths.db.is_file()
        assert h.store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before
        a = h.store.conn.execute("SELECT actor, channel FROM audit_log WHERE kind='hub.cleanup'").fetchall()
        assert [tuple(x) for x in a] == [("user", "cli")]

        # có --older-than: dọn cả scratch của run đã xong cũ; run sống lúc dọn → giữ dù được yêu cầu
        _mk(p["orphan"])
        h.store.conn.execute("INSERT INTO runs (id, agent_id, kind, status, queued_at, cwd) VALUES ('r_new','w1','adhoc','running',?,?)",
                             (_iso(datetime.now(timezone.utc)), str(p["orphan"])))
        h.store.conn.commit()
        res = await rpc(d, "hub.cleanup", {"paths": [rel("old"), rel("orphan")], "older_than_days": 30})
        assert [x["rel"] for x in res["removed"]] == [rel("old")] and not p["old"].exists() and p["orphan"].is_dir()
    finally:
        await close(d)


def test_cli_older_than_requires_cleanup_and_classifier_blocks_agent(hub_home, capsys):
    from cfharness.cli import main
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    assert main(["hub", "doctor", "--older-than", "30", "--home", str(hub_home.home)]) == 1
    assert "--cleanup" in capsys.readouterr().err
    ctx = GuardContext(cwd=hub_home.home, policy=PolicyStore(hub_home.home).load())
    for cmd in ("cfharness hub doctor --cleanup --yes", "cfharness hub doctor --cleanup --older-than 1 -y"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


def test_scan_without_db_never_guesses_orphans(tmp_path):
    home = tmp_path / "h"
    _mk(home / "agents" / "a" / "workspaces" / "r_1")
    _mk(home / "agents" / "a" / "workspaces" / "conv-x")
    _mk(home / "worktrees" / "w")
    rep = HK.scan(home, None)
    assert rep["items"] == [] and rep["cleanable"]["count"] == 0
    assert HK.human(0) == "0 B" and HK.human(1536) == "1.5 KB" and HK.human(5 * 1024 ** 3) == "5.0 GB"


def test_safe_target_rejects_outside_and_symlinks(tmp_path):
    home = tmp_path / "h"
    ok = _mk(home / "agents" / "a" / "workspaces" / "r_1")
    out = _mk(tmp_path / "out")
    (home / "agents" / "b").mkdir(parents=True)
    (home / "agents" / "b" / "workspaces").symlink_to(out, target_is_directory=True)
    _mk(out / "r_2")
    assert HK._safe_target(home, str(ok))
    assert not HK._safe_target(home, str(home / "agents" / "b" / "workspaces" / "r_2"))
    assert not HK._safe_target(home, str(home / "agents" / "a"))
    assert not HK._safe_target(home, str(out))
    assert not HK._safe_target(home, str(home / "logs"))
