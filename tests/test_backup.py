"""Sao lưu / khôi phục HUB_HOME (backup.py, backup_cli.py, web/backup_pages.py) + lưới an toàn cập nhật (update_helper.py, updater.py).

Không gọi mạng/uv thật: lệnh cài/khởi động lại là lệnh python giả; "daemon" để helper dò là một Unix socket giả trong luồng phụ.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cfharness import __version__, auth
from cfharness import backup as B
from cfharness import update_helper as helper
from cfharness.errors import BLOCKED, Conflict, HubError
from cfharness.guard.classifier import GuardContext, classify, sensitive_read
from cfharness.guard.policy import PolicyStore
from cfharness.guard.runtime import sandbox_settings, secret_read_deny_rules
from cfharness.paths import HubPaths
from cfharness.updater import Updater, parse_version

AGENT = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])


def seed_home(p: HubPaths) -> None:
    """Hub giả có đủ loại mục: được sao lưu, bị loại, bị loại trong agents/<id>/."""
    from cfharness.store import Store

    s = Store(p.db)
    s.state_set("marker", "v1")
    s.close()
    (p.home / "config.yaml").write_text("hub:\n  max_concurrent_runs: 3\n")
    (p.home / "policy").mkdir(exist_ok=True)
    (p.home / "policy" / "protected.yaml").write_text("paths: []\n")
    a = p.home / "agents" / "w1"
    (a / "skills" / "s1").mkdir(parents=True)
    (a / "MEMORY.md").write_text("mem v1\n")
    (a / "skills" / "s1" / "SKILL.md").write_text("skill v1\n")
    (a / "workspaces" / "scratch").mkdir(parents=True)
    (a / "workspaces" / "scratch" / "big.bin").write_text("workspace data")
    (a / "runtime").mkdir()
    (a / "runtime" / "mcp.json").write_text('{"token": "RESOLVED-TOKEN-XYZ"}')
    (p.home / "secrets" / "API_KEY").write_text("sk-SECRET")
    (p.home / "auth").mkdir(exist_ok=True)
    (p.home / "auth" / "web.json").write_text('{"k": "SIGNING-KEY-XYZ"}')
    (p.home / "mcp_server").mkdir(exist_ok=True)
    (p.home / "mcp_server" / "tokens.json").write_text('{"t": "x"}')
    (p.home / "runs" / "r1").mkdir(parents=True)
    (p.home / "runs" / "r1" / "stdout.jsonl").write_text("{}\n")
    (p.home / "ledger" / "blobs").mkdir(parents=True)
    (p.home / "ledger" / "blobs" / "ab").write_text("blob")


def tar_names(path: str) -> list[str]:
    with tarfile.open(path, "r:gz") as tf:
        return tf.getnames()


# ------------------------------------------------------------------ tạo + kiểm
def test_create_archive_private_manifest_and_excludes_secrets(hub_home):
    seed_home(hub_home)
    r = B.create(hub_home, label="Trước sửa lớn!")
    d = B.backup_dir(hub_home)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    arc = Path(r["path"] or B.archive_path(hub_home, r["id"]))
    assert stat.S_IMODE(os.stat(arc).st_mode) == 0o600 and stat.S_IMODE(os.stat(B.sidecar_path(hub_home, r["id"])).st_mode) == 0o600
    assert r["id"].endswith("-manual-truoc-sua-lon") or r["id"].endswith("-manual-tr-c-s-a-l-n"), r["id"]
    names = tar_names(str(arc))
    assert "hub.db" in names and names[-1] == "MANIFEST.json"
    assert "home/config.yaml" in names and "home/policy/protected.yaml" in names and "home/agents/w1/MEMORY.md" in names
    assert "home/agents/w1/skills/s1/SKILL.md" in names and "home/ledger/blobs/ab" in names
    for bad in ("secrets", "auth", "mcp_server", "runs", "workspaces", "runtime"):
        assert not any(f"/{bad}/" in n or n.startswith(f"home/{bad}") for n in names), (bad, names)
    raw = arc.read_bytes()
    with tarfile.open(arc, "r:gz") as tf:
        blob = b"".join(tf.extractfile(m).read() for m in tf if m.isreg())
    assert b"sk-SECRET" not in blob and b"SIGNING-KEY-XYZ" not in blob and b"RESOLVED-TOKEN-XYZ" not in blob and b"sk-SECRET" not in raw
    side = json.loads(B.sidecar_path(hub_home, r["id"]).read_text())
    m = side["manifest"]
    assert m["cfharness_version"] == __version__ and m["user_version"] == B.latest_schema_version() and m["db"]["sha256"]
    assert all(len(f["sha256"]) == 64 for f in m["files"]) and "secrets" in m["excluded"]
    lst = B.list_backups(hub_home)
    assert [b["id"] for b in lst] == [r["id"]] and lst[0]["has_db"]
    v = B.verify(hub_home, r["id"])
    assert v["ok"] and v["integrity"] == "ok" and v["files"] == len(m["files"]), v


def test_verify_detects_tampering_and_foreign_files_ignored(hub_home):
    seed_home(hub_home)
    r = B.create(hub_home)
    (B.backup_dir(hub_home) / "hub-before-v14-20261019-105529.db").write_text("bản sao tay cũ")  # file lạ trong thư mục
    assert [b["id"] for b in B.list_backups(hub_home)] == [r["id"]]
    arc = B.archive_path(hub_home, r["id"])
    # đổi nội dung một file trong archive (viết lại tar, giữ manifest cũ) → sha256 archive + sha256 file đều lệch
    tmp = arc.with_name("x.tar.gz")
    with tarfile.open(arc, "r:gz") as src, tarfile.open(tmp, "w:gz") as dst:
        for mem in src:
            data = src.extractfile(mem).read()
            if mem.name == "home/agents/w1/MEMORY.md":
                data = b"poisoned\n"
                mem.size = len(data)
            dst.addfile(mem, io.BytesIO(data))
    os.replace(tmp, arc)
    v = B.verify(hub_home, r["id"])
    assert not v["ok"] and any("archive" in e for e in v["errors"]) and any("MEMORY.md" in e for e in v["errors"]), v
    with pytest.raises(HubError, match="không hợp lệ"):
        B.verify(hub_home, "../../etc/passwd")


def test_retention_keeps_n_and_never_prunes_backup_needed_for_rollback(hub_home):
    seed_home(hub_home)
    cfg = {"keep": 2, "keep_safety": 1}
    ids = []
    for i in range(3):
        ids.append(B.create(hub_home, cfg=cfg, label=f"m{i}")["id"])
        time.sleep(1.05)
    left = [b["id"] for b in B.list_backups(hub_home)]
    assert left == [ids[2], ids[1]]
    pu = B.create(hub_home, kind="pre-update", cfg=cfg)["id"]
    (hub_home.home / "update").mkdir()
    (hub_home.home / "update" / "state.json").write_text(json.dumps({"state": "done", "backup": {"id": pu}}))
    time.sleep(1.05)
    B.create(hub_home, kind="pre-restore", cfg=cfg)
    assert pu in [b["id"] for b in B.list_backups(hub_home)]  # update --rollback còn cần → giữ dù vượt keep_safety


# ------------------------------------------------------------------ khôi phục
def test_restore_roundtrip_keeps_secrets_workspaces_and_rebaselines_integrity(hub_home):
    seed_home(hub_home)
    r = B.create(hub_home)
    # thay đổi sau khi sao lưu
    from cfharness.store import Store

    s = Store(hub_home.db)
    s.state_set("marker", "v2")
    s.close()
    (hub_home.home / "agents" / "w1" / "MEMORY.md").write_text("mem v2\n")
    (hub_home.home / "agents" / "w1" / "skills" / "new").mkdir()
    (hub_home.home / "agents" / "w1" / "skills" / "new" / "SKILL.md").write_text("skill mới\n")
    (hub_home.home / "policy" / "protected.yaml").write_text("paths: [/x]\n")
    (hub_home.home / "secrets" / "API_KEY").write_text("sk-NEW")
    res = B.restore(hub_home, r["id"])
    assert res["db_restored"] and res["pre_restore"] and res["files_removed"] >= 1
    s = Store(hub_home.db)
    assert s.state_get("marker") == "v1"
    rows = {x["path"]: x["sha256"] for x in s.conn.execute("SELECT path, sha256 FROM integrity_baseline")}
    s.close()
    assert (hub_home.home / "agents" / "w1" / "MEMORY.md").read_text() == "mem v1\n"
    assert not (hub_home.home / "agents" / "w1" / "skills" / "new" / "SKILL.md").exists()
    assert (hub_home.home / "policy" / "protected.yaml").read_text() == "paths: []\n"
    assert (hub_home.home / "secrets" / "API_KEY").read_text() == "sk-NEW"  # secret không nằm trong bản sao → giữ nguyên
    assert (hub_home.home / "agents" / "w1" / "workspaces" / "scratch" / "big.bin").exists()
    assert (hub_home.home / "agents" / "w1" / "runtime" / "mcp.json").exists()
    import hashlib

    assert rows["policy/protected.yaml"] == hashlib.sha256(b"paths: []\n").hexdigest()
    assert rows["config.yaml"] == hashlib.sha256((hub_home.home / "config.yaml").read_bytes()).hexdigest()
    # bản pre-restore giữ hiện trạng trước khi ghi đè (kể cả hub.db v2)
    pre = res["pre_restore"]
    assert B.verify(hub_home, pre)["ok"]
    with tarfile.open(B.archive_path(hub_home, pre), "r:gz") as tf:
        assert tf.extractfile("home/agents/w1/MEMORY.md").read() == b"mem v2\n"


def test_restore_refused_while_daemon_lock_held_or_inside_run(hub_home, monkeypatch):
    from cfharness.daemon import acquire_instance_lock, release_instance_lock

    seed_home(hub_home)
    r = B.create(hub_home)
    lk = acquire_instance_lock(hub_home)
    try:
        with pytest.raises(Conflict, match="daemon đang chạy"):
            B.restore(hub_home, r["id"])
    finally:
        release_instance_lock(lk)
    monkeypatch.setenv(auth.ENV_RUN_ID, "r_agent")
    with pytest.raises(HubError, match="chỉ người"):
        B.restore(hub_home, r["id"])


def test_restore_refuses_backup_from_newer_schema(hub_home):
    seed_home(hub_home)
    r = B.create(hub_home)
    side_p = B.sidecar_path(hub_home, r["id"])
    side = json.loads(side_p.read_text())
    side["manifest"]["user_version"] = B.latest_schema_version() + 5
    side_p.write_text(json.dumps(side))
    plan = B.restore_plan(hub_home, r["id"], check=False)
    assert plan["blockers"] and "mới hơn" in plan["blockers"][0]
    with pytest.raises(HubError, match="mới hơn"):
        B.restore(hub_home, r["id"])


def test_cli_restore_two_step_confirmation_and_requires_stop(hub_home, monkeypatch, capsys):
    from cfharness.cli import main

    seed_home(hub_home)
    r = B.create(hub_home)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["backup", "restore", r["id"]]) == 1  # không TTY, thiếu --yes/--confirm-id
    assert "--confirm-id" in capsys.readouterr().err
    assert main(["backup", "restore", r["id"], "--yes", "--confirm-id", "sai-id"]) == 1
    assert "không khớp" in capsys.readouterr().err
    monkeypatch.setattr("cfharness.home_migrate.daemon_running", lambda home: 4242)
    monkeypatch.setattr("cfharness.rpc.RpcClient.call", lambda self, m, p=None, **kw: B.restore_plan(hub_home, r["id"], daemon_running=True))
    assert main(["backup", "restore", r["id"], "--yes", "--confirm-id", r["id"]]) == 2  # daemon chạy, không --stop
    assert "--stop" in capsys.readouterr().err
    monkeypatch.setattr("cfharness.home_migrate.daemon_running", lambda home: None)
    (hub_home.home / "agents" / "w1" / "MEMORY.md").write_text("đổi\n")
    assert main(["backup", "restore", r["id"], "--yes", "--confirm-id", r["id"]]) == 0
    assert (hub_home.home / "agents" / "w1" / "MEMORY.md").read_text() == "mem v1\n"


# ------------------------------------------------------------------ RPC + quyền + guard
async def test_rpc_human_only_audit_and_event(hub_home):
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        for m, p in (("backup.create", {}), ("backup.verify", {"id": "20260101T000000-manual"}), ("backup.restore_plan", {"id": "20260101T000000-manual"})):
            with pytest.raises(HubError) as ei:
                await rpc(d, m, p, as_caller=AGENT)
            assert ei.value.code == BLOCKED, m
        assert not B.list_backups(hub_home)
        r = await rpc(d, "backup.create", {"label": "web", "_audit": {"channel": "web"}})
        assert r["id"] and r["user_version"] == d.hub.store.schema_version
        rows = d.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='backup.create'").fetchall()
        assert [x["channel"] for x in rows] == ["web"]
        lst = await rpc(d, "backup.list", {})
        assert [b["id"] for b in lst["backups"]] == [r["id"]] and lst["schedule"]["enabled"]
        assert (await rpc(d, "backup.verify", {"id": r["id"]}))["ok"]
        plan = await rpc(d, "backup.restore_plan", {"id": r["id"]})
        assert plan["daemon_running"] and not plan["blockers"] and "--stop" in plan["command"]
        evs = d.hub.store.conn.execute("SELECT type FROM events WHERE type='backup.created'").fetchall()
        assert evs
    finally:
        await close(d)


async def test_schedule_tick_creates_auto_backup_once(hub_home):
    from m7_fake import close, make_daemon

    d = await make_daemon(hub_home)
    try:
        svc = d.hub.backups
        d.hub.config.set("backup.schedule", "every 1h")
        svc.started_at = time.time() - 7200
        assert svc.due()
        r = await svc.tick()
        assert r and r["kind"] == "auto"
        assert not svc.due() and await svc.tick() is None  # mốc kế tiếp tính từ bản auto vừa tạo
        d.hub.config.set("backup.schedule", "off")
        assert not svc.schedule_info()["enabled"]
        with pytest.raises(ValueError):
            d.hub.config.set("backup.schedule", "khi nào rảnh")
        with pytest.raises(ValueError):
            d.hub.config.set("backup.keep", 0)
    finally:
        await close(d)


def test_classifier_and_sandbox_protect_backups(tmp_path):
    hub = tmp_path / "h"
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(hub).load())
    for cmd in ("cfharness backup restore 20260101T000000-manual --stop --yes", "cfharness backup list", "uv run cfharness backup create"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    home = str(ctx.policy.hub_home)
    assert sensitive_read(f"{home}/backups/20260101T000000-manual.tar.gz", ctx)
    sb = sandbox_settings(Path(home), Path(home) / "hub.sock", {})
    fs = sb["filesystem"]
    assert any(x.rstrip("/").endswith("/backups") for x in fs["denyRead"]) and any(x.rstrip("/").endswith("/backups") for x in fs["denyWrite"])
    assert any("backups/**" in r and r.startswith("Read(") for r in secret_read_deny_rules(Path(home), Path(home) / "hub.sock"))


# ------------------------------------------------------------------ web
async def test_web_backup_card_and_create_action(hub_home):
    from test_web import ISOLATED, _start, _stop, audit_rows

    from cfharness.runtime import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        await env.login()
        assert 'hx-get="/partials/backup"' in (await env.client.get("/settings")).text
        card = (await env.client.get("/partials/backup")).text
        assert "Sao lưu" in card and 'action="/do/backup_create"' in card and "Chưa có bản sao lưu" in card
        r = await env.post("/do/backup_create", {"next": "/settings#backup"})
        assert r.status_code == 200 and "Tạo bản sao lưu HUB_HOME ngay?" in r.text and not B.list_backups(hub_home)
        r = await env.post("/do/backup_create", {"confirm": "yes", "next": "/settings#backup"})
        assert r.status_code == 303
        (b,) = B.list_backups(hub_home)
        assert audit_rows(env, "backup.create")[-1]["channel"] == "web"
        card = (await env.client.get("/partials/backup")).text
        assert b["id"] in card and f"cfharness backup restore {b['id']} --stop" in card and "/do/backup_restore" not in card
    finally:
        await _stop(env)


# ------------------------------------------------------------------ cập nhật có lưới an toàn
CUR = parse_version(__version__)
NEWER = f"v{CUR[0]}.{CUR[1]}.{CUR[2] + 1}"


def _updater(paths, state_tag=NEWER):
    spawned: list = []

    def spawn(argv, opts):
        spawned.append((argv, opts))
        return 4242

    up = Updater(paths, fetch=lambda t: {"tag": state_tag, "name": state_tag, "published_at": None, "url": "u"}, spawn=spawn,
                 info=lambda: {"kind": "uv_git", "prefix": "/x", "source": "git"}, uv="/usr/bin/uv", python="/venv/bin/python",
                 base_python="/base/python3")
    return up, spawned


def test_update_start_backs_up_first_and_passes_db_to_helper(hub_home):
    seed_home(hub_home)
    up, spawned = _updater(hub_home)
    with pytest.raises(HubError, match="bản sao lưu"):
        up.start(backup=lambda: (_ for _ in ()).throw(OSError("đĩa đầy")))
    assert not spawned and up.state() is None
    up.start(backup=lambda: B.create(hub_home, kind="pre-update", label=NEWER))
    argv, _ = spawned[0]
    st = up.state()
    bk = st["backup"]
    assert bk["id"].split("-", 1)[1].startswith("pre-update") and bk["user_version"] == B.latest_schema_version()
    assert argv[argv.index("--db-backup") + 1] == bk["path"] and argv[argv.index("--db-version") + 1] == str(bk["user_version"])
    assert argv[argv.index("--socket") + 1] == str(hub_home.socket)
    rb = json.loads(argv[argv.index("--rollback-install") + 1])
    assert rb[-1] == "git+https://github.com/lephuong123/agent-hub@v%d.%d.%d" % CUR


def test_update_rollback_target_and_spawn(hub_home):
    up, spawned = _updater(hub_home)
    with pytest.raises(HubError, match="chưa có lần cập nhật"):
        up.rollback()
    up._write_state({"state": "done", "kind": "update", "tag": f"v{__version__}", "version_before": "0.1.0", "backup": None})
    r = up.rollback()
    argv, _ = spawned[0]
    assert argv[3] == "rollback" and json.loads(argv[argv.index("--install") + 1])[-1].endswith("@v0.1.0") and r["tag"] == "v0.1.0"
    assert "--rollback-install" not in argv and up.state()["kind"] == "rollback"
    up._write_state({**up.state(), "state": "done"})
    with pytest.raises(Conflict, match="đã là quay về"):
        up.rollback()
    up._write_state({"state": "failed", "kind": "update", "version_before": "0.1.0", "rolled_back": True, "tag": NEWER})
    with pytest.raises(Conflict, match="tự quay về"):
        up.rollback()
    up._write_state({"state": "done", "kind": "update", "version_before": __version__, "tag": NEWER})
    with pytest.raises(Conflict, match="đúng bản trước"):
        up.rollback()


async def test_rpc_update_rollback_human_only(hub_home):
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        up = d.hub.updater
        new, spawned = _updater(hub_home)
        up.spawn, up.info, up._uv = new.spawn, new.info, new._uv
        up._write_state({"state": "done", "kind": "update", "tag": NEWER, "version_before": "0.1.0"})
        with pytest.raises(HubError) as ei:
            await rpc(d, "update.rollback", {}, as_caller=AGENT)
        assert ei.value.code == BLOCKED and not spawned
        r = await rpc(d, "update.rollback", {"_audit": {"channel": "cli"}})
        assert r["rollback"] and spawned
        await asyncio.wait_for(d.stopping.wait(), 3)
        assert d.hub.store.conn.execute("SELECT count(*) FROM audit_log WHERE kind='update.rollback'").fetchone()[0] == 1
    finally:
        await close(d)


class FakeDaemonSocket:
    """Unix socket trả lời hub.ping CHỈ khi file cờ tồn tại (= "daemon đã lên")."""

    def __init__(self, flag: Path) -> None:
        self.dir = tempfile.mkdtemp(prefix="cfhb-", dir="/tmp")
        self.path = os.path.join(self.dir, "s.sock")
        self.flag = flag
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self) -> None:
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            with c:
                try:
                    c.recv(4096)
                    if self.flag.exists():
                        c.sendall(b'{"jsonrpc":"2.0","id":1,"result":{"pong":true,"pid":1}}\n')
                except OSError:
                    pass

    def close(self) -> None:
        self.stop = True
        self.t.join(2)
        self.srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def _py(code: str) -> str:
    return json.dumps([sys.executable, "-c", code])


def _set_uv(db: Path, v: int) -> None:
    c = sqlite3.connect(str(db))
    c.execute(f"PRAGMA user_version={v}")
    c.commit()
    c.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_helper_rolls_back_install_and_db_when_daemon_not_up(hub_home, monkeypatch):
    monkeypatch.setattr(helper, "STABLE_S", 0.0)
    seed_home(hub_home)
    bk = B.create(hub_home, kind="pre-update")
    uv0 = bk["user_version"]
    _set_uv(hub_home.db, uv0 + 2)  # bản mới đã chạy migration rồi mới chết
    flag = hub_home.home / "daemon_up"
    fake = FakeDaemonSocket(flag)
    try:
        state, log = hub_home.home / "update" / "state.json", hub_home.logs_dir / "update.log"
        helper.write_state(state, {"state": "starting", "tag": NEWER, "version_before": __version__})
        marker = hub_home.home / "reinstalled"
        rc = helper.main(["apply", "--state", str(state), "--log", str(log), "--home", str(hub_home.home), "--socket", fake.path,
                          "--health-timeout", "0.6", "--install", _py("print('Installed')"), "--restart", _py("pass"),
                          "--rollback-install", _py(f"open({str(marker)!r}, 'w').write('1'); open({str(flag)!r}, 'w').write('1')"),
                          "--db-backup", bk["path"], "--db-version", str(uv0), "--verify", _py(f"print('cfharness {__version__}')")])
    finally:
        fake.close()
    st = json.loads(state.read_text())
    assert rc == 1 and st["state"] == "failed" and st["rolled_back"] and marker.exists(), st
    rb = st["rollback"]
    assert rb["ok"] and rb["db_restored"] and rb["user_version_now"] == uv0 + 2 and "hub.db trước cập nhật" in st["error"]
    assert helper.db_user_version(hub_home.db) == uv0
    assert Path(rb["db_replaced_kept_at"]).exists()  # DB đã migrate được giữ lại, không xoá
    assert stat.S_IMODE(os.stat(hub_home.db).st_mode) == 0o600
    assert "daemon KHÔNG trả lời" in log.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_helper_healthy_update_no_rollback_and_db_untouched_when_schema_same(hub_home, monkeypatch):
    monkeypatch.setattr(helper, "STABLE_S", 0.0)
    seed_home(hub_home)
    bk = B.create(hub_home, kind="pre-update")
    flag = hub_home.home / "daemon_up"
    fake = FakeDaemonSocket(flag)
    try:
        state, log = hub_home.home / "update" / "state.json", hub_home.logs_dir / "update.log"
        helper.write_state(state, {"state": "starting", "tag": NEWER, "version_before": __version__})
        marker = hub_home.home / "reinstalled"
        rc = helper.main(["apply", "--state", str(state), "--log", str(log), "--home", str(hub_home.home), "--socket", fake.path,
                          "--health-timeout", "3", "--install", _py("pass"), "--restart", _py(f"open({str(flag)!r}, 'w').write('1')"),
                          "--rollback-install", _py(f"open({str(marker)!r}, 'w').write('1')"),
                          "--db-backup", bk["path"], "--db-version", str(bk["user_version"])])
        assert rc == 0 and json.loads(state.read_text())["state"] == "done" and not marker.exists()
        # daemon không lên nhưng schema KHÔNG đổi → chỉ cài lại bản cũ, hub.db giữ nguyên
        flag.unlink()
        helper.write_state(state, {"state": "starting", "tag": NEWER, "version_before": __version__})
        rc = helper.main(["apply", "--state", str(state), "--log", str(log), "--home", str(hub_home.home), "--socket", fake.path,
                          "--health-timeout", "0.5", "--install", _py("pass"), "--restart", _py("pass"),
                          "--rollback-install", _py(f"open({str(flag)!r}, 'w').write('1')"),
                          "--db-backup", bk["path"], "--db-version", str(bk["user_version"])])
    finally:
        fake.close()
    st = json.loads(state.read_text())
    assert rc == 1 and st["rolled_back"] and not st["rollback"]["db_restored"]
    assert not list(B.backup_dir(hub_home).glob("hub.db.replaced-*"))


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_helper_rollback_command_restores_db(hub_home, monkeypatch):
    monkeypatch.setattr(helper, "STABLE_S", 0.0)
    seed_home(hub_home)
    bk = B.create(hub_home, kind="pre-update")
    _set_uv(hub_home.db, bk["user_version"] + 1)
    flag = hub_home.home / "daemon_up"
    fake = FakeDaemonSocket(flag)
    try:
        state, log = hub_home.home / "update" / "state.json", hub_home.logs_dir / "update.log"
        helper.write_state(state, {"state": "starting", "kind": "rollback", "tag": "v0.1.0", "version_before": __version__})
        rc = helper.main(["rollback", "--state", str(state), "--log", str(log), "--home", str(hub_home.home), "--socket", fake.path,
                          "--health-timeout", "3", "--install", _py("pass"), "--restart", _py(f"open({str(flag)!r}, 'w').write('1')"),
                          "--db-backup", bk["path"], "--db-version", str(bk["user_version"]), "--verify", _py("print('cfharness 0.1.0')")])
    finally:
        fake.close()
    st = json.loads(state.read_text())
    assert rc == 0 and st["state"] == "done" and st["rollback"]["db_restored"] and st["version_after"] == "0.1.0"
    assert helper.db_user_version(hub_home.db) == bk["user_version"]


def test_helper_stays_stdlib_only():
    src = Path(helper.__file__).read_text()
    assert "cfharness" not in [ln.split()[1].split(".")[0] for ln in src.splitlines() if ln.startswith(("import ", "from "))]
