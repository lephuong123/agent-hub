"""Hub clone PR5 (docs/dev-notes/plan-hub-clone.md): chuyển nhà (`hub export --for move` → nguồn ĐÓNG BĂNG, `hub unfreeze`), `repos.json`
(+ `--clone-repos`, agent thiếu workspace → paused), viết lại đường dẫn trong tri thức qua ledger (actor `import`), `hub doctor`.

Hai HUB_HOME tạm với hai `$HOME` giả (hai máy). git thật trên repo tạm (remote `file://` tạm) — không mạng, không gọi claude.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfharness import auth
from cfharness import hub_transfer as T
from cfharness.errors import BLOCKED, Blocked, Conflict, HubError
from cfharness.paths import HubPaths
from test_hub_transfer import AGENT, make_source

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null")
CRED = "s3cr3t-TOKEN-zz"


def git(cwd: Path, *args: str) -> str:
    cp = subprocess.run([*GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return cp.stdout.strip()


def _db_exec(db: Path, sql: str, args: tuple = ()) -> None:
    c = sqlite3.connect(str(db), isolation_level=None)
    try:
        c.execute(sql, args)
    finally:
        c.close()


def make_repo_source(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Máy A: `make_source` + `proj` là repo git (remote file:// tạm, nhánh main, có file chưa commit) + team `t1` dùng repo thứ hai có
    remote https MANG thông tin đăng nhập + skill/SOUL có đường dẫn máy cũ."""
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    src, proj = make_source(tmp_path, monkeypatch)
    bare = tmp_path / "remote" / "proj.git"
    bare.parent.mkdir()
    subprocess.run([*GIT, "init", "--bare", "-q", str(bare)], check=True)
    git(proj, "init", "-q")
    (proj / "README.md").write_text("hello\n")
    git(proj, "add", "README.md")
    git(proj, "commit", "-q", "-m", "init")
    git(proj, "remote", "add", "origin", f"file://{bare}")
    git(proj, "push", "-q", "origin", "main")
    (proj / "wip.txt").write_text("chưa commit\n")  # dirty
    head = git(proj, "rev-parse", "HEAD")
    team_repo = proj.parent / "team-repo"
    team_repo.mkdir()
    git(team_repo, "init", "-q")
    (team_repo / "a.txt").write_text("a\n")
    git(team_repo, "add", "a.txt")
    git(team_repo, "commit", "-q", "-m", "a")
    git(team_repo, "remote", "add", "origin", f"https://bob:{CRED}@git.example.invalid/org/r.git")
    _db_exec(src.db, "UPDATE teams SET policies=? WHERE id='t1'", (json.dumps({"workspace": {"kind": "dir", "path": str(team_repo)}}),))
    h = Hub(src, runtime=FakeRuntime())
    try:
        r = h.learning.pipeline.propose(target="skill", op="add", agent_id="a1", created_by="user",
                                        payload={"name": "deploy", "description": "Deploy steps", "content": f"Run make in {proj}/app\n"})
        assert r.get("status") in ("applied", "promoted", "canary"), r
        h.agents.update("a1", {}, soul=f"# A1\nWork in {proj}.\n")
        h.integrity.rebaseline("test")
    finally:
        h.store.close()
    return SimpleNamespace(src=src, proj=proj, bare=bare, head=head, team_repo=team_repo)


def _dst(tmp_path: Path, monkeypatch, name: str = "userB") -> tuple[HubPaths, Path]:
    user = tmp_path / name
    user.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(user))
    return HubPaths.resolve(user / ".cfharness"), user


