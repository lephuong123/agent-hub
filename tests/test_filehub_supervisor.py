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
from fh_fake import FAKE, argv_log, configure, events, fake_bin, inject, login_argv, until
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
    assert str(fh.tokens_path).startswith(str(d.hub.paths.home / "secrets")) and "--root-kind" not in a  # folder = mặc định
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
    res = await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "name": "Tài liệu", "scope": "folder",
                                         "local_parent": str(tmp / "KGS"), "no_browser": True})
    assert res["login_url"].startswith("https://kgs.example.test/api/oauth/authorize?root_mode=single") and res["mode"] == "folder"
    await until(lambda: d.hub.filehub.login_state.get("state") == "done")
    f = d.hub.filehub.folders()
    rid = d.hub.filehub.login_state["roots"][0]["id"]
    assert f == [{"name": "Tài liệu", "root_id": rid, "ws_id": "ws_1", "local_path": str((tmp / "KGS" / "Tài liệu").resolve()), "kind": "folder",
                  "account": "admin.cchc"}]
    # chế độ một thư mục: KHÔNG truyền --root-mode (mặc định single của binary — chạy được với binary cũ)
    assert "--root-mode" not in login_argv(d.hub)[0]
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


# ------------------------------------------------------------------ gốc workspace (plan-filehub-workspace-root.md, PR H7)
async def test_setup_workspace_mode_root_5_cols_status_rename_alias(env):
    """Mặc định chế độ workspace: `--root-mode workspace`, ROOT 5 cột (kind + tài khoản) → thư mục chung kind=workspace (root_id = wsId);
    status: "Toàn bộ workspace «tên sống»", tài khoản, tệp ở gốc không đẩy được; đổi tên trên web → tên cũ giữ, tên mới là bí danh."""
    make, tmp = env
    d = await make()
    b = fake_bin(tmp / "bin", {"FAKE_TT_WS_NAME": "admin harness", "FAKE_TT_ACCOUNT": "chu.domain"})
    d.hub.config.set("filehub.tick_s", 0.05)
    d.hub.config.set("filehub.poll_s", 0.1)
    res = await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "local_parent": str(tmp / "KGS"), "no_browser": True})
    assert res["scope"] == "workspace" and res["mode"] == "workspace" and "root_mode=workspace" in res["login_url"]
    assert login_argv(d.hub)[0][login_argv(d.hub)[0].index("--root-mode") + 1] == "workspace"
    await until(lambda: d.hub.filehub.login_state.get("state") == "done")
    fh = d.hub.filehub
    [f] = fh.folders()
    local = (tmp / "KGS" / "admin harness").resolve()
    assert f["kind"] == "workspace" and f["name"] == "admin harness" and f["root_id"] == f["ws_id"] and f["account"] == "chu.domain"
    assert f["local_path"] == str(local) and fh.login_state["account"] == "chu.domain"
    await until(lambda: (fh.read_status() or {}).get("roots"))
    a = [x for x in argv_log(d.hub) if "--profile" in x["argv"]][0]["argv"]
    assert a[a.index("--add-root") + 1] == f["root_id"] and a[a.index("--root-name") + 1] == "admin harness"
    assert a[a.index("--root-kind") + 1] == "workspace"  # S5: gốc workspace BẮT BUỘC --root-kind workspace
    (local / "v1_2026-11-15").mkdir()
    (local / "v1_2026-11-15" / "a.md").write_text("x")
    (local / "o-goc.md").write_text("tệp ở gốc")
    st = await until(lambda: (lambda s: s if s["folders"][0]["root_files"] else None)(fh.status()))
    row = st["folders"][0]
    assert row["display"] == "Toàn bộ workspace «admin harness»" and row["account"] == "chu.domain" and row["root_files"] == ["o-goc.md"]
    assert row["url"] == "https://kgs.example.test/files" and not row["renamed"]
    await until(lambda: "v1_2026-11-15/a.md" in (fh.read_index(f["root_id"]).get("items") or {}))
    assert "o-goc.md" not in fh.read_index(f["root_id"])["items"]
    assert fh.link_for(str(local / "o-goc.md"))["url"] is None  # tệp ở gốc: không có trang trên web
    # đổi tên workspace trên web → status.server_name mới; tên cfharness + local_path giữ; tên mới khớp như bí danh
    (fh.profile_dir / "server_names.json").write_text(json.dumps({f["root_id"]: "Admin Harness 2026"}))
    await until(lambda: fh.status()["folders"][0]["renamed"])
    assert fh.folders()[0]["local_path"] == str(local)
    assert fh.resolve_ref("filehub:Admin Harness 2026/v1_2026-11-15")[0] == str(local / "v1_2026-11-15")
    assert fh.resolve_ref("filehub:admin harness/v1_2026-11-15")[0] == str(local / "v1_2026-11-15")
    lst = await rpc(d, "filehub.list")
    assert lst[0]["kind"] == "workspace" and lst[0]["server_name"] == "Admin Harness 2026" and "Admin Harness 2026" in lst[0]["note"]


