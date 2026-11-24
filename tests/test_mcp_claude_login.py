"""MCP do claude quản lý theo tài khoản — thêm / gỡ / đăng nhập / đăng xuất / trạng thái (docs/dev-notes/plan-mcp-claude-native.md PR3).

Service `hub.mcp_claude` (mcp_client/claude_admin.py) chạy `claude mcp …` với CLAUDE_CONFIG_DIR của ĐÚNG tài khoản; RPC `mcp.claude_*`
(HUMAN_ONLY, ghi bọc audit danh sách trắng), CLI `cfharness mcp claude …`, web (mục "MCP của tài khoản" trên hàng tài khoản), doctor.
Không gọi claude thật: `tests/fake_claude_mcp.py` (trạng thái theo CLAUDE_CONFIG_DIR, login qua PTY thật).
Kiểm cả: secret (header, URL chuyển hướng, url) không bao giờ vào argv / audit / thông báo lỗi / kết quả; URL chuyển hướng sai → lỗi
không nhắc lại URL; cô lập theo tài khoản.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from cfharness import auth, hub_doctor
from cfharness.errors import HubError
from cfharness.mcp_client import claude_source
from cfharness.mcp_client.claude_admin import CACHE_KEY, parse_list
from cfharness.mcp_server.rpc import MUTATING
from cfharness.web.app import ACTIONS  # noqa: F401 — nạp app (đăng ký thao tác)
from m7_fake import MgrScript, close, make_daemon, rpc
from test_web_accounts import env, get, post, section  # noqa: F401 — fixture + helper web

FAKE = str(Path(__file__).with_name("fake_claude_mcp.py"))
TOK_A = "sk-ant-oat01-" + "Aa1_" * 20
TOK_B = "sk-ant-oat01-" + "Bb2_" * 20
HDR = "Bearer hdr-SECRET-0123456789abcdef"
URL_SECRET = "QS-SECRET-77"
GOOD_REDIRECT = "http://localhost:51814/callback?code=CODE-SECRET-123&state=FAKESTATE"
BAD_REDIRECT = "http://localhost:51814/callback?code=BADCODE-SECRET-999&state=WRONG"
SECRETS = (HDR, "hdr-SECRET", URL_SECRET, "CODE-SECRET-123", "BADCODE-SECRET-999", TOK_A, TOK_B)

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="claude giả là script shebang (POSIX)")


@pytest.fixture
async def d(hub_home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("FAKE_MCP_LOGIN_MODE", raising=False)
    (tmp_path / "claude-default").mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(tmp_path / "claude-default"))  # cấu hình mặc định riêng mỗi test
    monkeypatch.setenv("FAKE_MCP_LOG", str(tmp_path / "mcp.log"))
    # env daemon "bẩn" như khi chạy trong phiên Claude Code / có token — mọi thứ phải bị gỡ khỏi tiến trình `claude mcp`
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-DAEMONENV" + "x" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-DAEMONENV")
    dm = await make_daemon(hub_home, MgrScript())
    dm.hub.config.set("runtime.claude.binary", FAKE)
    yield dm
    await dm.hub.mcp_claude.close_all()
    await close(dm)


def _log(tmp_path: Path) -> list[dict]:
    p = tmp_path / "mcp.log"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _two(d):
    a = d.hub.accounts.add_token("A", TOK_A)["id"]
    b = d.hub.accounts.add_token("B", TOK_B)["id"]
    return a, b


def _default_home() -> Path:
    return Path(os.environ["CFHARNESS_TEST_CLAUDE_HOME"])


def _audit_rows(d, prefix: str = "mcp.claude_") -> list[dict]:
    rows = d.hub.store.conn.execute("SELECT kind, data, target, outcome FROM audit_log WHERE kind LIKE ?", (prefix + "%",)).fetchall()
    return [dict(r) for r in rows]


def _no_secret(*objs) -> None:
    s = json.dumps(objs, ensure_ascii=False, default=str)
    for sec in SECRETS:
        assert sec not in s, sec


# ---------------------------------------------------------------- hàm thuần
def test_parse_list_real_shape():
    out = ("Checking MCP server health…\n\n"
           "atl: https://mcp.atlassian.com/v1/mcp (HTTP) - ! Needs authentication\n"
           "dead: http://127.0.0.1:9/mcp (HTTP) - ✘ Failed to connect — ECONNREFUSED: Unable to connect.\n"
           "st: /usr/bin/false  - ✘ Failed to connect — CONNECTION_CLOSED: Connection closed\n"
           "ok-1: https://x.example/mcp (HTTP) - ✓ Connected\n"
           "claude.ai Gmail: https://gmail.mcp.claude.com/mcp - ✓ Connected\n"
           "ok: weird - ? Something\n")
    got = parse_list(out, frozenset({"atl", "dead", "st", "ok-1", "ok", "missing"}))
    assert got == {"atl": "needs_auth", "dead": "failed", "st": "failed", "ok-1": "connected", "ok": "unknown"}


def test_find_any_url_and_redirect_redaction():
    from cfharness.accounts.login import find_any_url

    url = "https://auth.example/v1/authorize?response_type=code&client_id=c1&state=S"
    assert find_any_url(f"Visit:\r\n  \x1b]8;;{url}\x07\x1b[94m{url}\x1b[39m\x1b]8;;\x07\r\n") == url
    assert find_any_url(f"Visit:\r\n  {url}\r\n\r\nWaiting for authorization… (^C to cancel)") == url
    assert find_any_url("Starting authentication…") is None
    assert find_any_url("Error: cannot reach https://srv.example/mcp?k=1\r\nbye\r\n", final=True) is None  # không phải link authorize


# ---------------------------------------------------------------- thêm: cô lập theo tài khoản, secret không lộ
async def test_add_per_account_isolation_and_no_leak(d, tmp_path):
    a, b = _two(d)
    res = await rpc(d, "mcp.claude_add", {"account_id": a, "name": "jira", "transport": "http",
                                         "url": f"https://mcp.jira.example/v1/mcp?k={URL_SECRET}", "headers": {"Authorization": HDR}})
    assert res["account"] == a and res["name"] == "jira" and res["host"] == "mcp.jira.example" and res["registered"]
    _no_secret(res)
    da = d.hub.paths.home / "claude-config" / a
    cj = json.loads((da / ".claude.json").read_text(encoding="utf-8"))
    assert cj["mcpServers"]["jira"]["headers"]["Authorization"] == HDR  # claude giữ — trong thư mục riêng của tài khoản
    assert "jira" not in claude_source.read_servers(d.hub.paths.home / "claude-config" / b)  # tài khoản khác không có
    assert "jira" not in claude_source.read_servers(None)  # cấu hình mặc định không có
    row = d.hub.mcp.show("jira")
    assert row["config"]["source"] == "claude" and row["config"]["url"] == "https://mcp.jira.example"
    _no_secret(row)
    log = _log(tmp_path)
    add = [x for x in log if x["argv"][:2] == ["mcp", "add-json"]][-1]
    assert add["argv"] == ["mcp", "add-json", "-s", "user", "jira", "-"]  # secret CHỈ qua stdin
    assert add["config_dir"] == str(da) and not add["oauth_token"] and not add["api_key"] and not add["claudecode"]
    assert json.loads(add["stdin"])["headers"]["Authorization"] == HDR
    assert add["cwd"] == str(d.hub.paths.home / "claude-config" / ".mcp-cwd")
    rows = _audit_rows(d)
    assert [r["kind"] for r in rows] == ["mcp.claude_add"] and rows[0]["outcome"] == "ok"
    params = json.loads(rows[0]["data"])["params"]
    assert set(params) <= {"name", "account_id", "transport", "register", "trust"}
    _no_secret(rows)
    # thêm lần hai trên tài khoản B → một dòng registry dùng chung
    res = await rpc(d, "mcp.claude_add", {"account_id": b, "name": "jira", "transport": "http", "url": "https://mcp.jira.example/v1/mcp"})
    assert res["registered"]["existing"] and "jira" in claude_source.read_servers(d.hub.paths.home / "claude-config" / b)


async def test_add_validation_and_cli_error_no_echo(d, tmp_path):
    a, _ = _two(d)
    for bad in ("cfharness", "hub-self", "bad name", "", "-x"):
        with pytest.raises(HubError):
            await rpc(d, "mcp.claude_add", {"account_id": a, "name": bad, "url": "https://x.example/mcp"})
    port = d.hub.config.get("mcp_server.http_port")
    with pytest.raises(HubError, match="chính hub"):
        await rpc(d, "mcp.claude_add", {"account_id": a, "name": "loop", "url": f"http://127.0.0.1:{port}/mcp"})
    with pytest.raises(HubError, match="url"):
        await rpc(d, "mcp.claude_add", {"account_id": a, "name": "x", "transport": "http", "url": "javascript:alert(1)"})
    # cấu hình mặc định (machine): header bí mật được (v0.2.32 — agent bị chặn đọc ~/.claude.json, luật claude_credentials)
    res = await rpc(d, "mcp.claude_add", {"account_id": "machine", "name": "m1", "url": "https://m.example/mcp", "headers": {"X-Key": HDR}})
    assert "m1" in json.loads((_default_home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert res["account"] == "machine" and HDR not in json.dumps(res)
    # claude báo lỗi và NHẮC LẠI url + header trong stderr → thông báo của hub không chứa chúng
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.claude_add", {"account_id": a, "name": "failsrv", "url": f"https://f.example/mcp?k={URL_SECRET}",
                                        "headers": {"Authorization": HDR}})
    _no_secret(ei.value.message, ei.value.data)
    _no_secret(_audit_rows(d))
    assert all(r["outcome"] in ("ok", "error") for r in _audit_rows(d))


# ---------------------------------------------------------------- trạng thái + cache
async def test_status_parse_cache_and_no_urls(d, tmp_path):
    a, _ = _two(d)
    for name, url in (("needs", "https://oauth.example/mcp"), ("down", "https://dead.example/mcp"), ("fine", "https://fine.example/mcp")):
        await rpc(d, "mcp.claude_add", {"account_id": a, "name": name, "url": url + f"?k={URL_SECRET}"})
    st = await rpc(d, "mcp.claude_status", {"account_id": a})
    assert {s["name"]: s["status"] for s in st["servers"]} == {"needs": "needs_auth", "down": "failed", "fine": "connected"}
    assert st["checked_at"] and st["account"] == a
    _no_secret(st)
    assert "oauth.example/mcp" not in json.dumps(st)  # chỉ host, không url
    n = len([x for x in _log(tmp_path) if x["argv"][:2] == ["mcp", "list"]])
    await rpc(d, "mcp.claude_status", {"account_id": a})  # cache 30 s
    await rpc(d, "mcp.claude_status", {"account_id": a, "cached": True})  # chỉ cache
    assert len([x for x in _log(tmp_path) if x["argv"][:2] == ["mcp", "list"]]) == n
    await rpc(d, "mcp.claude_status", {"account_id": a, "refresh": True})
    assert len([x for x in _log(tmp_path) if x["argv"][:2] == ["mcp", "list"]]) == n + 1
    cache = d.hub.store.state_get(CACHE_KEY)
    assert cache[a]["servers"]["needs"] == "needs_auth"
    # cached cho tài khoản chưa kiểm → unknown, không chạy claude
    st = await rpc(d, "mcp.claude_status", {"account_id": "machine", "cached": True})
    assert st["servers"] == [] and st["checked_at"] is None
    from cfharness.hub_transfer import MACHINE_STATE

    assert CACHE_KEY in MACHINE_STATE


# ---------------------------------------------------------------- đăng nhập (PTY thật + claude giả)
@needs_pty
async def test_login_wrong_then_right_redirect(d, tmp_path):
    a, b = _two(d)
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "atl", "url": "https://oauth.example/v1/mcp"})
    await rpc(d, "mcp.claude_add", {"account_id": b, "name": "atl", "url": "https://oauth.example/v1/mcp"})
    lg = await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "atl"})
    assert lg["login_id"].startswith("l_") and lg["url"].startswith("https://auth.example/v1/authorize?") and lg["account"] == a
    st = await rpc(d, "mcp.claude_status", {"account_id": a, "cached": True})
    assert st["logins"] and st["logins"][0]["login_id"] == lg["login_id"]
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.claude_login_finish", {"login_id": lg["login_id"], "redirect_url": BAD_REDIRECT})
    assert "không khớp" in ei.value.message and "BADCODE" not in ei.value.message and "localhost" not in ei.value.message
    with pytest.raises(HubError):  # một lượt = một lần thử
        await rpc(d, "mcp.claude_login_finish", {"login_id": lg["login_id"], "redirect_url": GOOD_REDIRECT})
    lg = await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "atl"})
    res = await rpc(d, "mcp.claude_login_finish", {"login_id": lg["login_id"], "redirect_url": GOOD_REDIRECT})
    assert res == {"account": a, "name": "atl", "status": "connected", "ok": True, "hub_token_deleted": False}
    # cô lập: đăng nhập nằm ở thư mục của A; B vẫn cần đăng nhập
    assert (d.hub.paths.home / "claude-config" / a / ".fake_mcp_auth.json").exists()
    stb = await rpc(d, "mcp.claude_status", {"account_id": b, "refresh": True})
    assert stb["servers"][0]["status"] == "needs_auth"
    logins = [x for x in _log(tmp_path) if x["argv"][:2] == ["mcp", "login"]]
    assert logins and all(x["config_dir"] == str(d.hub.paths.home / "claude-config" / a) and x["argv"][3:] == ["--no-browser"]
                          and not x["oauth_token"] and not x["api_key"] for x in logins)
    rows = _audit_rows(d)
    assert {r["kind"] for r in rows} >= {"mcp.claude_login_start", "mcp.claude_login_finish"}
    assert any(r["kind"] == "mcp.claude_login_finish" and r["outcome"] == "error" for r in rows)
    _no_secret(rows)
    # đăng xuất → lại cần đăng nhập
    await rpc(d, "mcp.claude_logout", {"account_id": a, "name": "atl"})
    st = await rpc(d, "mcp.claude_status", {"account_id": a})
    assert st["servers"][0]["status"] == "needs_auth"


@needs_pty
async def test_login_not_saved_reports_error(d, monkeypatch):
    a, _ = _two(d)
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "atl", "url": "https://oauth.example/v1/mcp"})
    monkeypatch.setenv("FAKE_MCP_LOGIN_MODE", "nosave")
    lg = await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "atl"})
    with pytest.raises(HubError, match="vẫn báo cần đăng nhập"):
        await rpc(d, "mcp.claude_login_finish", {"login_id": lg["login_id"], "redirect_url": GOOD_REDIRECT})


@needs_pty
async def test_login_one_per_account_cancel_and_guards(d, monkeypatch):
    a, b = _two(d)
    for acct in (a, b):
        await rpc(d, "mcp.claude_add", {"account_id": acct, "name": "atl", "url": "https://oauth.example/v1/mcp"})
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "local", "transport": "stdio", "command": "/bin/echo", "args": ["x"]})
    with pytest.raises(HubError, match="stdio"):
        await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "local"})
    with pytest.raises(HubError):
        await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "nope"})
    l1 = await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "atl"})
    l2 = await rpc(d, "mcp.claude_login_start", {"account_id": b, "name": "atl"})  # tài khoản khác: song song được
    l3 = await rpc(d, "mcp.claude_login_start", {"account_id": a, "name": "atl"})  # cùng tài khoản: đóng lượt cũ
    live = set(d.hub.mcp_claude._logins)
    assert l1["login_id"] not in live and {l2["login_id"], l3["login_id"]} <= live
    await rpc(d, "mcp.claude_login_cancel", {"login_id": l3["login_id"]})
    with pytest.raises(HubError, match="hết hạn"):
        await rpc(d, "mcp.claude_login_finish", {"login_id": l3["login_id"], "redirect_url": GOOD_REDIRECT})
    with pytest.raises(HubError, match="thiếu URL"):
        await rpc(d, "mcp.claude_login_finish", {"login_id": l2["login_id"], "redirect_url": ""})
    monkeypatch.setenv("FAKE_MCP_LOGIN_MODE", "exit_early")
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.claude_login_start", {"account_id": b, "name": "atl"})
    assert "SECRETQUERY" not in ei.value.message and "leak.example" not in ei.value.message


# ---------------------------------------------------------------- gỡ: registry chỉ xoá khi không tài khoản nào còn
async def test_remove_registry_semantics(d, tmp_path):
    a, b = _two(d)
    for acct in (a, b):
        await rpc(d, "mcp.claude_add", {"account_id": acct, "name": "jira", "url": "https://jira.example/mcp"})
    d.hub.store.conn.execute("UPDATE agents SET mcp_servers=? WHERE 1=0", ("[]",))  # (không có agent mặc định)
    r = await rpc(d, "mcp.claude_remove", {"account_id": a, "name": "jira"})
    assert r["registry"] == "kept" and r["other_accounts"] == [b]
    assert d.hub.mcp.get_row("jira") is not None
    assert "jira" not in claude_source.read_servers(d.hub.paths.home / "claude-config" / a)
    with pytest.raises(HubError):  # không còn ở A
        await rpc(d, "mcp.claude_remove", {"account_id": a, "name": "jira"})
    # agent còn gán + đây là bản cuối → Conflict trừ force
    d.hub.agents.create({"id": "w1", "name": "w1", "mcp_servers": ["jira"]})
    with pytest.raises(HubError, match="đang được agent dùng"):
        await rpc(d, "mcp.claude_remove", {"account_id": b, "name": "jira"})
    assert "jira" in claude_source.read_servers(d.hub.paths.home / "claude-config" / b)  # chưa chạy claude remove
    r = await rpc(d, "mcp.claude_remove", {"account_id": b, "name": "jira", "force": True})
    assert r["registry"] == "removed" and d.hub.mcp.get_row("jira") is None
    # thêm lại → khôi phục dòng đã xoá mềm (giữ trust/tools đã đặt)
    r = await rpc(d, "mcp.claude_add", {"account_id": a, "name": "jira", "url": "https://jira.example/mcp"})
    assert r["registered"]["existing"] and d.hub.mcp.get_row("jira") is not None


async def test_logout_refused_while_run_uses_server(d):
    a, _ = _two(d)
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "jira", "url": "https://jira.example/mcp"})

    class _AR:
        run_id = "r_busy"
        claude_mcp = ("jira",)
        claude_session = a

    d.hub.runs.active["r_busy"] = _AR()
    try:
        with pytest.raises(HubError, match="run đang dùng"):
            await rpc(d, "mcp.claude_logout", {"account_id": a, "name": "jira"})
        with pytest.raises(HubError, match="run đang dùng"):
            await rpc(d, "mcp.claude_remove", {"account_id": a, "name": "jira"})
    finally:
        d.hub.runs.active.pop("r_busy", None)


# ---------------------------------------------------------------- an ninh: chỉ người, audit, classifier
def test_rpcs_human_only_audited_and_cli_blocked(hub_home):
    import time

    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI, GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    ms = ("mcp.claude_add", "mcp.claude_remove", "mcp.claude_login_start", "mcp.claude_login_finish", "mcp.claude_login_cancel",
          "mcp.claude_logout", "mcp.claude_status")
    for m in ms:
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED, m
    assert set(ms) - {"mcp.claude_status"} <= MUTATING
    assert "claude" in HUB_HUMAN_ONLY_CLI["mcp"]
    for cmd in ("cfharness mcp claude status", "cfharness mcp claude login jira", "cfharness mcp claude add x --url https://x"):
        ps = PolicyStore(hub_home.home)
        ps.ensure()
        ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
        v = classify("Bash", {"command": cmd}, ctx)
        assert v.forbidden and v.rule == "hub_human_only", cmd


async def test_manager_caller_denied(d):
    a, _ = _two(d)
    caller = auth.manager_caller("mgr", None)
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.claude_status", {"account_id": a}, as_caller=caller)
    assert ei.value.code == -32003


# ---------------------------------------------------------------- CLI
class _Ctx:
    def __init__(self, args, results):
        self.args, self.json, self.calls, self.results = args, False, [], results

    def call(self, method, params=None, **kw):
        self.calls.append((method, dict(params or {})))
        r = self.results.get(method)
        return r(params) if callable(r) else r

    def out(self, data, text=None, view=None):
        print(text)


def test_cli_login_add_rm_status(monkeypatch, capsys):
    from cfharness.cli import build_parser
    from cfharness.mcp_server import cli as mcli

    asked = iter([GOOD_REDIRECT, HDR])
    monkeypatch.setattr(mcli, "_ask_secret", lambda prompt: next(asked))
    a = build_parser().parse_args(["mcp", "claude", "login", "atl", "--account", "a_12345678"])
    ctx = _Ctx(a, {"mcp.claude_login_start": {"login_id": "l_1", "url": "https://auth.example/authorize?x=1", "name": "atl", "account": "a_12345678"},
                   "mcp.claude_login_finish": {"name": "atl", "status": "connected", "account": "a_12345678", "ok": True}})
    assert mcli.cmd_mcp(ctx) == 0
    assert ctx.calls[0] == ("mcp.claude_login_start", {"account_id": "a_12345678", "name": "atl"})
    assert ctx.calls[1] == ("mcp.claude_login_finish", {"login_id": "l_1", "redirect_url": GOOD_REDIRECT})
    out = capsys.readouterr().out
    assert "https://auth.example/authorize?x=1" in out and "đã kết nối" in out and "CODE-SECRET" not in out
    a = build_parser().parse_args(["mcp", "claude", "add", "jira", "--url", "https://j.example/mcp", "--header", "Authorization"])
    ctx = _Ctx(a, {"mcp.claude_add": {"name": "jira", "transport": "http", "host": "j.example", "account": None, "registered": {}, "warnings": []}})
    assert mcli.cmd_mcp(ctx) == 0
    p = ctx.calls[0][1]
    assert p["headers"] == {"Authorization": HDR} and p["url"] == "https://j.example/mcp" and p["transport"] == "http"
    a = build_parser().parse_args(["mcp", "claude", "add", "loc", "--", "npx", "-y", "srv"])
    ctx = _Ctx(a, {"mcp.claude_add": {"name": "loc", "transport": "stdio", "account": None, "registered": None, "warnings": []}})
    assert mcli.cmd_mcp(ctx) == 0 and ctx.calls[0][1]["command"] == "npx" and ctx.calls[0][1]["args"] == ["-y", "srv"]
    a = build_parser().parse_args(["mcp", "claude", "rm", "jira", "--force"])
    ctx = _Ctx(a, {"mcp.claude_remove": {"name": "jira", "account": None, "registry": "removed", "other_accounts": []}})
    assert mcli.cmd_mcp(ctx) == 0 and ctx.calls[0][1]["force"] is True
    a = build_parser().parse_args(["mcp", "claude", "status", "--refresh"])
    ctx = _Ctx(a, {"mcp.claude_status": {"account_label": "x", "config_path": "/p", "servers": [
        {"name": "atl", "transport": "http", "host": "h", "status": "needs_auth", "registered": True, "agents": []}]}})
    assert mcli.cmd_mcp(ctx) == 0 and ctx.calls[0][1]["refresh"] is True
    assert "cfharness mcp claude login atl" in capsys.readouterr().out


# ---------------------------------------------------------------- doctor
async def test_doctor_lists_needs_auth_for_active_account(d):
    a, _ = _two(d)
    d.hub.accounts.use(a)
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "needs", "url": "https://oauth.example/mcp"})
    await rpc(d, "mcp.claude_add", {"account_id": "machine", "name": "only-machine", "url": "https://m.example/mcp"})
    rep = hub_doctor.run(d.hub.paths)
    ck = {c["id"]: c for c in rep["checks"]}
    assert ck["mcp_claude_auth"]["ok"] and "chưa kiểm" in ck["mcp_claude_auth"]["detail"]
    assert not ck["mcp_claude_missing"]["ok"] and "only-machine" in ck["mcp_claude_missing"]["detail"]
    await rpc(d, "mcp.claude_status", {"account_id": a})
    rep = hub_doctor.run(d.hub.paths)
    ck = {c["id"]: c for c in rep["checks"]}
    assert not ck["mcp_claude_auth"]["ok"] and "needs" in ck["mcp_claude_auth"]["detail"]
    assert f"cfharness mcp claude login needs --account {a}" in ck["mcp_claude_auth"]["fix"]


# ---------------------------------------------------------------- web (mục "MCP của tài khoản" — handler GIẢ như test_web_accounts)
MCP_LOGIN_URL = "https://auth.example/v1/authorize?response_type=code&state=S1"


def _mcp(env):
    state = {"servers": {"machine": [{"name": "atl", "transport": "http", "host": "auth.example", "hub": False, "status": "needs_auth",
                                      "registered": True, "registered_source": "claude", "agents": []}]},
             "login": None}
    calls: list[tuple[str, dict]] = []

    def rec(name, fn):
        def w(p):
            p = p or {}
            calls.append((name, {k: v for k, v in p.items() if k not in ("redirect_url", "headers", "url")}
                          | {"has_redirect": bool(p.get("redirect_url")), "headers_keys": sorted((p.get("headers") or {}).keys()),
                             "has_url": bool(p.get("url"))}))
            return fn(p)
        env.d.methods[name] = w

    def status(p):
        aid = p.get("account_id") or "machine"
        lg = state["login"] if state["login"] and state["login"]["account"] == aid else None
        return {"account": aid, "servers": list(state["servers"].get(aid, [])), "checked_at": None, "error": None,
                "logins": [lg] if lg else []}

    def start(p):
        state["login"] = {"login_id": "LM", "url": MCP_LOGIN_URL, "expires_at": "2099-01-01T00:00:00Z", "account": p["account_id"],
                          "name": p["name"]}
        return state["login"]

    def finish(p):
        if p.get("redirect_url") != GOOD_REDIRECT:
            raise HubError("URL chuyển hướng không khớp lượt đăng nhập này")
        state["login"] = None
        state["servers"]["machine"][0]["status"] = "connected"
        return {"account": "machine", "name": "atl", "status": "connected", "ok": True}

    def add(p):
        state["servers"].setdefault(p["account_id"], []).append({"name": p["name"], "transport": p["transport"], "host": "x", "hub": False,
                                                                 "status": "unknown", "registered": True, "agents": []})
        return {"account": p["account_id"], "name": p["name"], "transport": p["transport"], "host": "x", "registered": {}, "warnings": []}

    rec("mcp.claude_status", status)
    rec("mcp.claude_login_start", start)
    rec("mcp.claude_login_finish", finish)
    rec("mcp.claude_login_cancel", lambda p: (state.update(login=None), {"ok": True})[1])
    rec("mcp.claude_add", add)
    rec("mcp.claude_remove", lambda p: {"account": p["account_id"], "name": p["name"], "registry": "removed", "other_accounts": []})
    rec("mcp.claude_logout", lambda p: {"account": p["account_id"], "name": p["name"], "ok": True})
    return state, calls


def _mrow(html: str) -> str:
    m = re.search(r'<details class="acct-mcp small" id="acct-mcp-machine".*?</details>\s*</details>', html, re.S)
    assert m, "thiếu mục MCP của tài khoản (machine)"
    return m.group(0)


async def test_web_mcp_section_login_flow(env):
    state, calls = _mcp(env)
    html = (await get(env, "/settings")).text
    row = _mrow(section(html))
    assert "atl" in row and "cần đăng nhập" in row and "/do/mcp_claude_login_start" in row and "/do/mcp_claude_rm" in row
    assert 'name="header_value"' in row  # machine cũng cho header (agent bị chặn đọc ~/.claude.json — v0.2.32)
    assert all(c[1].get("cached") for c in calls if c[0] == "mcp.claude_status")  # tải trang KHÔNG chạy claude
    r = await post(env, "/do/mcp_claude_login_start", {"account_id": "machine", "mcp_name": "atl", "name": "Máy",
                                                       "next": "/settings#acct-mcp-machine"})
    assert r.status_code == 303 and calls[-1][0] == "mcp.claude_login_start" and calls[-1][1]["_audit"]["channel"] == "web"
    row = _mrow(section((await get(env, "/settings")).text))
    link = re.search(r'<a class="btn primary" href="([^"]+)" target="_blank" rel="noopener noreferrer">', row)
    assert link and link.group(1).replace("&amp;", "&") == MCP_LOGIN_URL
    tag = re.search(r'<input name="redirect_url"[^>]*>', row).group(0)
    assert 'type="password"' in tag and "value=" not in tag
    await post(env, "/do/mcp_claude_login_finish", {"login_id": "LM", "redirect_url": BAD_REDIRECT, "next": "/settings"})
    page = (await get(env, "/settings")).text
    assert "BADCODE" not in page and "flash error" in page
    state["login"] = {"login_id": "LM", "url": MCP_LOGIN_URL, "expires_at": "2099", "account": "machine", "name": "atl"}
    await post(env, "/do/mcp_claude_login_finish", {"login_id": "LM", "redirect_url": GOOD_REDIRECT, "next": "/settings"})
    page = (await get(env, "/settings")).text
    assert "Đã đăng nhập MCP atl: đã kết nối" in page
    fin = [c[1] for c in calls if c[0] == "mcp.claude_login_finish"]
    assert fin[-1]["has_redirect"] and fin[-1]["_audit"]["channel"] == "web"
    for b in env.bodies:
        assert "CODE-SECRET" not in b and "BADCODE" not in b
    # bad link từ daemon → không vẽ, tự huỷ
    env.d.methods["mcp.claude_login_start"] = lambda p: {"login_id": "LX", "url": "javascript:alert(1)", "account": "machine", "name": "atl"}
    await post(env, "/do/mcp_claude_login_start", {"account_id": "machine", "mcp_name": "atl", "next": "/settings"})
    page = (await get(env, "/settings")).text
    assert "javascript:alert" not in page


async def test_web_mcp_add_rm_logout_confirm_and_validation(env):
    state, calls = _mcp(env)
    r = await post(env, "/do/mcp_claude_add", {"account_id": "machine", "name": "Máy", "mcp_name": "jira", "url": "https://j.example/mcp",
                                               "transport": "http", "next": "/settings"})
    assert r.status_code == 303 and calls[-1][0] == "mcp.claude_add" and calls[-1][1]["has_url"] and calls[-1][1]["name"] == "jira"
    n = len(calls)
    await post(env, "/do/mcp_claude_add", {"account_id": "machine", "mcp_name": "bad name", "url": "https://j.example", "next": "/settings"})
    await post(env, "/do/mcp_claude_add", {"account_id": "machine", "mcp_name": "ok", "url": "javascript:x", "next": "/settings"})
    await post(env, "/do/mcp_claude_add", {"account_id": "../x", "mcp_name": "ok", "url": "https://j.example", "next": "/settings"})
    await post(env, "/do/mcp_claude_add", {"account_id": "machine", "mcp_name": "ok", "url": "https://j.example", "header_name": "X-Key",
                                           "next": "/settings"})  # tên header thiếu giá trị
    assert len([c for c in calls[n:] if c[0] == "mcp.claude_add"]) == 0
    await post(env, "/do/mcp_claude_add", {"account_id": "machine", "mcp_name": "hdr", "url": "https://j.example/mcp", "header_name": "X-Key",
                                           "header_value": HDR, "next": "/settings"})
    assert calls[-1][1]["headers_keys"] == ["X-Key"]
    # gỡ / đăng xuất cần xác nhận
    r = await post(env, "/do/mcp_claude_rm", {"account_id": "machine", "mcp_name": "jira", "name": "Máy", "next": "/settings"})
    assert calls[-1][0] != "mcp.claude_remove" and "Gỡ MCP «jira»" in r.text
    await post(env, "/do/mcp_claude_rm", {"account_id": "machine", "mcp_name": "jira", "name": "Máy", "confirm": "yes", "next": "/settings"})
    assert calls[-1][0] == "mcp.claude_remove" and calls[-1][1]["name"] == "jira"
    r = await post(env, "/do/mcp_claude_logout", {"account_id": "machine", "mcp_name": "atl", "name": "Máy", "next": "/settings"})
    assert calls[-1][0] != "mcp.claude_logout"
    await post(env, "/do/mcp_claude_logout", {"account_id": "machine", "mcp_name": "atl", "confirm": "yes", "next": "/settings"})
    assert calls[-1][0] == "mcp.claude_logout"
    await post(env, "/do/mcp_claude_check", {"account_id": "machine", "next": "/settings"})
    assert calls[-1][0] == "mcp.claude_status" and calls[-1][1]["refresh"] is True
    for b in env.bodies:
        assert HDR not in b and "hdr-SECRET" not in b


async def test_web_mcp_hidden_on_old_daemon(env):
    env.d.methods.pop("mcp.claude_status", None)
    html = (await get(env, "/settings")).text
    assert "acct-mcp-" not in html


async def test_web_mcp_register_for_hub_button(env, tmp_path, monkeypatch):
    """«Đăng ký cho hub» trên server có trong cấu hình claude nhưng chưa đăng ký: `mcp.add{config:{source:claude}}` THẬT (daemon kiểm
    tên trong .claude.json), tick «Tin cậy» → trusted; server trỏ về hub không có nút; form agent gắn nhãn `claude`."""
    state, calls = _mcp(env)
    (tmp_path / "ch").mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(tmp_path / "ch"))
    (tmp_path / "ch" / ".claude.json").write_text(json.dumps({"mcpServers": {
        "jira": {"type": "http", "url": "https://j.example/mcp", "headers": {"Authorization": "Bearer RegHdr-SECRET-000111"}},
        "loose": {"type": "http", "url": "https://l.example/mcp"}}}), encoding="utf-8")
    state["servers"]["machine"] += [
        {"name": "jira", "transport": "http", "host": "j.example", "hub": False, "status": "connected", "registered": False, "agents": []},
        {"name": "loose", "transport": "http", "host": "l.example", "hub": False, "status": "connected", "registered": False, "agents": []},
        {"name": "selfhub", "transport": "stdio", "host": None, "hub": True, "status": "connected", "registered": False, "agents": []}]
    row = _mrow(section((await get(env, "/settings")).text))
    forms = re.findall(r'<form method="post" action="/do/mcp_claude_register".*?</form>', row, re.S)
    assert len(forms) == 2 and all('name="trust"' in f and 'type="checkbox"' in f for f in forms)
    assert 'value="selfhub"' not in "".join(forms) and "trỏ về chính hub" in row
    assert not any('value="atl"' in f for f in forms)  # đã đăng ký → không nút
    # chưa xác nhận → trang xác nhận, chưa đăng ký
    r = await post(env, "/do/mcp_claude_register", {"account_id": "machine", "mcp_name": "jira", "name": "Máy", "trust": "trusted",
                                                     "next": "/settings#acct-mcp-machine"})
    assert r.status_code == 200 and "Đăng ký MCP «jira»" in r.text and "Tin cậy" in r.text
    assert env.hub.mcp.get_row("jira") is None
    r = await post(env, "/do/mcp_claude_register", {"account_id": "machine", "mcp_name": "jira", "name": "Máy", "trust": "trusted",
                                                     "confirm": "yes", "next": "/settings#acct-mcp-machine"})
    assert r.status_code == 303 and r.headers["location"].startswith("/settings")
    row_db = env.hub.mcp.show("jira")
    assert row_db["config"]["source"] == "claude" and row_db["trust"] == "trusted" and not row_db["config"].get("headers")
    await post(env, "/do/mcp_claude_register", {"account_id": "machine", "mcp_name": "loose", "confirm": "yes", "next": "/settings"})
    assert env.hub.mcp.show("loose")["trust"] == "untrusted"
    # tên không có trong cấu hình claude / tên sai → không đăng ký
    await post(env, "/do/mcp_claude_register", {"account_id": "machine", "mcp_name": "ghost", "confirm": "yes", "next": "/settings"})
    assert env.hub.mcp.get_row("ghost") is None and "flash error" in (await get(env, "/settings")).text
    await post(env, "/do/mcp_claude_register", {"account_id": "machine", "mcp_name": "bad name", "confirm": "yes", "next": "/settings"})
    await post(env, "/do/mcp_claude_register", {"account_id": "../x", "mcp_name": "loose", "confirm": "yes", "next": "/settings"})
    aud = [dict(r) for r in env.hub.store.conn.execute("SELECT * FROM audit_log WHERE kind LIKE '%mcp.add%'")]
    assert aud and any("web" in json.dumps(a, default=str) for a in aud)
    for b in env.bodies + [json.dumps(aud, default=str)]:
        assert "RegHdr-SECRET" not in b
    # form agent: server đã đăng ký gán được (mọi MCP đều do claude giữ — không còn nhãn nguồn riêng)
    page = (await get(env, "/agents/new")).text
    assert re.search(r'value="jira".*?</label>', page, re.S)
