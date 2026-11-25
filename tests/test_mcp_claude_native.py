"""plan-mcp-claude-native PR7: hub bỏ lớp chính sách MCP — MCP bên thứ ba do claude quản lý theo tài khoản, MỌI agent dùng chung.

Kiểm: RPC `mcp.list` (danh sách thô theo tài khoản, che secret, JSON hỏng → `error`), CLI `mcp list`, argv của run (không
`--strict-mcp-config`, không `--disallowedTools` MCP trừ server trỏ về hub và Playwright ở chế độ Chrome, guard tắt → cho phép sẵn
`mcp__<server>`), guard không chặn/hỏi tool MCP bên thứ ba (vẫn chặn tool tự duyệt của hub), migration v35, agent/YAML cũ có
`mcp_servers` vẫn nạp. Không gọi claude thật: ScriptedRuntime (+ claude giả trên macOS/Linux).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.accounts import claude_config
from cfharness.config import DEFAULTS
from cfharness.errors import HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.mcp_client import claude_source
from cfharness.runtime import claude_cli
from cfharness.runtime.base import make_adapter
from cfharness.runtime.claude_cli import ClaudeCliRuntime, build_argv
from cfharness.store import MIGRATIONS, Store
from m7_fake import MgrScript, close, make_daemon, rpc, wait_status

TOK_A = "sk-ant-oat01-" + "Aa1_" * 20
TOK_B = "sk-ant-oat01-" + "Bb2_" * 20
TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]
SECRET_HDR = "Bearer jira-SECRET-0123456789abcdef"
SECRET_ENV = "ghp_SECRETSECRETSECRET0123456789"
SECRET_ARG = "--token=ARG-SECRET-987654321"
SECRET_BARE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dcXYZ"  # khoá giả (tách chuỗi để khỏi bị secret scanning chặn)

CLAUDE_JSON = {
    "numStartups": 3,
    "mcpServers": {
        "jira": {"type": "http", "url": "https://user:pw@mcp.jira.example:8443/v1/mcp?key=QS-SECRET", "headers": {"Authorization": SECRET_HDR}},
        "gh": {"command": "npx", "args": ["-y", "gh-mcp", SECRET_ARG, SECRET_BARE], "env": {"GITHUB_TOKEN": SECRET_ENV}},
        "remote": {"command": "npx", "args": ["mcp-remote", "https://u:pw2@r.example/mcp?api_key=QS2-SECRET-abcdefghijklmnop"]},
        "other": {"type": "sse", "url": "https://other.example/sse"},
        "selfhub": {"command": "cfharness", "args": ["mcp", "serve"]},
    },
    "projects": {"/tmp/x": {"mcpServers": {"localonly": {"command": "x"}}}},  # local-scope: không nạp trong -p → không liệt kê
    "oauthAccount": {"emailAddress": "someone@example.com"},
}


def _write_cj(d: Path, data: dict | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / ".claude.json"
    p.write_text(json.dumps(data or CLAUDE_JSON), encoding="utf-8")
    return p


def _no_secret(obj) -> None:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    for sec in (SECRET_HDR, "jira-SECRET", SECRET_ENV, "ARG-SECRET", SECRET_BARE, "QS-SECRET", "QS2-SECRET", "user:pw", "u:pw2",
                "someone@example.com", "GITHUB_TOKEN"):
        assert sec not in s, sec


# ---------------------------------------------------------------- đọc .claude.json (thuần)
def test_raw_servers_targets_are_redacted(tmp_path):
    _write_cj(tmp_path)
    servers, err = claude_source.raw_servers(tmp_path)
    assert err is None
    got = {s["name"]: s for s in servers}
    assert list(got) == ["gh", "jira", "other", "remote", "selfhub"]  # local-scope không liệt kê
    assert got["jira"] == {"name": "jira", "transport": "http", "target": "mcp.jira.example"}
    assert got["other"]["transport"] == "sse" and got["other"]["target"] == "other.example"
    assert got["gh"]["transport"] == "stdio" and got["gh"]["target"].startswith("npx -y gh-mcp --token=***") and "***" in got["gh"]["target"]
    assert got["remote"]["target"].startswith("npx mcp-remote https://r.example/mcp?api_key=")
    assert got["selfhub"]["target"] == "cfharness mcp serve"
    _no_secret(servers)
    assert claude_source.raw_servers(tmp_path / "missing") == ([], None)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / ".claude.json").write_text("{not json", encoding="utf-8")
    servers, err = claude_source.raw_servers(tmp_path / "bad")
    assert servers == [] and err and "JSON" in err
    (tmp_path / "arr").mkdir()
    (tmp_path / "arr" / ".claude.json").write_text("[1, 2]", encoding="utf-8")
    assert claude_source.raw_servers(tmp_path / "arr")[1]


def test_mcp_list_auth_class_and_removed_methods():
    assert "mcp.list" not in auth.HUMAN_ONLY  # như mcp.list chỉ đọc cũ: mọi người dùng web/CLI đã xác thực
    for m in ("mcp.add", "mcp.update", "mcp.remove", "mcp.restore", "mcp.assign", "mcp.claude_servers", "mcp.claude_add",
              "mcp.claude_status", "mcp.claude_login_start"):
        assert m not in auth.HUMAN_ONLY, m


def test_cli_parser_only_list_and_hub_server_commands():
    from cfharness.cli import build_parser

    p = build_parser()
    assert p.parse_args(["mcp", "list", "--json"]).mcp_action == "list"
    for removed in (["mcp", "add", "x"], ["mcp", "claude", "status"], ["mcp", "edit", "x"], ["mcp", "assign", "x", "y"],
                    ["mcp", "show", "x"], ["mcp", "rm", "x"], ["mcp", "claude-servers"]):
        with pytest.raises(SystemExit):
            p.parse_args(removed)
    for keep in (["mcp", "serve"], ["mcp", "snippet"], ["mcp", "token"]):
        assert p.parse_args(keep).mcp_action == keep[1]


def test_cli_list_text_has_add_hint():
    from cfharness.mcp_server.cli import _list_text

    res = {"accounts": [
        {"id": None, "label": "mặc định", "config_path": "/u/.claude.json", "servers": [{"name": "gh", "transport": "stdio", "target": "npx gh"}],
         "error": None},
        {"id": "a_12345678", "label": "A", "config_path": "/h/claude-config/a_12345678/.claude.json", "servers": [],
         "error": "JSON không hợp lệ (JSONDecodeError)"}]}
    text = _list_text(res)
    assert "gh" in text and "npx gh" in text and "JSON không hợp lệ" in text
    assert "claude mcp add" in text
    assert "CLAUDE_CONFIG_DIR=" in text and "a_12345678" in text.split("CLAUDE_CONFIG_DIR=", 1)[1]


# ---------------------------------------------------------------- daemon
class HoldScript(MgrScript):
    def __call__(self, spec):
        self.specs.append(spec)
        return {"text": "ok", "cost": 0.001, "hold": True}


@pytest.fixture(autouse=True)
def _os_sandbox(monkeypatch):
    monkeypatch.setattr("cfharness.manager.sandbox_gate.os_sandbox_available", lambda: True)


@pytest.fixture
async def d(hub_home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    uh = tmp_path / "userhome"
    (uh / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(uh))
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(tmp_path / "claude-home"))  # cấu hình claude mặc định riêng cho test này
    (tmp_path / "claude-home").mkdir()
    dm = await make_daemon(hub_home, HoldScript())
    claude_cli._login_cache.clear()
    yield dm
    await close(dm)
    claude_cli._login_cache.clear()


def _acct(d) -> tuple[str, str, Path]:
    a = d.hub.accounts.add_token("A", TOK_A)["id"]
    b = d.hub.accounts.add_token("B", TOK_B)["id"]
    cdir = claude_config.ensure(d.hub.paths.home, a)
    _write_cj(cdir)
    d.hub.accounts.use(a)
    return a, b, cdir


async def _run(d, agent="w1", wait=("running",)):
    res = await rpc(d, "run.start", {"agent": agent, "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    row = await wait_status(d, run_id, wait)
    specs = [s for s in d.script.specs if s.run_id == run_id]
    return run_id, (specs[-1] if specs else None), row


def _dis(argv: list[str]) -> list[str]:
    return argv[argv.index("--disallowedTools") + 1:] if "--disallowedTools" in argv else []


async def test_mcp_list_rpc_accounts(d, tmp_path):
    a, b, cdir = _acct(d)
    _write_cj(tmp_path / "claude-home", {"mcpServers": {"def1": {"type": "http", "url": "https://def.example/mcp"}}})
    bdir = claude_config.ensure(d.hub.paths.home, b)
    (bdir / ".claude.json").write_text("{broken", encoding="utf-8")
    res = await rpc(d, "mcp.list", {})
    assert list(res) == ["accounts"]
    accts = res["accounts"]
    assert [x["id"] for x in accts] == [None, a, b]
    for x in accts:
        assert set(x) == {"id", "label", "config_path", "servers", "error"}
    dflt, aa, bb = accts
    assert dflt["config_path"] == str(tmp_path / "claude-home" / ".claude.json") and dflt["error"] is None
    assert dflt["servers"] == [{"name": "def1", "transport": "http", "target": "def.example"}]
    assert aa["label"] == "A" and aa["config_path"] == str(cdir / ".claude.json") and aa["error"] is None
    assert [s["name"] for s in aa["servers"]] == ["gh", "jira", "other", "remote", "selfhub"]
    for s in aa["servers"]:
        assert set(s) == {"name", "transport", "target"} and s["transport"] in ("stdio", "http", "sse")
    assert bb["servers"] == [] and bb["error"]
    _no_secret(res)
    for gone in ("mcp.add", "mcp.show", "mcp.assign", "mcp.claude_status", "mcp.agent_config"):
        with pytest.raises(HubError):
            await rpc(d, gone, {"name": "x"})


async def test_run_loads_account_mcp_without_strict(d, tmp_path):
    a, _, cdir = _acct(d)
    _write_cj(tmp_path / "claude-home")  # cấu hình mặc định (Windows không tạo được symlink → run rơi về cấu hình mặc định)
    shared = claude_config.shared(cdir)
    # agent kiểu cũ có `mcp_servers` (gói/YAML/client cũ): nhận rồi bỏ qua
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}, "mcp_servers": ["jira"]})
    assert d.hub.store.get_agent("w1")["mcp_servers"] == []
    run_id, spec, _ = await _run(d)
    mcp = json.loads(spec.mcp_config_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "hub-guard" in mcp and not {"jira", "gh", "other"} & set(mcp)  # mcp.json: CHỈ server của hub
    assert spec.isolation_overrides == {"strict_mcp_config": False}
    assert spec.allowed_mcp_servers is None  # nạp MCP của tài khoản: không isolation.violation theo tên server
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert "--strict-mcp-config" not in argv and argv[argv.index("--setting-sources") + 1] == ""
    assert [x for x in _dis(argv) if x.startswith("mcp__")] == ["mcp__selfhub__*"]  # chỉ server trỏ về CHÍNH hub (H-18)
    assert "ToolSearch" in spec.tool_policy["tools"]
    # CLAUDE_CONFIG_DIR = thư mục của tài khoản đang dùng (đã có) — server/token MCP của claude theo thư mục đó
    env = ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=d.hub.secrets)._run_env(spec)
    if shared:
        assert spec.claude_config_dir == cdir and d.hub.runs.active[run_id].claude_session == a
        assert env["CLAUDE_CONFIG_DIR"] == str(cdir)
    else:  # không dựng được thư mục dùng chung session → cảnh báo, chạy bằng cấu hình mặc định (không hỏng run)
        assert spec.claude_config_dir is None and "CLAUDE_CONFIG_DIR" not in env
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": [], "plugins": [],
            "mcp_servers": [{"name": "jira", "status": "needs-auth"}, {"name": "claude.ai Slack", "status": "connected"},
                            {"name": "evil", "status": "connected"}, {"name": "hub-guard", "status": "connected"}]}
    evs = make_adapter(spec).feed(init)
    assert not [e for e in evs if e.type in ("isolation.violation", "mcp.unavailable")]


async def test_account_without_config_dir_uses_default(d, tmp_path):
    """Tài khoản token chưa có thư mục cấu hình riêng → MCP của cấu hình mặc định (không dựng thư mục mới cho run)."""
    _write_cj(tmp_path / "claude-home")
    a = d.hub.accounts.add_token("A", TOK_A)["id"]  # chưa có claude-config/<id>/
    d.hub.accounts.use(a)
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}})
    _, spec, _ = await _run(d)
    assert spec.claude_config_dir is None and spec.isolation_overrides == {"strict_mcp_config": False}
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert "--strict-mcp-config" not in argv and [x for x in _dis(argv) if x.startswith("mcp__")] == ["mcp__selfhub__*"]


async def test_guard_off_allows_account_mcp_tools(d, tmp_path):
    """guard.enabled=false: không có kênh xin quyền → claude -p TỪ CHỐI tool MCP chưa cho phép (`mcp__*` không khớp — kiểm 2.1.332)
    → run cho phép sẵn `mcp__<server>` của mọi server trong cấu hình claude (trừ server trỏ về hub)."""
    _write_cj(tmp_path / "claude-home")  # chưa cấu hình tài khoản → cấu hình claude mặc định
    d.hub.config.set("guard.enabled", False)
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}})
    _, spec, _ = await _run(d)
    allowed = spec.tool_policy["allowed_tools"]
    assert {"mcp__gh", "mcp__jira", "mcp__other", "mcp__remote"} <= set(allowed)
    assert "mcp__selfhub" not in allowed and "mcp__*" not in allowed
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert "--strict-mcp-config" not in argv and "mcp__jira" in argv[argv.index("--allowedTools") + 1:]


async def test_chrome_mode_removes_only_playwright(d, tmp_path):
    data = json.loads(json.dumps(CLAUDE_JSON))
    data["mcpServers"]["browser"] = {"command": "npx", "args": ["@playwright/mcp@latest"]}
    _write_cj(tmp_path / "claude-home", data)
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS, "browser": "chrome"}})
    _, spec, _ = await _run(d)
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert sorted(x for x in _dis(argv) if x.startswith("mcp__")) == ["mcp__browser__*", "mcp__selfhub__*"]


async def test_technical_runs_keep_strict(d):
    for run in ({"kind": "review"}, {"kind": "eval"}, {"kind": "adhoc", "run_opts": {"learning_review": True}},
                {"kind": "adhoc", "run_opts": {"eval_overlay": {"x": 1}}}):
        assert d.hub.runs._open_mcp(run) is False
    assert d.hub.runs._open_mcp({"kind": "adhoc"}) is True and d.hub.runs._open_mcp({"kind": "task"}) is True


async def test_guard_allows_third_party_mcp_in_run(d, tmp_path):
    _write_cj(tmp_path / "claude-home")
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}})
    run_id, _, _ = await _run(d)
    for tool, inp in (("mcp__foo__bar", {}), ("mcp__jira__delete_issue", {"id": "X-1"}), ("mcp__gh__create_issue", {}),
                      ("mcp__claude_ai_Slack__slack_send_message", {"text": "hi"})):
        res = await d.hub.guard.check({"run_id": run_id, "channel": "hook", "tool_name": tool, "tool_input": inp})
        assert res["decision"] == "allow", (tool, res)
    assert not d.hub.approvals.list(status="open") if hasattr(d.hub.approvals, "list") else True
    # vẫn chặn: tool tự duyệt / chỉ-người của CHÍNH hub (H-18), kể cả server hub đăng ký dưới tên khác trong cấu hình claude
    for tool in ("mcp__x__hub_approve", "mcp__selfhub__agent_create", "mcp__hub-x__approve"):
        res = await d.hub.guard.check({"run_id": run_id, "channel": "hook", "tool_name": tool, "tool_input": {}})
        assert res["decision"] != "allow", tool


def test_classify_third_party_mcp_safe(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load(), git_probe=False)
    for tool in ("mcp__foo__bar", "mcp__foo__delete_everything", "mcp__claude_ai_Atlassian__createJiraIssue"):
        v = classify(tool, {}, ctx)
        assert v.verdict == "safe" and not v.forbidden and v.rule == "mcp_third_party", (tool, v)
    for tool in ("mcp__x__hub_approve", "mcp__x__approve", "mcp__x__golden_reject"):  # tự duyệt / sửa chính sách: chặn trên MỌI server
        assert classify(tool, {}, ctx).forbidden, tool


# ---------------------------------------------------------------- migration v35 + cấu hình agent cũ
def test_migration_v35_drops_registry_and_assignment(tmp_path):
    assert len(MIGRATIONS) >= 35
    db = tmp_path / "hub.db"
    s = Store(db)
    now = "2026-11-25T00:00:00Z"
    s.conn.execute("INSERT INTO agents(id, name, home_path, mcp_servers, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                   ("w1", "w1", str(tmp_path / "a"), '["jira","gh"]', now, now))
    s.conn.execute("CREATE TABLE mcp_servers (name TEXT PRIMARY KEY, transport TEXT NOT NULL, config TEXT NOT NULL, created_at TEXT, updated_at TEXT)")
    s.conn.execute("INSERT INTO mcp_servers(name, transport, config) VALUES ('jira', 'http', '{}')")
    s.conn.execute("PRAGMA user_version=34")
    s.close()
    s2 = Store(db)
    try:
        assert s2.schema_version == len(MIGRATIONS)
        tabs = {r[0] for r in s2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "mcp_servers" not in tabs
        assert s2.get_agent("w1")["mcp_servers"] == []
    finally:
        s2.close()
    # DB cũ ở v33 (trước bước Python v34 đã gỡ) đi qua v34 (no-op) + v35
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE mcp_servers (name TEXT PRIMARY KEY, config TEXT)")
    c.execute("PRAGMA user_version=33")
    c.commit()
    c.close()
    s3 = Store(db)
    try:
        assert s3.schema_version == len(MIGRATIONS)
        assert "mcp_servers" not in {r[0] for r in s3.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        s3.close()


def test_old_agent_config_with_mcp_servers_still_loads(hub_home, tmp_path):
    from cfharness.agents import AgentConfig
    from cfharness.hub import Hub
    from cfharness.runtime import FakeRuntime

    cfg = AgentConfig(id="old", name="old", mcp_servers=["jira", "gh"])  # YAML agent cũ
    assert "mcp_servers" not in cfg.model_dump()
    hub = Hub(hub_home, runtime=FakeRuntime())
    try:
        _old_agent_checks(hub)
    finally:
        hub.store.close()


def _old_agent_checks(hub) -> None:
    ag = hub.agents.create({"id": "old", "name": "old", "mcp_servers": ["nope"]})
    assert ag["mcp_servers"] == []
    out = hub.agents.update("old", {"mcp_servers": ["x"], "role": "r"})  # client cũ gửi kèm → bỏ qua, không lỗi
    assert out["role"] == "r" and out["mcp_servers"] == []
    rev1 = hub.agents.update("old", {"mcp_servers": []})  # chỉ trường đã bỏ → không đổi gì
    assert rev1["config_revision"] == out["config_revision"]
    # revert về revision có snapshot mang `mcp_servers` (bản cũ)
    hub.store.conn.execute("UPDATE agent_revisions SET config_snapshot=? WHERE agent_id='old' AND revision=1",
                           (json.dumps({"name": "old", "model": "sonnet", "mcp_servers": ["jira"]}),))
    back = hub.agents.revert("old", 1)
    assert back["mcp_servers"] == [] and back["config_revision"] == out["config_revision"] + 1
    # file config.yaml của agent không còn khoá này
    assert "mcp_servers" not in (Path(ag["home_path"]) / "config.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- claude giả thật (ClaudeCliRuntime): argv + CLAUDE_CONFIG_DIR + init
FAKE = r'''
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)"); sys.exit(0)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "s"
sys.stdin.readline()
cdir = os.environ.get("CLAUDE_CONFIG_DIR")
with open(os.environ["FAKE_SEEN"], "w") as f:
    json.dump({"argv": args, "config_dir": cdir}, f)
names = []
if cdir and "--strict-mcp-config" not in args:
    names = list(json.load(open(os.path.join(cdir, ".claude.json"))).get("mcpServers") or {})
servers = [{"name": n, "status": "connected"} for n in names] + [{"name": "hub-guard", "status": "connected"},
                                                                 {"name": "claude.ai Slack", "status": "pending"}]
def out(o):
    o.setdefault("session_id", sid); print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": ".", "tools": [], "mcp_servers": servers,
     "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302"})
out({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.001, "usage": {"input_tokens": 1, "output_tokens": 1},
     "result": "ok", "duration_ms": 5, "num_turns": 1})
'''


@pytest.mark.skipif(sys.platform == "win32", reason="claude giả là shim sh")
async def test_fake_claude_e2e_every_agent_sees_account_mcp(hub_home, tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from conftest import fake_cli

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "uh"))
    (tmp_path / "uh").mkdir()
    script = tmp_path / "fake_claude_mcp.py"
    script.write_text(FAKE, encoding="utf-8")
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_SEEN", str(seen))
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(fake_cli(tmp_path, script))}
    hub = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    hub.start()
    try:
        a = hub.accounts.add_token("A", TOK_A)["id"]
        cdir = claude_config.ensure(hub.paths.home, a)
        _write_cj(cdir)
        hub.accounts.use(a)
        hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}})
        run = hub.runs.submit("w1", "xin chào")
        final = await hub.runs.wait(run["id"], 20)
        assert final["outcome"] == "completed", final["error"]
        got = json.loads(seen.read_text(encoding="utf-8"))
        assert got["config_dir"] == str(cdir) and "--strict-mcp-config" not in got["argv"]
        assert not any(x.startswith("mcp__") and x != "mcp__selfhub__*" for x in got["argv"] if x.endswith("__*"))
        assert not final["isolation_violations"]
    finally:
        await hub.dispatcher.stop()
        hub.store.close()


# ---------------------------------------------------------------- review PR7: H-18 với cổng MCP tuỳ chỉnh, Playwright tổ chức cấp
HUB9100 = {"mcpServers": {"cfh": {"type": "http", "url": "http://127.0.0.1:9100/mcp", "headers": {"Authorization": "Bearer x"}},
                          "hub": {"command": "some-wrapper"}, "docs": {"type": "http", "url": "https://docs.example/mcp"}}}


@pytest.mark.parametrize("guard_on", [False, True])
async def test_hub_server_on_custom_port_is_removed_and_forbidden(d, tmp_path, guard_on):
    """`cfharness mcp snippet --http --scope user` với `mcp_server.http_port` khác mặc định (đa instance) ghi MCP của CHÍNH hub (token
    control) vào `~/.claude.json` → mọi run nạp: phải bị gỡ (`--disallowedTools`), không được cho phép sẵn khi guard tắt, và guard cấm
    MỌI tool của nó (hub_run, hub_cancel…, không chỉ tool chỉ-người). Tên dành riêng của hub (`hub`) cũng vậy."""
    _write_cj(tmp_path / "claude-home", HUB9100)
    d.hub.config.set("mcp_server.http_port", 9100)
    d.hub.config.set("guard.enabled", guard_on)
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}})
    run_id, spec, _ = await _run(d)
    dis = spec.tool_policy.get("disallowed_tools") or []
    assert "mcp__cfh__*" in dis and "mcp__hub__*" in dis and "mcp__docs__*" not in dis
    allowed = spec.tool_policy.get("allowed_tools") or []
    assert not [x for x in allowed if str(x) in ("mcp__cfh", "mcp__hub") or str(x).startswith(("mcp__cfh__", "mcp__hub__"))]
    if not guard_on:
        assert "mcp__docs" in allowed
    else:
        assert {"cfh", "hub"} <= d.hub.guard.hub_mcp_servers()
        ctx = d.hub.guard.context_for(d.hub.store.get_run(run_id))
        for tool in ("mcp__cfh__hub_run", "mcp__cfh__hub_cancel", "mcp__cfh__hub_steer", "mcp__cfh__hub_task_create",
                     "mcp__cfh__hub_approve", "mcp__hub__anything"):
            v = classify(tool, {}, ctx)
            assert v.forbidden, (tool, v)
        assert classify("mcp__docs__search", {}, ctx).rule == "mcp_third_party"
        res = await d.hub.guard.check({"run_id": run_id, "channel": "hook", "tool_name": "mcp__cfh__hub_run", "tool_input": {}})
        assert res["decision"] != "allow"


def test_hub_port_detection_pure(tmp_path):
    _write_cj(tmp_path, HUB9100)
    assert claude_source.hub_servers(tmp_path) == ["hub"]  # cổng mặc định: 9100 không phải hub (chỉ tên dành riêng)
    assert claude_source.hub_servers(tmp_path, 9100) == ["cfh", "hub"]
    servers, _ = claude_source.raw_servers(tmp_path)
    assert all(set(s) == {"name", "transport", "target"} for s in servers)  # cờ nội bộ không lộ ra mcp.list


async def test_managed_playwright_removed_and_guard_forbids_in_chrome_mode(d, tmp_path):
    """Server Playwright do TỔ CHỨC cấp (`managedMcpServers`) tên không chứa 'playwright' — nhận theo lệnh, gỡ khỏi run chế độ Chrome
    và guard cấm gọi (`playwright_not_in_run`); không lộ lệnh ra `mcp.list`."""
    _write_cj(tmp_path / "claude-home", {"mcpServers": {"docs": {"type": "http", "url": "https://docs.example/mcp"}}})
    rs = tmp_path / "claude-home" / ".claude" / "remote-settings.json"
    rs.parent.mkdir(parents=True, exist_ok=True)
    rs.write_text(json.dumps({"managedMcpServers": {"orgbrowser": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}), encoding="utf-8")
    assert "orgbrowser" in claude_source.playwright_servers(None)
    d.hub.agents.create({"id": "c", "name": "c", "model": "haiku", "tool_policy": {"tools": TOOLS, "browser": "chrome"}})
    d.hub.agents.create({"id": "n", "name": "n", "model": "haiku", "tool_policy": {"tools": TOOLS, "browser": "none"}})
    rc, sc, _ = await _run(d, agent="c")
    assert "mcp__orgbrowser__*" in sc.tool_policy["disallowed_tools"]
    v = classify("mcp__orgbrowser__browser_navigate", {"url": "https://x"}, d.hub.guard.context_for(d.hub.store.get_run(rc)))
    assert v.forbidden and v.rule == "playwright_not_in_run"
    rn, sn, _ = await _run(d, agent="n")
    assert "mcp__orgbrowser__*" not in (sn.tool_policy.get("disallowed_tools") or [])
    assert classify("mcp__orgbrowser__browser_navigate", {}, d.hub.guard.context_for(d.hub.store.get_run(rn))).verdict == "safe"
    assert "@playwright" not in json.dumps(await rpc(d, "mcp.list", {}))
