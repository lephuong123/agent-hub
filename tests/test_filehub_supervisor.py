"""Thư mục chung Không gian số (filehub/) — giám sát binary sync + service + RPC/CLI, với binary GIẢ `fake_tangthu_sync.py`
(đúng hợp đồng §8.3 của docs/dev-notes/plan-cloud-files.md). Không gọi mạng, không gọi claude.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from cfharness.errors import HubError
from cfharness.filehub.supervisor import ENV_KEEP, child_env, parse_version, version_ok
from cfharness.recovery import pid_alive, pid_start_time
from fh_fake import FAKE, argv_log, configure, events, fake_bin, inject, until
from m7_fake import close, make_daemon, rpc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="binary sync chỉ macOS/Linux (shim #!/bin/sh)")


@pytest.fixture
async def env(hub_home, tmp_path, monkeypatch):
    # env "bẩn" của daemon — tiến trình sync KHÔNG được thấy
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak-0000")
    monkeypatch.setenv("CFHARNESS_SOCKET_HINT", "x")
    made: list = []

    async def make(**kw):
        d = await make_daemon(hub_home)
        made.append(d)
        return d

    yield make, tmp_path
    for d in made:
        await d.hub.filehub.stop()
        await close(d)


async def _started(make, tmp, **kw):
    d = await make()
    local = configure(d.hub, tmp, **kw)
    d.hub.filehub.start()
    await until(lambda: (d.hub.filehub.read_status() or {}).get("roots"))
    return d, local


def test_version_helpers():
    assert parse_version("tang-thu-sync-headless 1.2.3") == (1, 2, 3)
    assert version_ok("tang-thu-sync-headless 0.3.0", "0.1.0") and not version_ok("x 0.0.9", "0.1.0") and not version_ok(None, "0.1.0")
    env = child_env({"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_X": "1", "CFHARNESS_HOME": "/hub", "ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t"})
    assert set(env) <= set(ENV_KEEP) and "GITHUB_TOKEN" not in env and env["PATH"] == "/bin"


async def test_supervisor_starts_with_contract_args_and_minimal_env(env):
    make, tmp = env
    d, local = await _started(make, tmp)
    st = await rpc(d, "filehub.status")
    assert st["state"] in ("idle", "starting") and st["folders"][0]["name"] == "chung" and st["sync_version"].endswith("0.3.0")
    runs = [a for a in argv_log(d.hub) if "--profile" in a["argv"]]
    a = runs[0]["argv"]
    fh = d.hub.filehub
    for flag, val in (("--profile", str(fh.profile_dir)), ("--status", str(fh.status_path)), ("--control", str(fh.control_path)),
                      ("--tokens", str(fh.tokens_path)), ("--extra-ignore", str(fh.ignore_path)), ("--max-delete-batch", "0"),
                      ("--server", "https://kgs.example.test"), ("--add-root", "r_chung"), ("--root-name", "chung"), ("--local-parent", str(local.parent))):
        assert a[a.index(flag) + 1] == val, flag
    assert str(fh.tokens_path).startswith(str(d.hub.paths.home / "secrets"))
    # env tối thiểu: không CLAUDE*/ANTHROPIC*/CFHARNESS*, không token người
    keys = set(runs[0]["env"])
    assert not {k for k in keys if k.startswith(("CLAUDE", "ANTHROPIC", "CFHARNESS"))}, keys
    assert keys <= set(ENV_KEEP) | {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING"}, keys - set(ENV_KEEP)
    ign = fh.ignore_path.read_text()
    for pat in (".git/", ".env", "*.pem", "id_rsa", "!.env.example", ".tangthu*"):
        assert pat in ign.splitlines(), pat
    assert oct(os.stat(fh.dir).st_mode & 0o777) == "0o700"
    # lần chạy sau (root đã có trong status) không truyền --add-root
    d.hub.filehub.supervisor.restart()
    await until(lambda: len([x for x in argv_log(d.hub) if "--profile" in x["argv"]]) >= 2)
    assert "--add-root" not in [x for x in argv_log(d.hub) if "--profile" in x["argv"]][-1]["argv"]
    assert [e.payload["state"] for e in events(d.hub, "filehub.state")][:1] in (["idle"], ["starting"])


async def test_restart_with_backoff_then_graceful_sigterm(env):
    make, tmp = env
    d, _ = await _started(make, tmp, env={"FAKE_TT_EXIT_AFTER_S": "0.3"})
    sv = d.hub.filehub.supervisor
    await until(lambda: sv.restarts >= 2, timeout=15)
    assert sv.last_error and "mã 3" in sv.last_error
    # SIGTERM → binary dừng êm (ghi stopped.txt, exit 0) trước khi daemon dừng
    d.hub.config.set("filehub.sync_binary", str(fake_bin(tmp / "bin2")))
    sv.restart()
    await until(lambda: sv.state == "running" and sv.proc is not None and sv.proc.returncode is None)
    proc = sv.proc
    # chờ binary ghi status với pid của nó (đã cài handler SIGTERM) — tránh race SIGTERM tới trước khi interpreter sẵn sàng (CI chậm)
    st = d.hub.filehub.status_path
    await until(lambda: st.exists() and json.loads(st.read_text() or "{}").get("pid") == proc.pid, timeout=15)
    await d.hub.filehub.stop()
    assert proc.returncode == 0 and (d.hub.filehub.profile_dir / "stopped.txt").exists()
    assert json.loads(sv.pidfile.read_text())["pid"] is None


async def test_reconcile_kills_orphan_from_previous_daemon(env):
    make, tmp = env
    d = await make()
    configure(d.hub, tmp)
    fh = d.hub.filehub
    fh.prepare_files()
    orphan = subprocess.Popen([sys.executable, str(FAKE), "--profile", str(fh.profile_dir), "--server", "x", "--tokens", str(fh.tokens_path),
                               "--status", str(fh.status_path)], start_new_session=True)
    try:
        await until(lambda: pid_start_time(orphan.pid) is not None)
        fh.supervisor._save_pid(orphan.pid, pid_start_time(orphan.pid))
        res = fh.supervisor.reconcile()
        assert res["found"] and res["pid"] == orphan.pid
        await until(lambda: orphan.poll() is not None)
        # pid đã bị tái dùng (start_time lệch) → KHÔNG giết
        fh.supervisor._save_pid(os.getpid(), 1.0)
        assert fh.supervisor.reconcile() == {"found": False, "stale": True} and pid_alive(os.getpid())
    finally:
        if orphan.poll() is None:
            orphan.kill()


async def test_version_mismatch_does_not_run(env):
    make, tmp = env
    d = await make()
    configure(d.hub, tmp, env={"FAKE_TT_VERSION": "0.0.1"})
    d.hub.filehub.start()
    await until(lambda: d.hub.filehub.supervisor.state == "version_mismatch")
    assert not [x for x in argv_log(d.hub) if "--profile" in x["argv"]]
    st = await rpc(d, "filehub.status")
    assert st["state"] == "version_mismatch" and "0.0.1" in st["supervisor"]["last_error"]
    await until(lambda: any(e.payload["state"] == "version_mismatch" for e in events(d.hub, "filehub.state")))


async def test_setup_login_creates_folder_and_audits(env, monkeypatch):
    make, tmp = env
    d = await make()
    b = fake_bin(tmp / "bin")
    d.hub.config.set("filehub.tick_s", 0.05)
    d.hub.config.set("filehub.poll_s", 0.1)
    res = await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "name": "Tài liệu",
                                         "local_parent": str(tmp / "KGS"), "no_browser": True})
    assert res["login_url"].startswith("https://kgs.example.test/api/sync/oauth/authorize?root_mode=single")
    await until(lambda: d.hub.filehub.login_state.get("state") == "done")
    f = d.hub.filehub.folders()
    rid = d.hub.filehub.login_state["roots"][0]["id"]
    assert f == [{"name": "Tài liệu", "root_id": rid, "ws_id": "ws_1", "local_path": str((tmp / "KGS" / "Tài liệu").resolve())}]
    assert oct(os.stat(d.hub.filehub.tokens_path).st_mode & 0o777) == "0o600"
    await until(lambda: (d.hub.filehub.read_status() or {}).get("roots"))
    assert [r["kind"] for r in d.hub.audit.query(kind="filehub.setup")]
    # đăng nhập lỗi → trạng thái error, không thêm gì
    d.hub.config.set("filehub.sync_binary", str(fake_bin(tmp / "bin-err", {"FAKE_TT_LOGIN_ERROR": "access_denied"})))
    await rpc(d, "filehub.login", {"no_browser": True})
    await until(lambda: d.hub.filehub.login_state.get("state") == "error")
    assert d.hub.filehub.login_state["error"] == "access_denied" and len(d.hub.filehub.folders()) == 1
    with pytest.raises(HubError, match="https"):
        await rpc(d, "filehub.setup", {"server": "ftp://x", "binary": str(b)})


async def test_add_validates_local_path(env):
    make, tmp = env
    d, local = await _started(make, tmp)
    fh = d.hub.filehub
    with pytest.raises(HubError, match="HUB_HOME"):
        fh.add("r_2", "hai", local_path=str(d.hub.paths.home / "x"))
    with pytest.raises(HubError, match="trùng/chứa"):
        fh.add("r_2", "hai", local_path=str(local / "con"))
    with pytest.raises(HubError, match="không hợp lệ"):
        fh.add("../x", "hai")
    w = tmp / "W"
    w.mkdir()
    d.hub.config.set("workspace.confine", {"root": str(w)})
    with pytest.raises(HubError, match="giam"):
        fh.add("r_2", "hai", local_path=str(tmp / "ngoai" / "hai"))
    r = fh.add("r_2", "hai", local_path=str(w / "hai"))
    assert r["workspace"] == "filehub:hai" and len(fh.folders()) == 2
    assert (await rpc(d, "filehub.remove", {"name": "hai"}))["removed"] == "hai" and len(fh.folders()) == 1


async def test_conflict_and_mass_delete_block_then_resume_push(env):
    make, tmp = env
    d = await make()
    local = configure(d.hub, tmp)
    d.hub.config.set("filehub.max_delete_batch", 1)
    for n in ("a.txt", "b.txt", "c.txt"):
        (local / n).write_text(n)
    d.hub.filehub.start()
    await until(lambda: (d.hub.filehub.read_status() or {}).get("roots"))
    inject(d.hub, op="conflict", root_id="r_chung", path="bao-cao.docx")
    await until(lambda: events(d.hub, "filehub.conflict"))
    assert events(d.hub, "filehub.conflict")[0].payload == {"folder": "chung", "root_id": "r_chung", "path": "bao-cao.docx"}
    assert (await rpc(d, "filehub.status"))["state"] == "conflict"
    (local / "a.txt").unlink()
    (local / "b.txt").unlink()
    await until(lambda: events(d.hub, "filehub.blocked"))
    assert events(d.hub, "filehub.blocked")[0].payload["reason"] == "mass_delete"
    await rpc(d, "filehub.resume", {"root_id": "r_chung"})
    assert json.loads(d.hub.filehub.control_path.read_text())["resume_push"] == ["r_chung"]
    await until(lambda: not ((d.hub.filehub.read_status() or {}).get("roots") or [{}])[0].get("blocked"))
    await until(lambda: json.loads(d.hub.filehub.control_path.read_text())["resume_push"] == [])


async def test_link_from_index(env):
    make, tmp = env
    d, local = await _started(make, tmp)
    (local / "Báo cáo").mkdir()
    (local / "Báo cáo" / "q3.docx").write_text("x")
    await until(lambda: "Báo cáo/q3.docx" in (d.hub.filehub.read_index("r_chung").get("items") or {}))
    r = await rpc(d, "filehub.link", {"path": str(local / "Báo cáo" / "q3.docx")})
    it = d.hub.filehub.read_index("r_chung")["items"]
    assert r["url"] == f"https://kgs.example.test/files/{it['Báo cáo']['folder_id']}?file={it['Báo cáo/q3.docx']['file_id']}"
    assert (await rpc(d, "filehub.list"))[0]["workspace"] == "filehub:chung"
    with pytest.raises(HubError, match="không nằm trong thư mục chung"):
        await rpc(d, "filehub.link", {"path": str(tmp / "khac.txt")})


async def test_pause_resume(env):
    make, tmp = env
    d, _ = await _started(make, tmp)
    await rpc(d, "filehub.pause")
    await until(lambda: d.hub.filehub.supervisor.state == "stopped")
    assert (await rpc(d, "filehub.status"))["state"] == "paused"
    await rpc(d, "filehub.resume")
    await until(lambda: d.hub.filehub.supervisor.state == "running")
    assert [r["kind"] for r in d.hub.audit.query(kind="filehub.pause")]


def test_cli_parser_has_filehub_commands():
    from cfharness.cli import build_parser

    p = build_parser()
    a = p.parse_args(["filehub", "setup", "--server", "https://x", "--binary", "/b", "--no-browser", "--no-wait"])
    assert a.fh_action == "setup" and a.server == "https://x" and a.no_browser
    assert p.parse_args(["filehub", "resume", "--root", "r1"]).root == "r1"
    assert p.parse_args(["filehub", "remove", "chung"]).name == "chung"


async def test_hub_stop_terminates_sync(env):
    make, tmp = env
    d, _ = await _started(make, tmp)
    proc = d.hub.filehub.supervisor.proc
    assert proc is not None and proc.returncode is None
    await asyncio.wait_for(d.hub.filehub.stop(), 10)
    assert proc.returncode == 0