# ============================================================================ đóng băng (chuyển nhà)
async def test_move_export_freezes_source_blocks_everything_until_human_unfreeze(hub_home):
    from m7_fake import close, make_daemon, rpc, wait_status

    from cfharness.events import make_event
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.hub_freeze import frozen
    from cfharness.notify.base import QueuedNotifier
    from cfharness.recovery import resume_interrupted
    from cfharness.runs import export_quiesced, exporting

    class Probe(QueuedNotifier):
        channel = "probe"

        def cfg(self):
            return {"events": ["hub.estop"]}

        def target(self):
            return {}

        def render(self, ev):
            return "x"

        async def _send(self, target, ev, msg):
            return {}

    d = await make_daemon(hub_home)
    try:
        h = d.hub
        h.agents.create({"id": "w1", "name": "W1", "model": "haiku"})
        h.secrets.set("TELEGRAM_BOT_TOKEN", "123:tok-telegram")
        h.secrets.set("SLACK_BOT_TOKEN", "xoxb-slack")
        h.config.set("notify.slack.interactive", True)
        assert h.telegram.token() and h.slack.token()
        # agent không đóng băng/gỡ được
        for m in ("hub.unfreeze", "hub.freeze", "hub.doctor"):
            with pytest.raises(HubError) as ei:
                await rpc(d, m, {}, as_caller=AGENT)
            assert ei.value.code == BLOCKED, m
            assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED
        with pytest.raises(HubError) as ei:
            await rpc(d, "hub.export", {"for": "move"}, as_caller=AGENT)
        assert ei.value.code == BLOCKED and frozen(h) is None
        ctx = GuardContext(cwd=hub_home.home, policy=PolicyStore(hub_home.home).load())
        for cmd in ("cfharness hub unfreeze --yes", "cfharness hub doctor --json", "uv run cfharness hub export --for move -y"):
            assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd

        r = await rpc(d, "hub.export", {"profile": "core", "for": "move", "_audit": {"channel": "cli"}})
        mig = r["migrated_to"]
        assert mig["export_id"] == r["id"] and mig["sha256"] == r["sha256"] and mig["sealed"] is False and h.runs.quiesced is None
        assert frozen(h)["export_id"] == r["id"]
        # mọi đường tạo run bị từ chối
        with pytest.raises(Blocked, match="ĐÃ CHUYỂN"):
            h.runs.submit("w1", "x")
        for kind in ("manager", "schedule", "review"):
            with pytest.raises(Blocked):
                h.runs.submit("w1", "x", kind=kind)
        with pytest.raises(HubError) as ei:
            await rpc(d, "run.start", {"agent": "w1", "prompt": "x"})
        assert ei.value.code == BLOCKED and "migrated_to" in (ei.value.data or {})
        # vòng nền bỏ lượt (trừ sao lưu), hàng đợi không chạy, khôi phục không resume
        assert export_quiesced(h) and not exporting(h)
        assert h.runs.tick() == []
        assert resume_interrupted(h, {"id": "r_x", "agent_id": "w1", "kind": "adhoc", "session_id": "s", "started_at": "t"}) is None
        # kênh chat im: không token → không long poll / không gửi; notifier bỏ thông báo
        assert h.telegram.token() is None and h.slack.token() is None and h.telegram.token(ignore_enabled=True)
        p = Probe(h)
        p(make_event("hub.estop", None, {"on": True}))
        assert p.queue is None
        # banner: hub.status, tui.snapshot, hub.frozen
        st = await rpc(d, "hub.status")
        assert st["migrated_to"]["export_id"] == r["id"]
        snap = await rpc(d, "tui.snapshot")
        assert snap["hub"]["migrated_to"]["export_id"] == r["id"]
        fr = await rpc(d, "hub.frozen")
        assert "ĐÃ CHUYỂN" in fr["banner"] and "hub unfreeze" in fr["banner"]
        assert h.store.conn.execute("SELECT COUNT(*) FROM events WHERE type='hub.frozen'").fetchone()[0] == 1
        # xuất move lần nữa khi đã đóng băng → từ chối; hub.freeze chỉ cập nhật đúng export_id
        with pytest.raises(HubError, match="đã đóng băng"):
            await rpc(d, "hub.export", {"profile": "core", "for": "move"})
        with pytest.raises(HubError, match="export_id"):
            await rpc(d, "hub.freeze", {"export_id": "khac-20261115-000000", "sha256": "0" * 64})
        up = await rpc(d, "hub.freeze", {"export_id": r["id"], "sha256": "a" * 64, "sealed": True})
        assert up["sha256"] == "a" * 64 and up["sealed"] is True and up["at"] == mig["at"]
    finally:
        await close(d)

    # BỀN qua restart daemon
    d = await make_daemon(hub_home)
    try:
        h = d.hub
        assert frozen(h)["export_id"] == r["id"]
        with pytest.raises(Blocked):
            h.runs.submit("w1", "sau restart")
        assert h.telegram.token() is None
        # người gỡ (sai export_id → từ chối) → chạy lại bình thường
        with pytest.raises(HubError, match="không phải"):
            await rpc(d, "hub.unfreeze", {"export_id": "khac-20261115-000000"})
        res = await rpc(d, "hub.unfreeze", {"export_id": r["id"], "_audit": {"channel": "cli"}})
        assert res["unfrozen"] and frozen(h) is None and not export_quiesced(h)
        assert h.telegram.token() == "123:tok-telegram"
        run = h.runs.submit("w1", "chạy lại")
        await wait_status(d, run["id"], ("running", "completed"))
        kinds = [x["kind"] for x in h.store.conn.execute("SELECT kind FROM audit_log WHERE kind LIKE 'hub.%' ORDER BY id")]
        assert "hub.export" in kinds and "hub.freeze" in kinds and "hub.unfreeze" in kinds
        assert h.store.conn.execute("SELECT COUNT(*) FROM events WHERE type='hub.unfrozen'").fetchone()[0] == 1
    finally:
        await close(d)