async def test_old_binary_three_column_root_still_works(env):
    """Binary CŨ (trước S5): dòng ROOT 3 cột vẫn đọc được (kind=folder); chế độ workspace → báo cập nhật binary, không đổi cấu hình."""
    make, tmp = env
    d = await make()
    b = fake_bin(tmp / "bin", {"FAKE_TT_OLD": "1"})
    with pytest.raises(HubError, match="chưa hỗ trợ chế độ workspace"):
        await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "local_parent": str(tmp / "KGS"), "no_browser": True})
    assert not d.hub.filehub.folders()
    await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "scope": "folder", "local_parent": str(tmp / "KGS"),
                                   "no_browser": True})
    await until(lambda: d.hub.filehub.login_state.get("state") == "done")
    [f] = d.hub.filehub.folders()
    assert f["kind"] == "folder" and f["name"] == "cfharness" and "account" not in f
    assert d.hub.filehub.login_state["roots"][0] == {"id": f["root_id"], "ws_id": "ws_1", "name": "cfharness", "kind": "folder", "account": ""}


async def test_root_line_4_cols_and_empty_account(env):
    """S5 luôn in đủ 4 tab; server cũ → account rỗng (`ROOT r1\tw9\tcfharness\tfolder\t`). Dòng 4 cột cũng nhận."""
    make, tmp = env
    for cols, acc in (("5", ""), ("4", "x")):
        d = await make()
        b = fake_bin(tmp / f"bin{cols}", {"FAKE_TT_ACCOUNT": acc, "FAKE_TT_ROOT_COLS": cols})
        await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "scope": "folder", "local_parent": str(tmp / f"K{cols}"),
                                       "no_browser": True})
        await until(lambda: d.hub.filehub.login_state.get("state") == "done")
        [f] = d.hub.filehub.folders()
        assert f["kind"] == "folder" and "account" not in f, f
        await d.hub.filehub.stop()


def test_parse_root_lines_4_and_5_columns():
    from cfharness.filehub.service import kind_of, safe_name

    assert safe_name("admin/harness: 2026") == "admin-harness- 2026" and safe_name("...") == "workspace"
    assert kind_of({"kind": "workspace"}) == "workspace" and kind_of({}) == "folder" and kind_of({"kind": "lạ"}) == "folder"


