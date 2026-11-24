"""Chuyển MCP OAuth của hub sang MCP do claude quản lý theo tài khoản — `mcp.migrate_to_claude` (plan-mcp-claude-native PR4 giai đoạn A).

Không gọi claude thật: `tests/fake_claude_mcp.py` (trạng thái theo CLAUDE_CONFIG_DIR, login qua PTY thật; url chứa "oauth" cần đăng nhập).
Dòng registry OAuth KIỂU CŨ của hub (`add(..., allow_legacy_oauth=True)` — OAuth của hub đã gỡ từ v0.2.28) + bản ghi `MCP_OAUTH_*` dựng
trực tiếp — không mạng. Thêm: run của agent gán server kiểu cũ bị từ chối trước spawn, stub `mcp.oauth_*`, `mcp login|logout`.
Kiểm: đường đẹp (giữ trust/tools/annotations/agent gán, mở lượt đăng nhập), token hub chỉ bị xoá SAU khi đăng nhập claude thành công
(hoặc `drop_hub_token` rõ), hoàn tác khi chuyển nguồn lỗi, từ chối server không OAuth / client_secret / header + machine / run đang chạy,
audit không mang secret, CLI, web, doctor, hub import.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from cfharness import auth, hub_doctor
from cfharness import hub_transfer as T
from cfharness.errors import HubError
from cfharness.mcp_client import claude_source, migrate
from cfharness.mcp_client.legacy_oauth import load_record, secret_name
from cfharness.mcp_server.rpc import MUTATING
from cfharness.web.app import ACTIONS  # noqa: F401 — nạp app (đăng ký thao tác)
from m7_fake import MgrScript, close, make_daemon, rpc, wait_status
from test_web_accounts import env, get, post  # noqa: F401 — fixture + helper web

FAKE = str(Path(__file__).with_name("fake_claude_mcp.py"))
TOK_A = "sk-ant-oat01-" + "Aa1_" * 20
ACCESS = "hub-ACCESS-TOKEN-SECRET-123456"
REFRESH = "hub-REFRESH-TOKEN-SECRET-654321"
HDR = "Bearer static-HDR-SECRET-0123456789"
URL = "https://oauth.notes.example/mcp"
GOOD_REDIRECT = "http://localhost:51814/callback?code=CODE-SECRET-123&state=FAKESTATE"
SECRETS = (ACCESS, REFRESH, HDR, "static-HDR-SECRET", "CODE-SECRET-123", TOK_A)

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="claude giả là script shebang (POSIX)")


@pytest.fixture
async def d(hub_home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("FAKE_MCP_LOGIN_MODE", raising=False)
    (tmp_path / "claude-default").mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(tmp_path / "claude-default"))
    monkeypatch.setenv("FAKE_MCP_LOG", str(tmp_path / "mcp.log"))
    dm = await make_daemon(hub_home, MgrScript())
    dm.hub.config.set("runtime.claude.binary", FAKE)
    yield dm
    await dm.hub.mcp_claude.close_all()
    await close(dm)


def _acct(d) -> str:
    return d.hub.accounts.add_token("A", TOK_A)["id"]


def _save_record(d, name: str, rec: dict) -> None:
    d.hub.secrets.set(secret_name(name), json.dumps(rec))


def _hub_oauth(d, name: str = "notes", **extra) -> None:
    cfg = {"name": name, "transport": "http", "url": URL, "auth": {"type": "oauth"}, "trust": "trusted", "description": "ghi chú",
           "tools": {"exclude": ["delete_*"], "require_approval": ["write_*"], "annotations": {"browse_*": {"readOnlyHint": True}}}}
    cfg.update(extra)
    d.hub.mcp.add(cfg, allow_legacy_oauth=True)
    _save_record(d, name, {"tokens": {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 9999999999}})
    d.hub.store.conn.execute("UPDATE mcp_servers SET annotations_cache=? WHERE name=?",
                             (json.dumps({"browse_page": {"readOnlyHint": True}}), name))
    d.hub.agents.create({"id": "w1", "name": "W1", "model": "haiku"})
    d.hub.agents.update("w1", {"mcp_servers": [name]})


def _cdir(d, aid: str) -> Path:
    return d.hub.paths.home / "claude-config" / aid


def _audit(d) -> list[dict]:
    rows = d.hub.store.conn.execute("SELECT kind, data, outcome FROM audit_log WHERE kind LIKE 'mcp.%'").fetchall()
    return [dict(r) for r in rows]


def _no_secret(*objs) -> None:
    s = json.dumps(objs, ensure_ascii=False, default=str)
    for sec in SECRETS:
        assert sec not in s, sec


def _log(tmp_path: Path) -> list[dict]:
    p = tmp_path / "mcp.log"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ---------------------------------------------------------------- đường đẹp
@needs_pty
async def test_migrate_happy_path_keeps_policy_and_drops_token_after_login(d, tmp_path):
    a = _acct(d)
    _hub_oauth(d)
    pl = await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a, "dry_run": True})
    assert pl["account"] == a and pl["agents"] == ["w1"] and pl["host"] == "oauth.notes.example"
    assert d.hub.mcp.get_row("notes")["config"]["source"] != "claude"  # dry-run không ghi
    res = await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a})
    assert res["status"] == "migrated" and res["added"] and res["hub_token"] == "pending_login" and res["agents"] == ["w1"]
    assert res["login"]["url"].startswith("https://auth.example/") and res["login"]["login_id"].startswith("l_")
    _no_secret(res)
    # claude giữ url + không oauth.clientId (DCR); stdin, không argv
    cj = json.loads((_cdir(d, a) / ".claude.json").read_text(encoding="utf-8"))
    assert cj["mcpServers"]["notes"] == {"type": "http", "url": URL}
    add = [x for x in _log(tmp_path) if x["argv"][:2] == ["mcp", "add-json"]][-1]
    assert add["argv"] == ["mcp", "add-json", "-s", "user", "notes", "-"] and add["config_dir"] == str(_cdir(d, a))
    # registry: nguồn claude, giữ trust/tools/annotations/agent gán/annotations_cache; không còn auth
    row = d.hub.mcp.get_row("notes")
    c = row["config"]
    assert c["source"] == "claude" and "auth" not in c and c["url"] == "https://oauth.notes.example" and row["source"] == "claude"
    assert row["trust"] == "trusted" and c["description"] == "ghi chú"
    assert c["tools"]["exclude"] == ["delete_*"] and c["tools"]["require_approval"] == ["write_*"]
    assert c["tools"]["annotations"] == {"browse_*": {"readOnlyHint": True}} and row["annotations_cache"] == {"browse_page": {"readOnlyHint": True}}
    assert d.hub.agents.get_row("w1")["mcp_servers"] == ["notes"]
    ann = d.hub.mcp.tool_annotations("notes", "write_doc")
    assert ann["require_approval"] and ann["trust"] == "trusted" and ann["source"] == "claude"
    # token hub CHƯA xoá trước khi đăng nhập claude xong
    assert (load_record(d.hub.secrets, "notes") or {}).get("tokens")
    assert d.hub.store.state_get(migrate.STATE_KEY)["notes"]["key"] == a
    fin = await rpc(d, "mcp.claude_login_finish", {"login_id": res["login"]["login_id"], "redirect_url": GOOD_REDIRECT})
    assert fin["status"] == "connected" and fin["hub_token_deleted"] is True
    assert load_record(d.hub.secrets, "notes") is None and not (d.hub.paths.home / "secrets" / secret_name("notes")).exists()
    assert "notes" not in (d.hub.store.state_get(migrate.STATE_KEY) or {})
    rows = _audit(d)
    kinds = [r["kind"] for r in rows]
    assert "mcp.migrate_to_claude" in kinds and "mcp.migrate_hub_token_deleted" in kinds
    for r in rows:
        if r["kind"] == "mcp.migrate_to_claude":
            assert set(json.loads(r["data"])["params"]) <= {"name", "account_id", "keep_hub_token", "drop_hub_token", "dry_run", "login"}
    _no_secret(rows)


@needs_pty
async def test_token_kept_until_status_connected_and_keep_flag(d):
    a = _acct(d)
    _hub_oauth(d)
    _hub_oauth_extra = {"name": "keep", "transport": "http", "url": "https://oauth.keep.example/mcp", "auth": {"type": "oauth"}}
    d.hub.mcp.add(_hub_oauth_extra, allow_legacy_oauth=True)
    _save_record(d, "keep", {"tokens": {"access_token": ACCESS + "K"}})
    res = await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a, "login": False})
    assert res["login"] is None and not any("đăng nhập sau" in w for w in res["warnings"])  # login=False: không mở, không cảnh báo
    st = await rpc(d, "mcp.claude_status", {"account_id": a, "refresh": True})  # vẫn cần đăng nhập → token giữ
    assert st["servers"][0]["status"] == "needs_auth" and load_record(d.hub.secrets, "notes")
    # đăng nhập xong NGOÀI hub (vd trên trình duyệt của máy hub) → «Kiểm tra MCP» thấy connected → xoá token hub
    (_cdir(d, a) / ".fake_mcp_auth.json").write_text(json.dumps({"notes": True}))
    await rpc(d, "mcp.claude_status", {"account_id": a, "refresh": True})
    assert load_record(d.hub.secrets, "notes") is None
    # --keep-hub-token: đăng nhập xong vẫn giữ
    res = await rpc(d, "mcp.migrate_to_claude", {"name": "keep", "account_id": a, "keep_hub_token": True})
    assert res["hub_token"] == "kept"
    fin = await rpc(d, "mcp.claude_login_finish", {"login_id": res["login"]["login_id"], "redirect_url": GOOD_REDIRECT})
    assert fin["ok"] and fin["hub_token_deleted"] is False and load_record(d.hub.secrets, "keep")
    # xoá rõ ràng
    out = await rpc(d, "mcp.migrate_to_claude", {"name": "keep", "drop_hub_token": True})
    assert out["hub_token_deleted"] and load_record(d.hub.secrets, "keep") is None


async def test_static_client_id_and_headers(d):
    a = _acct(d)
    d.hub.mcp.add({"name": "cid", "transport": "http", "url": URL, "headers": {"X-Api": HDR}, "auth": {"type": "oauth", "client_id": "my-app",
                                                                                                         "scopes": ["read"]}},
                  allow_legacy_oauth=True)
    assert "${" in d.hub.mcp.get_row("cid")["config"]["headers"]["X-Api"]  # registry chỉ giữ tham chiếu
    with pytest.raises(HubError, match="tài khoản token"):  # header + machine → từ chối (luật PR3)
        await rpc(d, "mcp.migrate_to_claude", {"name": "cid", "account_id": "machine"})
    assert not (Path(claude_source.default_claude_json())).exists() or "cid" not in claude_source.read_servers(None)
    res = await rpc(d, "mcp.migrate_to_claude", {"name": "cid", "account_id": a, "login": False})
    assert res["hub_token"] == "none" and any("redirect" in w for w in res["warnings"]) and any("scope" in w for w in res["warnings"])
    e = json.loads((_cdir(d, a) / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["cid"]
    assert e["oauth"] == {"clientId": "my-app"} and e["headers"] == {"X-Api": HDR}  # secret đã thay — CHỈ trong thư mục tài khoản
    _no_secret(res, _audit(d))


# ---------------------------------------------------------------- từ chối + hoàn tác
async def test_refuse_non_oauth_client_secret_and_busy(d):
    a = _acct(d)
    d.hub.mcp.add({"name": "stat", "transport": "http", "url": "https://s.example/mcp", "headers": {"Authorization": HDR}})
    d.hub.mcp.add({"name": "loc", "command": "npx", "args": ["-y", "srv"]})
    d.hub.mcp.add({"name": "sec", "transport": "http", "url": URL, "auth": {"type": "oauth", "client_id": "c", "client_secret_ref": "pw-123456"}}, allow_legacy_oauth=True)
    for n, pat in (("stat", "secret tĩnh"), ("loc", "stdio"), ("sec", "client_secret"), ("nope", "không có MCP")):
        with pytest.raises(HubError, match=pat):
            await rpc(d, "mcp.migrate_to_claude", {"name": n, "account_id": a})
    assert not (_cdir(d, a) / ".claude.json").exists() or not claude_source.read_servers(_cdir(d, a))
    _hub_oauth(d)

    class _AR:
        run_id = "r_busy"
        agent_id = "w1"

    d.hub.runs.active["r_busy"] = _AR()
    try:
        with pytest.raises(HubError, match="run đang dùng"):
            await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a})
    finally:
        d.hub.runs.active.pop("r_busy", None)
    await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a, "login": False})
    with pytest.raises(HubError, match="đã là nguồn claude"):
        await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a})
    with pytest.raises(HubError, match="vẫn là nguồn hub"):
        await rpc(d, "mcp.migrate_to_claude", {"name": "sec", "drop_hub_token": True})


async def test_rollback_when_flip_fails(d, monkeypatch):
    a = _acct(d)
    _hub_oauth(d)

    def boom(name, info):
        raise HubError("lỗi giả khi ghi registry")

    monkeypatch.setattr(d.hub.mcp, "convert_to_claude", boom)
    with pytest.raises(HubError, match="lỗi giả"):
        await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a})
    assert "notes" not in claude_source.read_servers(_cdir(d, a))  # đã gỡ entry vừa thêm
    row = d.hub.mcp.get_row("notes")
    assert row["config"]["source"] != "claude" and row["config"]["auth"]["type"] == "oauth"
    assert load_record(d.hub.secrets, "notes") and not (d.hub.store.state_get(migrate.STATE_KEY) or {})
    # claude add lỗi → không có gì đổi (tên chứa "fail" làm claude giả báo lỗi + in url/header)
    monkeypatch.undo()
    d.hub.mcp.add({"name": "will-fail", "transport": "http", "url": URL, "auth": {"type": "oauth"}}, allow_legacy_oauth=True)
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.migrate_to_claude", {"name": "will-fail", "account_id": a})
    assert URL not in ei.value.message and d.hub.mcp.get_row("will-fail")["config"]["source"] != "claude"
    # đã có sẵn trong cấu hình claude, cùng host → dùng lại (không thêm, không gỡ khi hoàn tác)
    await rpc(d, "mcp.claude_add", {"account_id": a, "name": "pre", "url": "https://oauth.notes.example/other", "register": False})
    d.hub.mcp.add({"name": "pre", "transport": "http", "url": URL, "auth": {"type": "oauth"}}, allow_legacy_oauth=True)
    res = await rpc(d, "mcp.migrate_to_claude", {"name": "pre", "account_id": a, "login": False})
    assert res["added"] is False and d.hub.mcp.get_row("pre")["config"]["source"] == "claude"


# ---------------------------------------------------------------- an ninh
def test_human_only_mutating_classifier(hub_home):
    import time

    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI, GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    assert "mcp.migrate_to_claude" in auth.HUMAN_ONLY and "mcp.migrate_to_claude" not in auth.MANAGER_ALLOWED
    assert "mcp.migrate_to_claude" in MUTATING and "migrate-to-claude" in HUB_HUMAN_ONLY_CLI["mcp"]
    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    v = classify("Bash", {"command": "cfharness mcp migrate-to-claude notes --account a_12345678 --yes"}, ctx)
    assert v.forbidden and v.rule == "hub_human_only"


async def test_manager_caller_denied(d):
    a = _acct(d)
    _hub_oauth(d)
    with pytest.raises(HubError) as ei:
        await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a}, as_caller=auth.manager_caller("mgr", None))
    assert ei.value.code == -32003
    assert d.hub.mcp.get_row("notes")["config"]["source"] != "claude"


# ---------------------------------------------------------------- doctor + hub import
async def test_doctor_requires_migration(d):
    _hub_oauth(d)
    ck = {c["id"]: c for c in hub_doctor.run(d.hub.paths)["checks"]}
    assert ck["mcp_hub_oauth"]["level"] == "error" and "cần chuyển sang claude" in ck["mcp_hub_oauth"]["detail"]
    assert "cfharness mcp migrate-to-claude notes --account <id>" in ck["mcp_hub_oauth"]["fix"]
    assert ck["mcp:notes"]["level"] == "error" and ck["mcp:notes"]["fix"] == "cfharness mcp migrate-to-claude notes --account <id>"
    a = _acct(d)
    await rpc(d, "mcp.migrate_to_claude", {"name": "notes", "account_id": a, "login": False})
    ck = {c["id"]: c for c in hub_doctor.run(d.hub.paths)["checks"]}
    assert "mcp_hub_oauth" not in ck


def test_import_notes_relogin_per_account(tmp_path):
    db = tmp_path / "hub.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE hub_state(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE mcp_servers(name TEXT PRIMARY KEY, transport TEXT, config TEXT, deleted_at TEXT)")
    c.executemany("INSERT INTO mcp_servers VALUES (?,?,?,?)", [
        ("notes", "http", json.dumps({"source": "claude", "transport": "http", "url": "https://oauth.notes.example"}), None),
        ("other", "http", json.dumps({"source": "claude", "transport": "http", "url": "https://o.example"}), None),
        ("hubsrc", "http", json.dumps({"transport": "http", "url": "https://h.example", "auth": {"type": "oauth"}}), None),
        ("gone", "http", json.dumps({"source": "claude", "transport": "http"}), "2026-01-01")])
    c.executemany("INSERT INTO hub_state VALUES (?,?,?)", [
        ("mcp.claude_status", json.dumps({"a_11111111": {"servers": {"notes": "connected"}}, "default": {"servers": {"notes": "needs_auth"}}}), "t"),
        ("claude_account_active", json.dumps("a_22222222"), "t")])
    c.commit()
    items = T.mcp_claude_relogin(c)
    c.close()
    assert items == [{"server": "notes", "transport": "http", "host": "oauth.notes.example", "accounts": ["a_11111111", "machine"]},
                     {"server": "other", "transport": "http", "host": "o.example", "accounts": ["a_22222222"]}]
    assert "mcp.claude_status" in T.MACHINE_STATE and "mcp.migrations" in T.MACHINE_STATE and T.MCP_CLAUDE_STATE in T.MACHINE_STATE
    steps = T.mcp_claude_steps(items[0])
    assert steps[0].startswith("cfharness mcp claude add notes --url https://oauth.notes.example/") and "--account a_11111111" in steps[0]
    assert "cfharness mcp claude login notes --account machine" in steps[1]
    assert "notes (tài khoản a_11111111, machine)" in T.mcp_claude_note(items)


def test_hub_import_records_mcp_claude_and_doctor_reports(tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime
    from test_hub_transfer import make_source

    src, _ = make_source(tmp_path, monkeypatch)
    h = Hub(src, runtime=FakeRuntime())
    try:
        h.mcp.add({"name": "notes", "source": "claude", "transport": "http", "url": "https://oauth.notes.example"})
        h.store.state_set("mcp.claude_status", {"a_11111111": {"checked_at": "t", "servers": {"notes": "connected"}}})
        h.store.state_set(migrate.STATE_KEY, {"notes": {"key": "a_11111111"}})
        h.integrity.rebaseline("test")
    finally:
        h.store.close()
    pkg = Path(T.export_offline(src)["path"])
    userb = tmp_path / "userB"
    userb.mkdir()
    monkeypatch.setenv("HOME", str(userb))
    from cfharness.paths import HubPaths

    dst = HubPaths.resolve(userb / ".cfharness")
    r = T.import_package(dst, pkg)
    assert r["mcp_claude_relogin"] == [{"server": "notes", "transport": "http", "host": "oauth.notes.example", "accounts": ["a_11111111"]}]
    c = sqlite3.connect(str(dst.db))
    st = {k: json.loads(v) for k, v in c.execute("SELECT key, value FROM hub_state")}
    c.close()
    assert st[T.MCP_CLAUDE_STATE][0]["server"] == "notes" and "mcp.claude_status" not in st and migrate.STATE_KEY not in st
    ck = {x["id"]: x for x in hub_doctor.run(dst)["checks"]}
    item = ck["mcp_claude_import"]
    assert not item["ok"] and "đăng nhập lại MCP notes trên tài khoản a_11111111" in item["detail"]
    assert "cfharness mcp claude login notes --account a_11111111" in item["fix"]


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


def test_cli_migrate_flow(monkeypatch, capsys):
    from cfharness.cli import build_parser
    from cfharness.mcp_server import cli as mcli

    monkeypatch.setattr(mcli, "_ask_secret", lambda prompt: GOOD_REDIRECT)

    def mig(p):
        if p.get("dry_run"):
            return {"name": "notes", "account": "a_12345678", "account_label": "a_12345678", "transport": "http", "host": "h.example",
                    "agents": ["w1"], "trust": "trusted", "warnings": []}
        return {"name": "notes", "account": "a_12345678", "account_label": "a_12345678", "added": True, "warnings": [],
                "login": {"login_id": "l_1", "url": "https://auth.example/authorize?x=1"}}

    a = build_parser().parse_args(["mcp", "migrate-to-claude", "notes", "--account", "a_12345678", "--yes"])
    ctx = _Ctx(a, {"mcp.migrate_to_claude": mig, "mcp.claude_login_finish": {"name": "notes", "status": "connected", "hub_token_deleted": True}})
    assert mcli.cmd_mcp(ctx) == 0
    assert [m for m, _ in ctx.calls] == ["mcp.migrate_to_claude", "mcp.migrate_to_claude", "mcp.claude_login_finish"]
    assert ctx.calls[0][1]["dry_run"] and ctx.calls[1][1] == {"name": "notes", "account_id": "a_12345678", "keep_hub_token": False, "login": True}
    assert ctx.calls[2][1] == {"login_id": "l_1", "redirect_url": GOOD_REDIRECT}
    out = capsys.readouterr().out
    assert "agent gán: w1" in out and "đã xoá token OAuth của hub" in out and "CODE-SECRET" not in out
    # không --yes, trả lời N → không ghi gì
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    a = build_parser().parse_args(["mcp", "migrate-to-claude", "notes", "--account", "a_12345678"])
    ctx = _Ctx(a, {"mcp.migrate_to_claude": mig})
    assert mcli.cmd_mcp(ctx) != 0 and len(ctx.calls) == 1
    # thiếu --account
    a = build_parser().parse_args(["mcp", "migrate-to-claude", "notes"])
    assert mcli.cmd_mcp(_Ctx(a, {})) != 0
    # --drop-hub-token
    a = build_parser().parse_args(["mcp", "migrate-to-claude", "notes", "--drop-hub-token"])
    ctx = _Ctx(a, {"mcp.migrate_to_claude": {"name": "notes", "hub_token_deleted": True}})
    assert mcli.cmd_mcp(ctx) == 0 and ctx.calls == [("mcp.migrate_to_claude", {"name": "notes", "drop_hub_token": True})]


def test_cli_oauth_login_logout_removed(monkeypatch, capsys):
    from cfharness.cli import build_parser
    from cfharness.mcp_server import cli as mcli

    for act in ("login", "logout"):
        a = build_parser().parse_args(["mcp", act, "notes", "--no-browser"] if act == "login" else ["mcp", act, "notes"])
        ctx = _Ctx(a, {})
        assert mcli.cmd_mcp(ctx) == 1 and ctx.calls == []  # không gọi daemon
        err = capsys.readouterr().err
        assert "đã gỡ từ v0.2.28" in err and "cfharness mcp migrate-to-claude notes --account <id>" in err


# ---------------------------------------------------------------- giai đoạn B: OAuth của hub đã gỡ (v0.2.28)
async def test_run_with_legacy_oauth_server_fails_closed_before_spawn(d):
    _hub_oauth(d)
    res = await rpc(d, "run.start", {"agent": "w1", "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    row = await wait_status(d, run_id, ("failed",))
    assert "MCP 'notes'" in (row.get("error") or row.get("summary") or "") or "notes" in json.dumps(row, default=str)
    assert "cfharness mcp migrate-to-claude notes --account <id>" in json.dumps(row, default=str, ensure_ascii=False)
    assert not [sp for sp in d.script.specs if sp.run_id == run_id]  # không spawn
    assert not (Path(d.hub.agents.get_row("w1")["home_path"]) / "runtime" / run_id).exists()  # không ghi mcp.json
    evs = [json.loads(r[0]) for r in d.hub.store.conn.execute("SELECT payload FROM events WHERE type='mcp.unavailable'")]
    assert {"server": "notes", "status": "needs_migration", "source": "hub"} in evs
    with pytest.raises(HubError, match="migrate-to-claude notes"):  # đường build mcp.json (takeover…) cũng fail-closed
        d.hub.mcp.resolved_entry("notes")
    pr = await rpc(d, "mcp.probe", {"name": "notes"})
    assert not pr["ok"] and pr["hint"] == "cfharness mcp migrate-to-claude notes --account <id>"
    # agent không gán server cũ → chạy bình thường; server stdio/header tĩnh không đổi
    d.hub.mcp.add({"name": "stat", "transport": "http", "url": "https://s.example/mcp", "headers": {"Authorization": HDR}})
    assert d.hub.mcp.resolved_entry("stat")["headers"]["Authorization"] == HDR


async def test_oauth_rpc_stubs_add_refused_and_start_warning(d, caplog):
    for m in ("mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_status", "mcp.oauth_disconnect", "mcp.oauth_refresh"):
        with pytest.raises(HubError, match="migrate-to-claude notes --account <id>") as ei:
            await rpc(d, m, {"name": "notes", "redirect_uri": "http://x", "state": "s", "code": "CODE-SECRET-123"})
        assert "v0.2.28" in ei.value.message
    for m in ("mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_disconnect", "mcp.oauth_refresh"):
        assert m in auth.HUMAN_ONLY
    _no_secret(_audit(d))
    for cfg in ({"name": "n1", "transport": "http", "url": URL, "auth": {"type": "oauth"}},):
        with pytest.raises(HubError, match="mcp claude add"):
            await rpc(d, "mcp.add", {"config": cfg, "probe": False})
    assert d.hub.mcp.get_row("n1") is None
    _hub_oauth(d)
    row = d.hub.mcp.with_oauth(d.hub.mcp.list())[0]
    assert row["oauth_status"] == "needs_migration" and row["migrate_cmd"] == "cfharness mcp migrate-to-claude notes --account <id>"
    d.hub.mcp.update("notes", {"description": "vẫn sửa được dòng cũ"})  # không đụng auth → được
    from cfharness.mcp_client.legacy_oauth import warn_on_start

    with caplog.at_level("WARNING", logger="cfharness.mcp_client"):
        assert warn_on_start(d.hub) == ["notes"]
    assert sum("cần chuyển sang claude" in r.getMessage() and "notes" in r.getMessage() for r in caplog.records) == 1
    assert d.hub.mcp.get_row("notes") is not None and load_record(d.hub.secrets, "notes")  # không tự xoá gì


# ---------------------------------------------------------------- web
def _web_stub(env, oauth: bool = True):
    calls: list[tuple[str, dict]] = []
    cfg = {"transport": "http", "url": "https://n.example/mcp", "trust": "untrusted", "tools": {}}
    if oauth:
        cfg["auth"] = {"type": "oauth"}
    env.d.methods["mcp.show"] = lambda p: {"name": "notes", "transport": "http", "trust": "untrusted", "config": cfg, "agents": [],
                                           "oauth_status": "needs_migration" if oauth else None, "annotations_cache": {}}
    env.d.methods["mcp.list"] = lambda p: [{"name": "notes", "transport": "http", "trust": "untrusted", "config": cfg, "agents": [],
                                            "oauth_status": "needs_migration" if oauth else None, "source": "manual"}]

    def mig(p):
        calls.append(("mcp.migrate_to_claude", dict(p)))
        return {"name": "notes", "account": p.get("account_id"), "account_label": "A", "login": {"login_id": "l_1", "url": "https://x"}, "warnings": []}

    env.d.methods["mcp.migrate_to_claude"] = mig
    return calls


async def test_web_migrate_button_confirm_and_redirect(env):
    env.acc.tokens["a_11111111"] = TOK_A
    calls = _web_stub(env)
    html = (await get(env, "/mcp/notes")).text
    assert 'id="migrate"' in html and 'action="/do/mcp_migrate_claude"' in html and "Chuyển sang claude" in html
    assert "mcp_connect" not in html and "mcp_disconnect" not in html  # nút Kết nối/Ngắt của OAuth hub đã gỡ
    lst = (await get(env, "/mcp")).text
    assert "cần chuyển sang claude" in lst and 'href="/mcp/notes#migrate"' in lst
    r = await post(env, "/do/mcp_migrate_claude", {"name": "notes", "account_id": "machine", "next": "/mcp/notes"})
    assert not calls and "Chuyển MCP notes sang claude" in r.text  # cần xác nhận
    r = await post(env, "/do/mcp_migrate_claude", {"name": "notes", "account_id": "machine", "confirm": "yes", "next": "/mcp/notes"})
    assert r.status_code == 303 and r.headers["location"].startswith("/settings#acct-mcp-machine")
    assert calls[-1][1]["name"] == "notes" and calls[-1][1]["account_id"] == "machine" and calls[-1][1]["_audit"]["channel"] == "web"
    n = len(calls)
    await post(env, "/do/mcp_migrate_claude", {"name": "notes", "account_id": "../x", "confirm": "yes", "next": "/mcp/notes"})
    await post(env, "/do/mcp_migrate_claude", {"name": "bad name", "account_id": "machine", "confirm": "yes", "next": "/mcp/notes"})
    assert len(calls) == n


async def test_web_no_button_for_non_oauth(env):
    _web_stub(env, oauth=False)
    html = (await get(env, "/mcp/notes")).text
    assert "mcp_migrate_claude" not in html and "cần chuyển sang claude" not in (await get(env, "/mcp")).text