def test_move_offline_cli_freezes_with_sealed_sha_and_package_never_carries_freeze(tmp_path, monkeypatch, capsys):
    from cfharness import backup as B
    from cfharness.cli import main

    src, _proj = make_source(tmp_path, monkeypatch)
    pf = tmp_path / "pass.txt"
    pf.write_text("mot-passphrase-du-dai\n")
    os.chmod(pf, 0o600)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    out = tmp_path / "move.cfhx"
    assert main(["hub", "export", str(out), "--for", "move", "--profile", "core", "--passphrase-file", str(pf), "--home", str(src.home)]) == 1
    assert not out.exists() and "--yes" in capsys.readouterr().err  # đóng băng cần xác nhận
    assert main(["hub", "export", str(out), "--for", "move", "--yes", "--profile", "core", "--passphrase-file", str(pf), "--home", str(src.home)]) == 0
    text = capsys.readouterr().out
    assert "ĐÃ CHUYỂN" in text and "--mode move" in text
    c = sqlite3.connect(str(src.db))
    mig = json.loads(c.execute("SELECT value FROM hub_state WHERE key='migrated_to'").fetchone()[0])
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE kind='hub.freeze'").fetchone()[0] >= 1
    c.close()
    assert mig["sealed"] is True and mig["sha256"] == B._sha_file(out) and mig["export_id"] == T.read_manifest(out)["id"]
    # xuất move lần nữa → từ chối (đã đóng băng); xuất clone vẫn được (sao chép dữ liệu của hub đóng băng)
    assert main(["hub", "export", "--for", "move", "--yes", "--no-secrets", "--home", str(src.home)]) == 1
    assert "đã đóng băng" in capsys.readouterr().err
    capsys.readouterr()
    r2 = T.export_offline(src, profile="core")
    # gói xuất từ hub đóng băng KHÔNG mang trạng thái đóng băng sang máy mới
    dst, _ = _dst(tmp_path, monkeypatch)
    rep = T.import_package(dst, Path(r2["path"]), mode="move")
    assert "migrated_to" in rep["state_cleared"]
    c = sqlite3.connect(str(dst.db))
    assert c.execute("SELECT value FROM hub_state WHERE key='migrated_to'").fetchone() is None
    c.close()