async def test_relay_login_state_checked_single_use(env):
    """Relay OAuth (web public): binary chạy với --redirect-uri <public_origin>/filehub/oauth/cb, stdin PIPE; callback sai state → lỗi
    CHUNG, lượt chờ giữ nguyên; đúng → ghi `CODE <code>\\t<state>` vào stdin → đăng nhập xong; state dùng lại → lỗi. code không vào audit."""
    from cfharness.filehub.service import RELAY_PATH

    make, tmp = env
    d = await make()
    b = fake_bin(tmp / "bin", {"FAKE_TT_WS_NAME": "admin harness"})
    with pytest.raises(HubError, match="web.public_origin"):
        await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "local_parent": str(tmp / "KGS"), "relay": True})
    d.hub.config.set("web.public_origin", "https://an.example.com")
    res = await rpc(d, "filehub.login", {"relay": True, "mode": "workspace"})
    argv = login_argv(d.hub)[-1]
    assert argv[argv.index("--redirect-uri") + 1] == "https://an.example.com" + RELAY_PATH and "--no-browser" in argv
    fh = d.hub.filehub
    state = fh._relay["state"]
    assert f"state={state}" in res["login_url"] and fh.status()["login"]["relay"] is True
    for bad in ({"code": "c1", "state": "x" * 32}, {"code": "c1", "state": state + "\nCODE"}, {"code": "c1"}):
        with pytest.raises(HubError, match="không hợp lệ hoặc đã hết hạn"):
            await rpc(d, "filehub.login_callback", dict(bad))
    assert fh._relay is not None and fh._login_proc.returncode is None  # kẻ dò không làm hỏng lượt chờ thật
    r = await rpc(d, "filehub.login_callback", {"code": "the-code_123", "state": state})
    assert r == {"ok": True, "state": "done", "error": None, "message": None}
    assert (fh.tokens_path.parent / "relay_code.txt").read_text() == "the-code_123"
    assert fh.folders()[0]["kind"] == "workspace"
    with pytest.raises(HubError, match="không hợp lệ hoặc đã hết hạn"):  # một lần
        await rpc(d, "filehub.login_callback", {"code": "the-code_123", "state": state})
    rows = d.hub.audit.query(kind="filehub.login_callback")
    assert rows and "the-code_123" not in json.dumps(rows) and state not in json.dumps(rows)


async def test_relay_expired_and_error_and_bad_code(env):
    make, tmp = env
    d = await make()
    configure(d.hub, tmp)
    d.hub.config.set("web.public_origin", "https://an.example.com")
    fh = d.hub.filehub
    # hết hạn → lỗi chung, lượt bị huỷ
    await rpc(d, "filehub.login", {"relay": True})
    st = fh._relay["state"]
    fh._relay["expires"] = 0
    with pytest.raises(HubError, match="hết hạn"):
        await rpc(d, "filehub.login_callback", {"code": "c", "state": st})
    await until(lambda: fh.login_state.get("state") == "error")
    assert fh.login_state["error"] == "timeout"
    # trang đồng ý trả lỗi (danh sách trắng; chữ lạ → other)
    await rpc(d, "filehub.login", {"relay": True})
    st = fh._relay["state"]
    r = await rpc(d, "filehub.login_callback", {"error": "<script>", "state": st})
    assert r["ok"] is False and r["error"] == "other"  # chữ lạ → `ERROR other\t<state>` cho binary
    d.hub.config.set("filehub.folders", [{**fh.folders()[0], "account": "chu.domain"}])
    await rpc(d, "filehub.login", {"relay": True})
    r = await rpc(d, "filehub.login_callback", {"error": "not_ws_admin", "state": fh._relay["state"]})
    # tài khoản vừa đồng ý không biết được → không đổ lỗi cho tài khoản hub đang dùng, chỉ nêu làm ngữ cảnh
    assert r["error"] == "not_ws_admin" and "vừa dùng" in r["message"] and "Workspace Admin" in r["message"]
    assert "Tài khoản «chu.domain»" not in r["message"] and "Hub hiện đăng nhập bằng «chu.domain»" in r["message"]
    assert "«chu.domain»" in fh.status()["login"]["message"]
    await rpc(d, "filehub.login", {"relay": True})
    r = await rpc(d, "filehub.login_callback", {"error": "super_not_member", "state": fh._relay["state"]})
    assert "Super Admin" in r["message"] and "Hub hiện đăng nhập bằng «chu.domain»" in r["message"]
    # code có ký tự xuống dòng/tab (chèn thêm dòng vào stdin của binary) → từ chối, không ghi gì vào stdin
    await rpc(d, "filehub.login", {"relay": True})
    r = await rpc(d, "filehub.login_callback", {"code": "a\nCODE b", "state": fh._relay["state"]})
    assert r["error"] == "invalid_code" and not (fh.tokens_path.parent / "relay_code.txt").exists()


