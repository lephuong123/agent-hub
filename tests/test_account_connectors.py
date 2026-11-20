"""Mỗi tài khoản Claude một connector claude.ai riêng (docs/dev-notes/manager-connectors.md — "Mỗi tài khoản một connector riêng").

Tài khoản token có thư mục cấu hình claude riêng `HUB_HOME/claude-config/<id>/` (CLAUDE_CONFIG_DIR) giữ phiên claude.ai của nó;
`machine` dùng cấu hình mặc định. Kiểm: đăng nhập/đăng xuất/trạng thái theo tài khoản, cache cho `account.list`, symlink dùng chung,
xoá tài khoản xoá thư mục, run có connector chọn phiên theo TÀI KHOẢN ĐANG DÙNG (không mượn phiên tài khoản khác), bảo vệ thư mục
trước agent, sao lưu/gói chẩn đoán. Không gọi claude thật: `tests/fake_claude_auth.py` (đọc CLAUDE_CONFIG_DIR) + ScriptedRuntime.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from cfharness import auth, backup
from cfharness.accounts import claude_config
from cfharness.errors import HubError
from cfharness.runtime import claude_cli
from m7_fake import MgrScript, close, make_daemon, rpc, wait_status

FAKE = str(Path(__file__).with_name("fake_claude_auth.py"))
GOOD = "goodcode#state123"
TOK_A = "sk-ant-oat01-" + "Aa1_" * 20
TOK_B = "sk-ant-oat01-" + "Bb2_" * 20
TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="claude giả là script shebang (POSIX)")


class HoldScript(MgrScript):
    def __call__(self, spec):
        self.specs.append(spec)
        return {"text": "ok", "cost": 0.001, "hold": True}


@pytest.fixture(autouse=True)
def _os_sandbox(monkeypatch):
    monkeypatch.setattr("cfharness.manager.sandbox_gate.os_sandbox_available", lambda: True)


@pytest.fixture
async def d(hub_home, tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith(("FAKE_SETUP_", "FAKE_AUTH_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("FAKE_AUTH_STATE", str(tmp_path / "machine.login"))
    monkeypatch.setenv("FAKE_AUTH_TOKEN_SEEN", str(tmp_path / "token.seen"))
    monkeypatch.setenv("FAKE_AUTH_LOG", str(tmp_path / "auth.log"))
    uh = tmp_path / "userhome"
    (uh / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(uh))
    dm = await make_daemon(hub_home, HoldScript())
    dm.hub.config.set("runtime.claude.binary", FAKE)
    claude_cli._login_cache.clear()
    yield dm
    await dm.hub.accounts.close_all_logins()
    await close(dm)
    claude_cli._login_cache.clear()


def _two(d):
    a = d.hub.accounts.add_token("A", TOK_A)["id"]
    b = d.hub.accounts.add_token("B", TOK_B)["id"]
    return a, b


def _claude_home() -> Path:
    return Path(os.environ["CFHARNESS_TEST_CLAUDE_HOME"])


# ---------------------------------------------------------------- thư mục cấu hình + symlink
def test_config_dir_layout_and_symlinks(hub_home):
    home = hub_home.home
    ch = _claude_home()
    (ch / "settings.json").write_text("{}", encoding="utf-8")
    assert claude_config.account_dir(home, "machine") is None and claude_config.account_dir(home, "../x") is None
    d = claude_config.ensure(home, "a_0123abcd")
    assert claude_config.shared(d)
    assert d == home / "claude-config" / "a_0123abcd"
    if os.name == "posix":
        assert (d.stat().st_mode & 0o777) == 0o700 and ((home / "claude-config").stat().st_mode & 0o777) == 0o700
    assert os.readlink(d / "projects") == str(ch / "projects") and (ch / "projects").is_dir()
    assert os.readlink(d / "settings.json") == str(ch / "settings.json")
    assert not os.path.lexists(d / "CLAUDE.md")  # bản mặc định không có → không symlink
    assert not os.path.lexists(d / ".claude.json")  # KHÔNG BAO GIỜ symlink cấu hình/credentials
    # idempotent + sửa symlink sai đích
    os.unlink(d / "projects")
    os.symlink("/tmp", d / "projects")
    claude_config.ensure(home, "a_0123abcd")
    assert os.readlink(d / "projects") == str(ch / "projects")
    # xoá: chỉ thư mục đó, không đi theo symlink (session dùng chung còn nguyên)
    (ch / "projects" / "keep.jsonl").write_text("x", encoding="utf-8")
    assert claude_config.remove(home, "a_0123abcd") and not d.exists()
    assert (ch / "projects" / "keep.jsonl").exists() and (ch / "settings.json").exists()
    with pytest.raises(HubError):
        claude_config.ensure(home, "machine")


def test_remove_refuses_symlinked_dir(hub_home, tmp_path):
    home = hub_home.home
    (home / "claude-config").mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f").write_text("x", encoding="utf-8")
    os.symlink(target, home / "claude-config" / "a_deadbeef")
    with pytest.raises(HubError):
        claude_config.remove(home, "a_deadbeef")
    assert (target / "f").exists()


# ---------------------------------------------------------------- đăng nhập / trạng thái / đăng xuất theo tài khoản
@needs_pty
async def test_two_accounts_login_separately(d, tmp_path):
    a, b = _two(d)
    home = d.hub.paths.home
    st = await rpc(d, "account.claudeai_status", {"id": a})
    assert st["id"] == a and st["logged_in"] is False
    assert not (home / "claude-config" / a).exists()  # kiểm trạng thái không tạo thư mục

    lg = await rpc(d, "account.claudeai_login_start", {"id": a})
    assert lg["account_id"] == a and lg["account_label"] == "A"
    assert (await rpc(d, "account.claudeai_status", {}))["login"]["account_id"] == a  # web: hộp chờ nói rõ tài khoản
    st = await rpc(d, "account.claudeai_login_finish", {"login_id": lg["login_id"], "code": GOOD})
    assert st["logged_in"] is True and st["id"] == a and st["email"] == f"{a}@example.com"
    assert (home / "claude-config" / a / ".fake_login").exists()
    assert not (tmp_path / "machine.login").exists()  # phiên mặc định (machine) không bị đụng
    assert os.path.islink(home / "claude-config" / a / "projects")

    lg = await rpc(d, "account.claudeai_login_start", {"id": b})
    await rpc(d, "account.claudeai_login_finish", {"login_id": lg["login_id"], "code": GOOD})
    assert not (tmp_path / "token.seen").exists()  # không bao giờ mang token hub khi đăng nhập/kiểm phiên claude.ai

    lst = {x["id"]: x for x in (await rpc(d, "account.list", {}))["accounts"]}
    assert lst[a]["claudeai"] == {"logged_in": True, "email": f"{a}@example.com"}
    assert lst[b]["claudeai"]["logged_in"] is True
    assert lst["machine"]["claudeai"] == {"logged_in": False, "email": None}  # status không id ở trên đã kiểm phiên của máy
    st = await rpc(d, "account.claudeai_status", {})  # không id = machine + cache mọi tài khoản
    assert st["id"] == "machine" and st["logged_in"] is False and st["accounts"][a]["logged_in"] is True
    log_lines = (tmp_path / "auth.log").read_text().splitlines()
    assert f"auth status {home / 'claude-config' / a}" in log_lines and "auth status -" in log_lines

    # đăng xuất A không đụng B
    st = await rpc(d, "account.claudeai_logout", {"id": a})
    assert st["logged_in"] is False and (home / "claude-config" / b / ".fake_login").exists()
    lst = {x["id"]: x for x in (await rpc(d, "account.list", {}))["accounts"]}
    assert lst[a]["claudeai"]["logged_in"] is False and lst[b]["claudeai"]["logged_in"] is True
    ev = d.hub.store.query_events(types=["account.claudeai_login"])
    assert [(e.payload["id"], e.payload["logged_in"]) for e in ev] == [(a, True), (b, True), (a, False)]

    # xoá B: đăng xuất trước (best-effort) rồi xoá thư mục của nó — chỉ thư mục đó
    await rpc(d, "account.rm", {"id": b})
    assert not (home / "claude-config" / b).exists() and (home / "claude-config" / a).is_dir()
    assert f"auth logout {home / 'claude-config' / b}" in (tmp_path / "auth.log").read_text().splitlines()
    assert b not in (await rpc(d, "account.claudeai_status", {}))["accounts"]
    rows = [r[0] for r in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind LIKE 'account.%'")]
    assert rows and not any(GOOD in (x or "") or TOK_A in (x or "") for x in rows)


@needs_pty
async def test_token_login_finish_refuses_claudeai_login(d):
    a, _ = _two(d)
    lg = await rpc(d, "account.claudeai_login_start", {"id": a})
    with pytest.raises(HubError):
        await rpc(d, "account.login_finish", {"login_id": lg["login_id"], "code": GOOD})


async def test_check_refreshes_connector_cache(d):
    a, _ = _two(d)
    claude_config.ensure(d.hub.paths.home, a)
    (d.hub.paths.home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    await rpc(d, "account.check", {"id": a})
    lst = {x["id"]: x for x in (await rpc(d, "account.list", {}))["accounts"]}
    assert lst[a]["claudeai"]["logged_in"] is True


async def test_custom_and_unknown_ids_rejected(d):
    with pytest.raises(HubError):
        await rpc(d, "account.claudeai_login_start", {"id": "custom"})
    with pytest.raises(HubError):
        await rpc(d, "account.claudeai_status", {"id": "a_ffffffff"})
    assert "account.claudeai_status" in auth.HUMAN_ONLY and "account.claudeai_status" not in auth.MANAGER_ALLOWED


# ---------------------------------------------------------------- run có connector theo tài khoản đang dùng
def _patch_login(d, monkeypatch, *, machine: bool):
    seen: list = []

    async def login_available(self, config_dir=None):
        seen.append(config_dir)
        if config_dir is None:
            return machine
        return (Path(config_dir) / ".fake_login").exists()
    monkeypatch.setattr(type(d.hub.runs.runtime()), "login_available", login_available, raising=False)
    return seen


async def _run(d, connectors, agent="w1"):
    d.hub.agents.create({"id": agent, "name": agent, "model": "haiku", "tool_policy": {"tools": TOOLS, "connectors": connectors}})
    res = await rpc(d, "run.start", {"agent": agent, "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    await wait_status(d, run_id, ("running",))
    spec = [s for s in d.script.specs if s.run_id == run_id][-1]
    fb = d.hub.store.query_events(run_id=run_id, types=["connector.auth_fallback"])
    return spec, fb


async def test_run_uses_active_account_session(d, monkeypatch):
    a, b = _two(d)
    home = d.hub.paths.home
    claude_config.ensure(home, a)
    (home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    seen = _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use(a)
    spec, fb = await _run(d, ["Google Drive"], "w1")
    assert spec.auth_mode == "login" and spec.claude_config_dir == home / "claude-config" / a and not fb
    assert seen[-1] == home / "claude-config" / a
    # env của run: CLAUDE_CONFIG_DIR = thư mục của A, không token nào
    from cfharness.config import DEFAULTS
    from cfharness.runtime.claude_cli import OAUTH_TOKEN_SECRET, ClaudeCliRuntime

    env = ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=d.hub.secrets)._run_env(spec)
    assert env["CLAUDE_CONFIG_DIR"] == str(home / "claude-config" / a) and OAUTH_TOKEN_SECRET not in env

    # đổi sang B (chưa gắn connector) → token của B + fallback, KHÔNG mượn phiên của A hay của máy
    d.hub.accounts.use(b)
    spec, fb = await _run(d, ["Google Drive"], "w2")
    assert spec.auth_mode == "token" and spec.claude_config_dir is None
    assert [(e.payload["reason"], e.payload.get("account")) for e in fb] == [("account_no_claudeai", b)]
    assert not (home / "claude-config" / b).exists()  # kiểm phiên không tạo thư mục
    env = ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=d.hub.secrets)._run_env(spec)
    assert env[OAUTH_TOKEN_SECRET] == TOK_B and "CLAUDE_CONFIG_DIR" not in env


async def test_machine_active_uses_default_login(d, monkeypatch):
    _two(d)
    _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use("machine")
    spec, fb = await _run(d, ["Google Drive"])
    assert spec.auth_mode == "login" and spec.claude_config_dir is None and not fb


async def test_machine_without_login_falls_back(d, monkeypatch):
    _patch_login(d, monkeypatch, machine=False)
    spec, fb = await _run(d, ["Google Drive"])
    assert spec.auth_mode == "token" and [e.payload["reason"] for e in fb] == ["no_claude_ai_login"]


async def test_run_without_connectors_untouched(d, monkeypatch):
    a, _ = _two(d)
    claude_config.ensure(d.hub.paths.home, a)
    (d.hub.paths.home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use(a)
    spec, fb = await _run(d, None)
    assert spec.auth_mode == "token" and spec.claude_config_dir is None and spec.connectors is None and not fb


async def test_auto_connectors_follow_active_account(d, monkeypatch):
    monkeypatch.delenv("CFHARNESS_CONNECTORS_AUTO")
    a, b = _two(d)
    claude_config.ensure(d.hub.paths.home, a)
    (d.hub.paths.home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use(b)  # B chưa có phiên → không tự bật connector (dù máy và A có phiên)
    spec, fb = await _run(d, None, "w1")
    assert spec.connectors is None and spec.auth_mode == "token" and not fb
    d.hub.accounts.use(a)
    spec, fb = await _run(d, None, "w2")
    assert spec.connectors == ["*"] and spec.auth_mode == "login" and spec.claude_config_dir == d.hub.paths.home / "claude-config" / a


async def test_forced_login_uses_active_account_dir(d, monkeypatch):
    a, _ = _two(d)
    _patch_login(d, monkeypatch, machine=False)
    d.hub.accounts.use(a)
    d.hub.config.set("runtime.claude.connectors_auth", "login")
    spec, _ = await _run(d, ["Google Drive"])
    assert spec.auth_mode == "login" and spec.claude_config_dir == d.hub.paths.home / "claude-config" / a
    assert os.path.islink(spec.claude_config_dir / "projects")


async def test_rm_and_logout_refused_while_run_uses_session(d, monkeypatch):
    a, b = _two(d)
    home = d.hub.paths.home
    claude_config.ensure(home, a)
    (home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use(a)
    spec, _ = await _run(d, ["Google Drive"])
    assert spec.auth_mode == "login" and d.hub.runs.active[spec.run_id].claude_session == a
    d.hub.accounts.use(b)  # đổi tài khoản: run cũ vẫn chạy bằng thư mục của A
    for m in ("account.rm", "account.claudeai_logout"):
        with pytest.raises(HubError) as ei:
            await rpc(d, m, {"id": a})
        assert "đang chạy bằng connector" in str(ei.value) and spec.run_id in str(ei.value)
    assert (home / "claude-config" / a / ".fake_login").exists() and d.hub.accounts._row(a) is not None
    await rpc(d, "run.stop", {"id": spec.run_id})
    await wait_status(d, spec.run_id, ("cancelled", "completed", "failed", "interrupted"))
    for _ in range(100):
        if spec.run_id not in d.hub.runs.active:
            break
        await asyncio.sleep(0.05)
    await rpc(d, "account.rm", {"id": a})
    assert not (home / "claude-config" / a).exists()


async def test_machine_session_logout_refused_while_in_use(d, monkeypatch):
    _patch_login(d, monkeypatch, machine=True)
    spec, _ = await _run(d, ["Google Drive"])
    assert d.hub.runs.active[spec.run_id].claude_session == "machine"
    with pytest.raises(HubError, match="đang chạy bằng connector"):
        await rpc(d, "account.claudeai_logout", {})


async def test_unshared_config_dir_falls_back(d, monkeypatch):
    a, _ = _two(d)
    home = d.hub.paths.home
    claude_config.ensure(home, a)
    (home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    _patch_login(d, monkeypatch, machine=True)
    d.hub.accounts.use(a)
    monkeypatch.setattr(claude_config, "_link", lambda link, target: None)  # như Windows không tạo được symlink
    os.unlink(home / "claude-config" / a / "projects")
    (home / "claude-config" / a / "projects").mkdir()
    spec, fb = await _run(d, ["Google Drive"])
    assert spec.auth_mode == "token" and spec.claude_config_dir is None
    assert [(e.payload["reason"], e.payload.get("account")) for e in fb] == [("config_dir_unshared", a)]
    assert d.hub.runs.active[spec.run_id].claude_session is None


async def test_forced_login_custom_and_dir_error_fall_back(d, monkeypatch):
    a, _ = _two(d)
    _patch_login(d, monkeypatch, machine=True)
    d.hub.config.set("runtime.claude.connectors_auth", "login")
    # custom (token tự đặt): không mượn phiên của máy dù đang ép login
    d.hub.secrets.set("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "Cc3_" * 20)
    assert d.hub.accounts.active_id() == "custom"
    spec, fb = await _run(d, ["Google Drive"], "w1")
    assert spec.auth_mode == "token" and [(e.payload["reason"], e.payload.get("account")) for e in fb] == [("account_no_claudeai", "custom")]
    # dựng thư mục lỗi → fallback có lý do, run vẫn chạy
    d.hub.accounts.use(a)

    def boom(aid):
        raise OSError("disk")
    monkeypatch.setattr(d.hub.accounts, "ensure_config_dir", boom)
    spec, fb = await _run(d, ["Google Drive"], "w2")
    assert spec.auth_mode == "token" and [e.payload["reason"] for e in fb] == ["config_dir_error"]


async def test_active_account_read_once_per_start(d, monkeypatch):
    """`account.use` giữa lượt khởi động: các bước dùng chung MỘT tài khoản (đọc một lần)."""
    a, b = _two(d)
    home = d.hub.paths.home
    claude_config.ensure(home, a)
    (home / "claude-config" / a / ".fake_login").write_text("1", encoding="utf-8")
    d.hub.accounts.use(a)
    calls = []
    orig = d.hub.runs._connector_account

    def once():
        calls.append(1)
        return orig()
    monkeypatch.setattr(d.hub.runs, "_connector_account", once)

    async def login_available(self, config_dir=None):
        d.hub.accounts.use(b)  # người đổi tài khoản đúng lúc hub đang kiểm phiên
        return config_dir is not None and (Path(config_dir) / ".fake_login").exists()
    monkeypatch.setattr(type(d.hub.runs.runtime()), "login_available", login_available, raising=False)
    spec, fb = await _run(d, ["Google Drive"])
    assert len(calls) == 1 and spec.auth_mode == "login" and spec.claude_config_dir == home / "claude-config" / a and not fb


def test_run_setup_reads_account_dir(tmp_path):
    from cfharness.manager.connectors import run_setup

    uh = tmp_path / "u"
    (uh / ".claude").mkdir(parents=True)
    (uh / ".claude.json").write_text('{"mcpServers": {"pw": {}}}', encoding="utf-8")
    cd = tmp_path / "cfg"
    cd.mkdir()
    (cd / ".claude.json").write_text('{"mcpServers": {"acct": {}}}', encoding="utf-8")
    (cd / "settings.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    r = run_setup(("*",), home=uh, config_dir=cd)
    assert r["allowed_mcp"] == ["acct", "pw"] and r["allowed_hooks"] == ["Stop"]


# ---------------------------------------------------------------- bảo vệ thư mục + sao lưu/xuất/chẩn đoán
def test_claude_config_protected_from_agents(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import HUB_WRITE_DENY_NAMES, sandbox_settings, secret_read_deny_rules
    from cfharness.pathmap import SCAN_SKIP_DIRS
    from cfharness.support_bundle import NEVER_INCLUDED

    home = hub_home.home
    assert "claude-config" in backup.EXCLUDED and not backup.included(PurePosixPath("claude-config/a_0123abcd/.claude.json"))
    assert "claude-config" in SCAN_SKIP_DIRS and "claude-config" in HUB_WRITE_DENY_NAMES and "claude-config/" in NEVER_INCLUDED
    from cfharness.hub_transfer import MACHINE_STATE

    assert "accounts.claudeai" in MACHINE_STATE  # cache phiên claude.ai theo tài khoản gắn với máy (thư mục không đi theo gói)
    rules = secret_read_deny_rules(home, hub_home.socket)
    for tool in ("Read", "Grep", "Glob"):
        assert any("claude-config" in r and r.startswith(f"{tool}(") for r in rules)
    fs = sandbox_settings(home, hub_home.socket, {"enabled": True})["filesystem"]
    assert any(str(x).endswith("/claude-config") for x in fs["denyRead"])
    assert any("claude-config" in str(x) for x in fs["denyWrite"])
    ps = PolicyStore(home)
    ps.ensure()
    ctx = GuardContext(home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    cc = f"{home}/claude-config/a_0123abcd"
    for tool, inp in (("Read", {"file_path": f"{cc}/.claude.json"}), ("Read", {"file_path": f"{cc}/.credentials.json"}),
                      ("Grep", {"pattern": "token", "path": f"{home}/claude-config"}), ("Glob", {"pattern": "**", "path": cc}),
                      ("Bash", {"command": f"cat {cc}/.credentials.json"}), ("Bash", {"command": f"tar czf /tmp/x.tgz {home}/claude-config"}),
                      ("Bash", {"command": f"rm -rf {home}/claude-config"}),
                      ("Bash", {"command": "cfharness account connector a_0123abcd login"})):
        assert classify(tool, inp, ctx).forbidden, (tool, inp)
    # ghi bằng tool Write: như secrets/ — tài nguyên được bảo vệ (hỏi người) + sandbox denyWrite ở lớp OS
    for p in (f"{cc}/.claude.json", f"{home}/secrets/X"):
        v = classify("Write", {"file_path": p, "content": "{}"}, ctx)
        assert v.forbidden or v.verdict == "destructive", v


# ---------------------------------------------------------------- hub doctor
@needs_pty
def test_doctor_counts_auto_and_probes_active_account(tmp_path, monkeypatch):
    import json
    import sqlite3

    from cfharness import hub_doctor

    assert hub_doctor._connector_users({"connectors": {"auto": True}}, None) == ["(connectors.auto)"]
    assert hub_doctor._connector_users({"connectors": {"auto": False}}, None) == []
    home = tmp_path / "hub"
    home.mkdir()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE hub_state (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO hub_state VALUES ('claude_account_active', ?)", (json.dumps("a_0123abcd"),))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("FAKE_AUTH_STATE", str(tmp_path / "machine.login"))
    (tmp_path / "machine.login").write_text("1", encoding="utf-8")  # máy có phiên — nhưng tài khoản đang dùng thì chưa
    cfg = {"connectors": {"auto": True}}

    def check():
        ck = hub_doctor._Checks()
        hub_doctor._connectors(ck, cfg, c, probe=True, path=FAKE, tok=True, home=home)
        return next(x for x in ck.items if x["id"] == "claude_connectors")

    r = check()
    assert r["ok"] is False and "a_0123abcd" in r["detail"]
    claude_config.ensure(home, "a_0123abcd")
    (home / "claude-config" / "a_0123abcd" / ".fake_login").write_text("1", encoding="utf-8")
    assert check()["ok"] is True
    os.unlink(home / "claude-config" / "a_0123abcd" / "projects")
    (home / "claude-config" / "a_0123abcd" / "projects").mkdir()
    r = check()
    assert r["ok"] is False and "không dùng chung session" in r["detail"]
    c.execute("UPDATE hub_state SET value=? WHERE key='claude_account_active'", (json.dumps("machine"),))
    assert check()["ok"] is True  # máy: phiên mặc định