# ============================================================================ repos.json
def test_repos_json_masks_credentials_records_dirty_and_import_pauses_missing(tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    s = make_repo_source(tmp_path, monkeypatch)
    r = T.export_offline(s.src, profile="core")
    pkg = Path(r["path"])
    assert CRED.encode() not in pkg.read_bytes()
    m = T.read_manifest(pkg)
    assert m["sections"]["repos"] == {"prefix": "repos.json", "count": 2, "bytes": m["sections"]["repos"]["bytes"], "optional": True}
    repos = {x["path"]: x for x in r["repos"]}
    p = repos[str(s.proj)]
    assert p["git"] and p["remote"] == f"file://{s.bare}" and p["branch"] == "main" and p["dirty"] and p["uncommitted"] == 1
    t = repos[str(s.team_repo)]
    assert t["remote"] == "https://git.example.invalid/org/r.git" and t["remote_masked"] and not t["dirty"]
    assert any("chưa commit" in line for line in [json.dumps(r)]) is False  # chữ cảnh báo chỉ ở CLI

    dst, userb = _dst(tmp_path, monkeypatch)
    new_proj = userb / "Workspace" / "proj"
    plan = T.import_plan(dst, pkg)
    assert not plan["blockers"], plan["blockers"]
    pr = {x["from"]: x for x in plan["repos"]}
    assert pr[str(s.proj)]["to"] == str(new_proj) and not pr[str(s.proj)]["exists"]
    assert pr[str(s.proj)]["hint"] == f"git clone file://{s.bare} {new_proj} && git -C {new_proj} checkout main"
    assert any("KHÔNG đi theo gói" in w for w in plan["warnings"]) and any("chưa có ở máy này" in w for w in plan["warnings"])
    assert set(plan["agents_paused"]) == {"a1"} and not dst.home.exists()

    rep = T.import_package(dst, pkg)
    assert rep["repos_cloned"] == [] and set(rep["agents_paused"]) == {"a1"}
    h = Hub(dst, runtime=FakeRuntime())
    try:
        assert h.store.get_agent("a1")["status"] == "paused" and h.store.get_agent("w1")["status"] != "paused"
        why = h.store.state_get("import_paused")["a1"]
        assert why["reason"] == "[hub] workspace chưa có trên máy này" and why["workspace"] == str(new_proj)
        with pytest.raises(Blocked, match=r"\[hub\] workspace chưa có"):
            h.runs.submit("a1", "x")
        assert {x["to"] for x in h.store.state_get("import_repos")} == {str(new_proj), str(userb / "Workspace" / "team-repo")}
        # người tạo repo rồi `agent resume` → chạy được, lý do bị gỡ
        new_proj.mkdir(parents=True)
        h.agents.set_paused("a1", False)
        assert h.store.get_agent("a1")["status"] == "idle" and "a1" not in (h.store.state_get("import_paused") or {})
    finally:
        h.store.close()


def test_clone_repos_clones_from_remote_never_into_existing_and_unpauses(tmp_path, monkeypatch, capsys):
    from cfharness.cli import main

    s = make_repo_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(s.src, profile="core")["path"])
    dst, userb = _dst(tmp_path, monkeypatch)
    new_proj = userb / "Workspace" / "proj"
    new_team = userb / "Workspace" / "team-repo"
    new_team.mkdir(parents=True)
    (new_team / "mine.txt").write_text("của người ở máy B\n")  # thư mục ĐÃ CÓ (không phải git) — không bao giờ bị đụng
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["hub", "import", str(pkg), "--clone-repos", "--yes", "--home", str(dst.home)]) == 0
    text = capsys.readouterr().out
    assert f"✓ đã clone {new_proj}" in text and "hub doctor" in text
    assert git(new_proj, "rev-parse", "HEAD") == s.head and git(new_proj, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not (new_proj / "wip.txt").exists()  # thay đổi chưa commit không đi theo
    assert sorted(os.listdir(new_team)) == ["mine.txt"]
    c = sqlite3.connect(str(dst.db))
    assert c.execute("SELECT status FROM agents WHERE id='a1'").fetchone()[0] == "idle"
    assert json.loads(c.execute("SELECT value FROM hub_state WHERE key='import_paused'").fetchone()[0]) == {}
    c.close()

    # repo thiếu + remote không tới được → báo, lần nhập vẫn xong
    from cfharness import transfer_repos as TR

    res = TR.clone([{"to": str(tmp_path / "x" / "r"), "remote": "file:///khong/co/repo.git", "exists": False},
                    {"to": str(new_team), "remote": f"file://{s.bare}", "exists": False},
                    {"to": str(tmp_path / "y"), "remote": None, "exists": False}])
    assert [x["ok"] for x in res] == [False, False, False]
    assert "không clone đè" in res[1]["error"] and "không có remote" in res[2]["error"]
    assert sorted(os.listdir(new_team)) == ["mine.txt"]

    # doctor JSON trên hub vừa nhập (daemon chưa chạy)
    capsys.readouterr()
    rc = main(["hub", "doctor", "--json", "--home", str(dst.home)])
    doc = json.loads(capsys.readouterr().out)
    ids = {x["id"]: x for x in doc["checks"]}
    assert rc in (0, 1) and doc["home"] == str(dst.home)
    assert ids[f"repo:{new_proj}"]["ok"] and ids["imported_from"]["level"] == "info"
    assert ids["followups"]["level"] == "info" and "CHỜ daemon" in ids["followups"]["detail"]
    assert ids["schedules"]["fix"].startswith("cfharness schedule list") and ids["cloned_from"]
    assert ids["daemon"]["level"] == "info" and "secret_refs" in ids and "perms" in ids and "vector" in ids and "autostart" in ids
    assert "claude_path" in ids and "claude_login" in ids and "telegram" in ids


def test_mask_remote_variants():
    from cfharness.transfer_repos import mask_remote

    assert mask_remote("https://u:p@h.example/x.git") == ("https://h.example/x.git", True)
    assert mask_remote("https://ghp_abc@github.com/o/r") == ("https://github.com/o/r", True)
    assert mask_remote("git@github.com:o/r.git") == ("git@github.com:o/r.git", False)
    assert mask_remote("https://h.example/r.git?private_token=zzz") == ("https://h.example/r.git?private_token=***", True)
    assert mask_remote(None) == (None, False)


# ============================================================================ viết lại tri thức qua ledger (actor import)
async def test_followups_rewrite_memory_skill_soul_graph_via_ledger_idempotent_and_rollbackable(tmp_path, monkeypatch):
    from m7_fake import close, make_daemon

    from cfharness import import_followups as F
    from cfharness.learning.ledger import AUTO_CHANGE_ACTORS, IMPORT_ACTOR, is_auto_change_actor

    assert IMPORT_ACTOR not in AUTO_CHANGE_ACTORS and not is_auto_change_actor(IMPORT_ACTOR)
    s = make_repo_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(s.src, profile="core")["path"])
    dst, userb = _dst(tmp_path, monkeypatch)
    new_proj = userb / "Workspace" / "proj"
    rep = T.import_package(dst, pkg)
    a1 = dst.home / "agents" / "a1"
    assert {"agents/a1/MEMORY.md", "agents/a1/SOUL.md", "agents/a1/skills/deploy/SKILL.md"} <= set(rep["review"]), rep["review"]
    assert str(s.proj) in (a1 / "MEMORY.md").read_text()

    real_soul = F._soul
    monkeypatch.setattr(F, "_soul", lambda hub, pm, aid: (_ for _ in ()).throw(RuntimeError("hỏng giả")))
    d = await make_daemon(dst)  # Hub.start → import_followups.process
    try:
        h = d.hub
        fu = h.store.state_get("import_followups")
        assert fu["status"] == "partial" and fu["retry"] == ["agents/a1/SOUL.md"], fu
        assert str(new_proj) in (a1 / "MEMORY.md").read_text() and str(s.proj) not in (a1 / "MEMORY.md").read_text()  # lỗi SOUL không chặn mục khác
        assert str(s.proj) in (a1 / "SOUL.md").read_text()
        monkeypatch.setattr(F, "_soul", real_soul)
        res = F.process(h)
        assert res["status"] == "done" and res["rewritten"] == ["agents/a1/SOUL.md"]
        assert f"Work in {new_proj}." in (a1 / "SOUL.md").read_text()
        assert h.store.conn.execute("SELECT actor FROM agent_revisions WHERE agent_id='a1' ORDER BY revision DESC LIMIT 1").fetchone()[0] == "import"

        rows = h.store.conn.execute("SELECT id, target, blob_kind, actor FROM ledger_entries WHERE actor='import' ORDER BY rowid").fetchall()
        assert {(x["target"], x["blob_kind"]) for x in rows} == {("memory", "file"), ("skill", "tree"), ("config", "config"), ("graph", "graph")}
        assert str(new_proj) in (a1 / "skills" / "deploy" / "SKILL.md").read_text()
        assert str(s.proj) not in (a1 / "config.yaml").read_text()
        live = [x["fact"] for x in h.store.conn.execute("SELECT fact, folded FROM mem_facts WHERE retracted_by IS NULL AND invalidated_at IS NULL")]
        assert any(str(new_proj) in f for f in live) and not any(str(s.proj) in f for f in live)
        assert not F.graph_rows(h.store.conn, F._pm(fu))
        audit = h.store.conn.execute("SELECT data FROM audit_log WHERE kind='hub.import_followups' ORDER BY id").fetchall()
        assert [json.loads(x["data"])["status"] for x in audit] == ["partial", "done"]

        # idempotent: chạy lại (kể cả ép status) không tạo entry mới
        assert F.process(h) is None
        h.store.state_set("import_followups", {**h.store.state_get("import_followups"), "status": "pending"})
        n = len(rows)
        again = F.process(h)
        assert again["status"] == "done" and not again["entries"] and not again["rewritten"]
        assert h.store.conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE actor='import'").fetchone()[0] == n

        # tự rollback (regression monitor) KHÔNG đụng thay đổi do người khởi xướng; người rollback được
        mem = next(x["id"] for x in rows if x["target"] == "memory")
        with pytest.raises(Conflict) as ei:
            h.learning.ledger.rollback(mem, actor="regression_monitor")
        assert (ei.value.data or {}).get("human_change")
        h.learning.ledger.rollback(mem, actor="user")
        assert str(s.proj) in (a1 / "MEMORY.md").read_text()
        g = next(x["id"] for x in rows if x["target"] == "graph")
        h.learning.ledger.rollback(g, actor="user")
        assert any(str(s.proj) in x[0] for x in h.store.conn.execute("SELECT fact FROM mem_facts WHERE retracted_by IS NULL"))
    finally:
        await close(d)


