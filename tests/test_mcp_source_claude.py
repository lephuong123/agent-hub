"""MCP server nguồn `claude` (docs/dev-notes/plan-mcp-claude-native.md PR2): cấu hình + token do `claude` giữ trong `.claude.json` của
TÀI KHOẢN đang dùng; registry hub chỉ giữ metadata an toàn (trust, include/exclude, require_approval, annotations, agent).

Kiểm: schema (không command/header/env/auth, url chỉ còn host), đọc `.claude.json` chỉ ra tên/transport/host (không bao giờ secret),
RPC `mcp.claude_servers` + `mcp.add{source:claude}` (kiểm tồn tại), run: không ghi vào mcp.json, bỏ --strict-mcp-config (đường
connector), `--disallowedTools mcp__<x>__*` cho server không gán, CLAUDE_CONFIG_DIR của tài khoản, thiếu server/guard tắt → run hỏng
(fail-closed), guard (in-run, suy theo tên, require_approval, ghi đè), sandbox domain, ToolWriteWatcher, adapter `mcp.unavailable` +
`isolation.violation`, điều phối viên thấy `source`. Không gọi claude thật: ScriptedRuntime + claude giả (đọc CLAUDE_CONFIG_DIR).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.accounts import claude_config
from cfharness.config import DEFAULTS
from cfharness.errors import HubError
from cfharness.events import make_event
from cfharness.guard.classifier import classify
from cfharness.mcp_client import claude_source
from cfharness.mcp_client.config import McpServerConfig
from cfharness.runtime import claude_cli
from cfharness.runtime.base import make_adapter
from cfharness.runtime.claude_cli import OAUTH_TOKEN_SECRET, ClaudeCliRuntime, build_argv
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status

TOK_A = "sk-ant-oat01-" + "Aa1_" * 20
TOK_B = "sk-ant-oat01-" + "Bb2_" * 20
TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]
SECRET_HDR = "Bearer jira-SECRET-0123456789abcdef"
SECRET_ENV = "ghp_SECRETSECRETSECRET0123456789"
SECRET_ARG = "--token=ARG-SECRET-987654321"

CLAUDE_JSON = {
    "numStartups": 3,
    "mcpServers": {
        "jira": {"type": "http", "url": "https://user:pw@mcp.jira.example:8443/v1/mcp?key=QS-SECRET", "headers": {"Authorization": SECRET_HDR}},
        "gh": {"command": "npx", "args": ["-y", "gh-mcp", SECRET_ARG], "env": {"GITHUB_TOKEN": SECRET_ENV}},
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
    for sec in (SECRET_HDR, "jira-SECRET", SECRET_ENV, SECRET_ARG, "QS-SECRET", "user:pw", "someone@example.com"):
        assert sec not in s, sec


# ---------------------------------------------------------------- schema + đọc .claude.json (thuần)
def test_config_source_claude_keeps_only_safe_metadata():
    c = McpServerConfig(name="jira", source="claude", transport="http", url="https://u:p@mcp.jira.example:8443/x?k=1", trust="trusted")
    assert c.managed_by_claude and c.url == "https://mcp.jira.example:8443"
    st = c.stored()
    assert st["source"] == "claude" and not {"command", "args", "env", "headers", "auth"} & set(st)
    with pytest.raises(HubError):
        c.claude_entry()
    for bad in ({"command": "npx"}, {"headers": {"Authorization": "x"}}, {"env": {"A": "b"}}, {"args": ["x"]},
                {"auth": {"type": "oauth"}}):
        with pytest.raises(ValueError):
            McpServerConfig(name="x", source="claude", transport="http", **bad)
    assert McpServerConfig(name="gh", source="claude").transport == "stdio"  # stdio không cần command
    assert McpServerConfig(name="h", source="hub", command="x").source == "manual"  # "hub" = nguồn hub (giá trị cũ)
    with pytest.raises(ValueError):
        McpServerConfig(name="h", source="weird", command="x")


def test_read_servers_names_transport_host_only(tmp_path):
    _write_cj(tmp_path)
    got = claude_source.read_servers(tmp_path)
    assert got == {
        "jira": {"transport": "http", "host": "mcp.jira.example", "hub": False},
        "gh": {"transport": "stdio", "host": None, "hub": False},
        "other": {"transport": "sse", "host": "other.example", "hub": False},
        "selfhub": {"transport": "stdio", "host": None, "hub": True},
    }
    _no_secret(got)
    assert claude_source.read_servers(tmp_path / "missing") == {}
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / ".claude.json").write_text("{not json", encoding="utf-8")
    assert claude_source.read_servers(tmp_path / "bad") == {}
    assert claude_source.tool_server_name("a.b c") == "a_b_c" and claude_source.tool_server_name("cf-db_1") == "cf-db_1"


def test_claude_servers_rpc_is_human_only():
    assert "mcp.claude_servers" in auth.HUMAN_ONLY and "mcp.claude_servers" not in auth.MANAGER_ALLOWED
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI

    assert "claude-servers" in HUB_HUMAN_ONLY_CLI["mcp"] and "add" in HUB_HUMAN_ONLY_CLI["mcp"]


# ---------------------------------------------------------------- daemon
class HoldScript(MgrScript):
    def __call__(self, spec):
        self.specs.append(spec)
        if spec.prompt.lstrip().startswith(("[cfharness · OWNER", "[cfharness heartbeat", "[cfharness · key")):
            return {"text": "ok", "cost": 0.001, "hold": True}
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


async def _setup_agent(d, servers=("jira", "local1"), agent="w1", trust="trusted"):
    if "local1" in servers and d.hub.mcp.get_row("local1") is None:
        d.hub.mcp.add({"name": "local1", "transport": "stdio", "command": "local1-mcp", "trust": "trusted"})
    if "jira" in servers and d.hub.mcp.get_row("jira") is None:
        await rpc(d, "mcp.add", {"config": {"name": "jira", "source": "claude", "trust": trust, "tools": {"require_approval": ["approve*"]}}})
    d.hub.agents.create({"id": agent, "name": agent, "model": "haiku", "tool_policy": {"tools": TOOLS}, "mcp_servers": list(servers)})


async def _run(d, agent="w1", wait=("running",)):
    res = await rpc(d, "run.start", {"agent": agent, "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    row = await wait_status(d, run_id, wait)
    specs = [s for s in d.script.specs if s.run_id == run_id]
    return run_id, (specs[-1] if specs else None), row


async def test_rpc_list_and_add_checks_account_config(d):
    a, b, cdir = _acct(d)
    res = await rpc(d, "mcp.claude_servers", {})
    assert res["account"] == a and res["exists"] and res["config_path"] == str(cdir / ".claude.json")
    assert [s["name"] for s in res["servers"]] == ["gh", "jira", "other", "selfhub"]
    assert all(not s["registered"] for s in res["servers"])
    _no_secret(res)
    out = await rpc(d, "mcp.add", {"config": {"name": "jira", "source": "claude", "trust": "trusted", "command": "evil",
                                              "headers": {"X": "y"}, "url": "https://attacker.example"}})
    assert out["account"] == a and out["source"] == "claude" and out["transport"] == "http" and "probe" not in out
    assert out["config"]["url"] == "https://mcp.jira.example"  # host lấy từ cấu hình claude, không theo tham số
    assert not {"command", "headers", "env", "args", "auth"} & set(out["config"])
    _no_secret(out)
    _no_secret(d.hub.store.conn.execute("SELECT * FROM mcp_servers").fetchall())
    _no_secret((d.hub.paths.mcp_registry).read_text(encoding="utf-8"))
    res = await rpc(d, "mcp.claude_servers", {})
    assert {s["name"]: s["registered_source"] for s in res["servers"]}["jira"] == "claude"
    with pytest.raises(HubError) as ei:  # không có trong cấu hình của tài khoản đang dùng
        await rpc(d, "mcp.add", {"config": {"name": "nope", "source": "claude"}})
    assert a in str(ei.value) and "nope" in str(ei.value)
    with pytest.raises(HubError):  # server trỏ về chính hub
        await rpc(d, "mcp.add", {"config": {"name": "selfhub", "source": "claude"}})
    with pytest.raises(HubError):  # tài khoản B không có cấu hình
        await rpc(d, "mcp.add", {"config": {"name": "gh", "source": "claude"}, "account_id": b})
    pr = await rpc(d, "mcp.probe", {"name": "jira"})
    assert pr["ok"] is False and "nguồn claude" in pr["error"]
    with pytest.raises(HubError):  # không đổi được nguồn
        await rpc(d, "mcp.update", {"name": "jira", "patch": {"source": "hub", "command": "x"}})


async def test_run_uses_account_config_without_strict(d):
    a, _, cdir = _acct(d)
    await _setup_agent(d)
    run_id, spec, _ = await _run(d)
    # mcp.json: server hub + hub-guard, KHÔNG có server nguồn claude
    mcp = json.loads(spec.mcp_config_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "local1" in mcp and "hub-guard" in mcp and "jira" not in mcp
    _no_secret(mcp)
    assert spec.isolation_overrides == {"setting_sources": "user", "strict_mcp_config": False}
    assert spec.connectors is None and spec.claude_mcp_servers == ["jira"]
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert "--strict-mcp-config" not in argv and argv[argv.index("--setting-sources") + 1] == "user"
    dis = argv[argv.index("--disallowedTools") + 1:]
    assert "mcp__gh__*" in dis and "mcp__other__*" in dis and "mcp__selfhub__*" in dis
    assert not any(x.startswith(("mcp__jira", "mcp__local1")) for x in dis)
    # CLAUDE_CONFIG_DIR = thư mục của tài khoản đang dùng (token MCP của claude theo thư mục), vẫn token của tài khoản
    assert spec.auth_mode == "token" and spec.claude_config_dir == cdir
    env = ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=d.hub.secrets)._run_env(spec)
    assert env["CLAUDE_CONFIG_DIR"] == str(cdir) and env[OAUTH_TOKEN_SECRET] == TOK_A
    assert d.hub.runs.active[run_id].claude_session == a and d.hub.runs.active[run_id].claude_mcp == ("jira",)
    # sandbox mạng: host của server nguồn claude
    assert "mcp.jira.example" in d.hub.guard.agent_mcp_domains("w1")
    # adapter: server đã gán chưa đăng nhập → mcp.unavailable; server lạ (ngoài cấu hình) → isolation.violation; server không gán
    # nhưng có trong cấu hình claude của tài khoản → không vi phạm (tool đã bị gỡ khỏi ngữ cảnh + guard chặn)
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": [], "plugins": [],
            "mcp_servers": [{"name": "jira", "status": "needs-auth"}, {"name": "gh", "status": "connected"},
                            {"name": "local1", "status": "connected"}, {"name": "hub-guard", "status": "connected"},
                            {"name": "evil", "status": "connected"}]}
    evs = make_adapter(spec).feed(init)
    assert [(e.payload["server"], e.payload["status"]) for e in evs if e.type == "mcp.unavailable"] == [("jira", "needs-auth")]
    assert [e.payload.get("name") or e.payload for e in evs if e.type == "isolation.violation"] and \
        all("evil" in json.dumps(e.payload) for e in evs if e.type == "isolation.violation")
    init["mcp_servers"][0]["status"] = "pending"
    assert not [e for e in make_adapter(spec).feed(init) if e.type == "mcp.unavailable"]
    init["mcp_servers"] = init["mcp_servers"][1:]
    assert [(e.payload["server"], e.payload["status"]) for e in make_adapter(spec).feed(init) if e.type == "mcp.unavailable"] == [("jira", "missing")]


async def test_guard_verdicts_for_claude_source(d):
    _acct(d)
    await _setup_agent(d)
    run_id, _, _ = await _run(d)
    ctx = d.hub.guard.context_for(d.hub.store.get_run(run_id))
    assert ctx.connectors == () and "jira" in ctx.run_mcp_servers  # run "connector": MCP lạ bị chặn cứng
    v = classify("mcp__jira__getIssue", {}, ctx)
    assert v.verdict == "safe" and v.rule == "mcp_claude_read_name"  # không phải mcp_unknown_tool
    v = classify("mcp__jira__createIssue", {}, ctx)
    assert v.verdict == "write" and v.rule == "mcp_claude_write"
    assert classify("mcp__jira__deleteIssue", {}, ctx).verdict == "destructive"
    v = classify("mcp__jira__approveRequest", {}, ctx)  # tools.require_approval vẫn giữ
    assert v.verdict == "uncertain" and v.rule == "mcp_require_approval"
    for t in ("mcp__gh__create_issue", "mcp__other__list", "mcp__evil__x"):  # không gán / lạ → chặn cứng
        v = classify(t, {}, ctx)
        assert v.forbidden and v.rule == "mcp_not_in_run", t
    # ghi đè của người thắng suy theo tên
    await rpc(d, "mcp.update", {"name": "jira", "patch": {"tools": {"annotations": {"createIssue": {"readOnlyHint": True},
                                                                                    "getSecret*": {"readOnlyHint": False},
                                                                                    "sync*": {"destructiveHint": True}}}}})
    assert classify("mcp__jira__createIssue", {}, ctx).verdict == "safe"
    assert classify("mcp__jira__getSecretValue", {}, ctx).rule == "mcp_claude_write"
    assert classify("mcp__jira__syncAll", {}, ctx).rule == "mcp_destructive_hint"
    # exclude glob (không thành --disallowedTools được vì không probe) → guard chặn
    await rpc(d, "mcp.update", {"name": "jira", "patch": {"tools": {"exclude": ["admin*"]}}})
    v = classify("mcp__jira__adminPanel", {}, ctx)
    assert v.forbidden and v.rule == "mcp_not_in_run"
    # untrusted: tool ghi → hỏi duyệt (như mọi server untrusted), tool đọc theo tên vẫn safe
    await rpc(d, "mcp.update", {"name": "jira", "patch": {"trust": "untrusted"}})
    assert classify("mcp__jira__transitionIssue", {}, ctx).rule == "mcp_untrusted_write"
    assert classify("mcp__jira__searchIssues", {}, ctx).verdict == "safe"


async def test_tool_write_watcher_audits_claude_source(d):
    _acct(d)
    await _setup_agent(d)
    sub = d.hub.bus.subscribe(None, level="summary")
    for tid, name in (("t1", "mcp__jira__getIssue"), ("t2", "mcp__jira__createIssue")):
        d.hub.publish(make_event("tool.start", "w1", {"tool_id": tid, "name": name, "input": {"k": "v"}}, run_id="r_x"))
    await asyncio.sleep(0.05)
    tw = [e for e in sub.drain() if e.type == "tool.write"]
    assert [e.payload["tool"] for e in tw] == ["createIssue"] and tw[0].payload["annotations_known"] is False
    rows = d.hub.audit.query(kind="tool.write")
    assert [r["target"] for r in rows] == ["jira/createIssue"] and rows[0]["data"]["source"] == "claude"


async def test_missing_server_in_active_account_fails_closed(d):
    a, b, cdir = _acct(d)
    await _setup_agent(d, servers=("jira",))
    d.hub.accounts.use(b)  # B không có `.claude.json` → server đã gán không tồn tại
    run_id, spec, row = await _run(d, wait=("failed",))
    assert spec is None and row["outcome"] == "failed"
    assert "jira" in row["error"] and b in row["error"]
    d.hub.accounts.use(a)
    _write_cj(cdir, {"mcpServers": {"gh": {"command": "x"}}})  # A bỏ server khỏi cấu hình
    run_id, spec, row = await _run(d, wait=("failed",))
    assert spec is None and "jira" in row["error"] and a in row["error"]


async def test_guard_disabled_fails_run(d):
    _acct(d)
    await _setup_agent(d, servers=("jira",))
    d.hub.config.set("guard.enabled", False)
    _, spec, row = await _run(d, wait=("failed",))
    assert spec is None and "guard.enabled" in row["error"]


async def test_hub_only_agent_keeps_strict(d):
    _acct(d)
    await _setup_agent(d, servers=("local1",))
    _, spec, _ = await _run(d)
    assert spec.isolation_overrides is None and spec.claude_mcp_servers is None and spec.claude_config_dir is None
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    assert "--strict-mcp-config" in argv and not any(str(x).startswith("mcp__gh") for x in argv)
    assert d.hub.guard.context_for(d.hub.store.get_run(spec.run_id)).connectors is None


async def test_machine_account_uses_default_config(d, tmp_path):
    _write_cj(tmp_path / "claude-home")  # .claude.json của cấu hình mặc định (CLAUDE_CONFIG_DIR không đặt)
    d.hub.accounts.use("machine")
    await rpc(d, "mcp.add", {"config": {"name": "other", "source": "claude"}})
    d.hub.agents.create({"id": "w2", "name": "w2", "model": "haiku", "tool_policy": {"tools": TOOLS}, "mcp_servers": ["other"]})
    _, spec, _ = await _run(d, agent="w2")
    assert spec.claude_config_dir is None and spec.isolation_overrides["strict_mcp_config"] is False
    assert "mcp__jira__*" in spec.tool_policy["disallowed_tools"] and "mcp__other__*" not in spec.tool_policy["disallowed_tools"]
    assert "CLAUDE_CONFIG_DIR" not in ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=d.hub.secrets)._run_env(spec)


async def test_technical_runs_keep_strict(d):
    _acct(d)
    await _setup_agent(d, servers=("jira",))
    agent = d.hub.store.get_agent("w1")
    for run in ({"kind": "review"}, {"kind": "eval"}, {"kind": "adhoc", "run_opts": {"learning_review": True}},
                {"kind": "adhoc", "run_opts": {"eval_overlay": {"x": 1}}}):
        assert d.hub.runs._agent_claude_mcp(run, agent) == []
    assert d.hub.runs._agent_claude_mcp({"kind": "adhoc"}, agent) == ["jira"]


async def test_manager_registry_list_shows_source(d):
    _acct(d)
    await _setup_agent(d)
    res = await manager_run(d)
    rows = {m["name"]: m for m in await tool(d, res["run_id"], "mcp_registry_list")}
    assert rows["jira"]["source"] == "claude" and rows["local1"]["source"] == "hub"
    _no_secret(rows)


# ---------------------------------------------------------------- claude giả thật (ClaudeCliRuntime): init theo CLAUDE_CONFIG_DIR
FAKE = r'''
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)"); sys.exit(0)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "s"
sys.stdin.readline()
cdir = os.environ.get("CLAUDE_CONFIG_DIR")
with open(os.environ["FAKE_SEEN"], "w") as f:
    json.dump({"argv": args, "config_dir": cdir, "token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}, f)
names = []
if cdir and "--strict-mcp-config" not in args:
    names = list(json.load(open(os.path.join(cdir, ".claude.json"))).get("mcpServers") or {})
st = json.loads(os.environ.get("FAKE_STATUS") or "{}")
servers = [{"name": n, "status": st.get(n, "connected")} for n in names] + [{"name": "hub-guard", "status": "connected"},
                                                                            {"name": "evil", "status": "connected"}]
def out(o):
    o.setdefault("session_id", sid); print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": ".", "tools": [], "mcp_servers": servers,
     "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302"})
out({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.001, "usage": {"input_tokens": 1, "output_tokens": 1},
     "result": "ok", "duration_ms": 5, "num_turns": 1})
'''


@pytest.mark.skipif(sys.platform == "win32", reason="claude giả là shim sh")
async def test_fake_claude_e2e_init_status(hub_home, tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from conftest import fake_cli

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "uh"))
    (tmp_path / "uh").mkdir()
    script = tmp_path / "fake_claude_mcp.py"
    script.write_text(FAKE, encoding="utf-8")
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_SEEN", str(seen))
    monkeypatch.setenv("FAKE_STATUS", json.dumps({"jira": "needs-auth"}))
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(fake_cli(tmp_path, script))}
    hub = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    hub.start()
    try:
        a = hub.accounts.add_token("A", TOK_A)["id"]
        cdir = claude_config.ensure(hub.paths.home, a)
        _write_cj(cdir)
        hub.accounts.use(a)
        hub.mcp.add({"name": "jira", "source": "claude", "transport": "http", "url": "https://mcp.jira.example"})
        hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS}, "mcp_servers": ["jira"]})
        run = hub.runs.submit("w1", "xin chào")
        final = await hub.runs.wait(run["id"], 20)
        assert final["outcome"] == "completed", final["error"]
        got = json.loads(seen.read_text(encoding="utf-8"))
        assert got["config_dir"] == str(cdir) and "--strict-mcp-config" not in got["argv"]
        assert "mcp__gh__*" in got["argv"] and "mcp__jira__*" not in got["argv"]
        un = hub.store.query_events(run_id=run["id"], types=["mcp.unavailable"])
        assert [(e.payload["server"], e.payload["status"]) for e in un] == [("jira", "needs-auth")]
        viol = final["isolation_violations"] or []
        assert "evil" in json.dumps(viol) and not any(n in json.dumps(viol) for n in ('"gh"', '"jira"', '"other"'))
    finally:
        await hub.dispatcher.stop()
        hub.store.close()


async def test_org_managed_and_seen_servers_are_removed_from_context(d):
    """Server tổ chức cấp (`remote-settings.json.managedMcpServers`, thấy thật trên 2.1.329) + server lạ đã gặp ở system/init của run
    trước (`mcp.claude_seen`) bị `--disallowedTools` ở run sau; liệt kê được (chỉ tên/host), đăng ký được làm nguồn claude."""
    a, _, cdir = _acct(d)
    (cdir / "remote-settings.json").write_text(json.dumps({"managedMcpServers": {
        "orgdb": {"type": "http", "url": "https://mcp-db.org.example/mcp", "oauth": {"clientId": "cid-SECRET", "callbackPort": 9009}}},
        "permissions": {}}), encoding="utf-8")
    res = await rpc(d, "mcp.claude_servers", {})
    assert "orgdb" in [s["name"] for s in res["servers"]] and "cid-SECRET" not in json.dumps(res)
    await _setup_agent(d)
    run_id, spec, _ = await _run(d)
    argv, _ = build_argv(spec, dict(DEFAULTS["runtime"]["claude"]))
    dis = argv[argv.index("--disallowedTools") + 1:]
    assert "mcp__orgdb__*" in dis and "mcp__ghost__*" not in dis
    ar = d.hub.runs.active[run_id]
    assert ar.seen_key == str(cdir)
    ar.violations.append({"kind": "mcp_server", "name": "ghost"})  # server claude nạp từ nguồn hub không đọc được
    ar.violations.append({"kind": "hook", "name": "SessionStart:startup"})
    d.hub.runs._remember_claude_seen(ar)
    assert d.hub.store.state_get("mcp.claude_seen") == {str(cdir): ["ghost"]}
    await rpc(d, "run.stop", {"id": run_id})
    _, spec2, _ = await _run(d)
    dis2 = build_argv(spec2, dict(DEFAULTS["runtime"]["claude"]))[0]
    assert "mcp__ghost__*" in dis2 and "mcp__orgdb__*" in dis2
    out = await rpc(d, "mcp.add", {"config": {"name": "orgdb", "source": "claude", "trust": "trusted"}})
    assert out["config"]["url"] == "https://mcp-db.org.example" and "cid-SECRET" not in json.dumps(out)