async def test_switch_to_workspace_keeps_old_files(env, monkeypatch):
    """`switch-to-workspace`: đăng nhập chế độ workspace → gỡ thư mục cũ khỏi cấu hình (tệp giữ) → thêm gốc workspace ở bản sao MỚI;
    có run giữ khoá → từ chối; đã là workspace → từ chối; audit."""
    make, tmp = env
    d, old = await _started(make, tmp, env={"FAKE_TT_WS_NAME": "chung"})  # cố ý trùng tên với thư mục cũ → bản sao mới phải khác
    (old / "giu-lai.txt").write_text("x")
    fh = d.hub.filehub
    monkeypatch.setattr(d.hub.runs.locks, "holder", lambda p: {"run_id": "run_x"})
    with pytest.raises(HubError, match="đang có run"):
        await rpc(d, "filehub.switch_to_workspace", {"no_browser": True})
    monkeypatch.undo()
    res = await rpc(d, "filehub.switch_to_workspace", {"no_browser": True})
    assert res["mode"] == "workspace" and res["switch_from"] == [str(old)]
    await until(lambda: fh.login_state.get("state") in ("done", "error"))
    assert fh.login_state["state"] == "done", fh.login_state
    [f] = fh.folders()
    assert f["kind"] == "workspace" and f["local_path"] != str(old) and not f["local_path"].startswith(str(old) + "/")
    assert (old / "giu-lai.txt").exists() and fh.status()["login"]["switched_from"] == [str(old)]
    assert fh.status()["can_switch"] is False
    with pytest.raises(HubError, match="đã ở chế độ"):
        await rpc(d, "filehub.switch_to_workspace", {})
    assert [r["kind"] for r in d.hub.audit.query(kind="filehub.switch_to_workspace")]
    await until(lambda: any(r.get("id") == f["root_id"] for r in (fh.read_status() or {}).get("roots") or []))


async def test_switch_server_without_workspace_keeps_old_config(env):
    make, tmp = env
    d, old = await _started(make, tmp, env={"FAKE_TT_DENY_WORKSPACE": "1"})
    before = d.hub.filehub.folders()
    await rpc(d, "filehub.switch_to_workspace", {"no_browser": True})
    await until(lambda: d.hub.filehub.login_state.get("state") == "error")
    assert "chưa cấp gốc workspace" in d.hub.filehub.login_state["error"] and d.hub.filehub.folders() == before


async def test_remote_setup_cannot_change_server_or_pick_writable_binary(env):
    make, tmp = env
    d = await make()
    configure(d.hub, tmp)
    d.hub.config.set("filehub.sync_binary", None)
    b = fake_bin(tmp / "bin2")  # người dùng của hub ghi được → không nhận qua link public
    with pytest.raises(HubError, match="link public"):
        await rpc(d, "filehub.setup", {"server": "https://khac.example.test", "binary": str(b), "remote": True})
    with pytest.raises(HubError, match="do quản trị cài"):
        await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(b), "remote": True})
    assert d.hub.config.get("filehub.server") == "https://kgs.example.test" and d.hub.config.get("filehub.sync_binary") is None


def test_cli_parser_switch_and_scope():
    from cfharness.cli import build_parser

    p = build_parser()
    a = p.parse_args(["filehub", "switch-to-workspace", "--local-path", "/x", "--no-wait"])
    assert a.fh_action == "switch-to-workspace" and a.local_path == "/x"
    assert p.parse_args(["filehub", "setup", "--scope", "folder"]).scope == "folder"