def test_doctor_rpc_registered_human_only_and_runs_offline(tmp_path, monkeypatch):
    from cfharness import hub_doctor as D

    src, _ = make_source(tmp_path, monkeypatch)
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho '2.1.312 (Claude Code)'\n")
    os.chmod(fake, 0o755)
    (src.home / "secrets" / "CLAUDE_CODE_OAUTH_TOKEN").write_text("sk-ant-DOCTOR-VALUE-9")
    src.config.write_text(src.config.read_text() + "x_ref: '${MISSING_ONE}'\n")
    rep = D.run(src, claude_binary=str(fake))
    ids = {x["id"]: x for x in rep["checks"]}
    assert ids["claude_path"]["ok"] and ids["claude_version"]["ok"] and ids["claude_login"]["ok"]
    assert not ids["secret_refs"]["ok"] and "MISSING_ONE" in ids["secret_refs"]["detail"] and ids["secret_refs"]["fix"] == "cfharness secret set MISSING_ONE"
    assert not rep["ok"] and rep["summary"]["errors"] >= 1
    assert "DOCTOR-VALUE" not in json.dumps(rep)  # không bao giờ đọc/in giá trị secret
    assert "✗ secret được tham chiếu" in D.text(rep)
    assert "hub.doctor" in auth.HUMAN_ONLY and "hub.doctor" not in auth.all_mutating()


async def test_web_banner_when_frozen(hub_home):
    from test_web import ISOLATED, _start, _stop

    from cfharness.hub_freeze import freeze, unfreeze
    from cfharness.runtime import FakeRuntime
    from cfharness.web.app import invalidate_nav_badge

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        await env.login()
        assert 'id="migrated-banner"' not in (await env.client.get("/costs")).text
        freeze(env.hub, {"export_id": "may-a-20261115-101010", "sha256": "0" * 64, "profile": "full"})
        invalidate_nav_badge(env.app.state.web)  # banner theo cache menu 2 s/phiên (#196); đổi thẳng trong hub, không qua POST web
        html = (await env.client.get("/costs")).text
        assert 'id="migrated-banner"' in html and "may-a-20261115-101010" in html and "cfharness hub unfreeze" in html
        unfreeze(env.hub)
        invalidate_nav_badge(env.app.state.web)
        assert 'id="migrated-banner"' not in (await env.client.get("/costs")).text
    finally:
        await _stop(env)
