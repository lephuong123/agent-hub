"""§9 M7-core.md — khung quyền điều phối viên do NGƯỜI đặt (`manager.capabilities`: strict | hermes).

Kiểm: strict mặc định giữ hành vi MGR-8; hermes cho Bash/Write/Web/MCP trusted nhưng VẪN dưới hook guard + sandbox OS + approval;
MCP untrusted/ngoài registry, bypass/acceptEdits/subagents/Task vẫn bị chặn; manager/agent không đổi được khung (config.set, Bash CLI,
socket từ tiến trình con của run manager); classifier chặn các đường vòng Bash/Write/WebFetch của run manager. Không gọi claude thật.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys

import pytest
import yaml

from cfharness import auth
from cfharness.config import validate_setting
from cfharness.errors import BLOCKED, HubError
from cfharness.guard.classifier import classify
from cfharness.guard.integrity import SECURITY_KEYS
from cfharness.manager.permissions import CAP_TOOLS_MAX, resolve_capabilities
from cfharness import transport
from cfharness.guard.runtime import sb_path
from m7_fake import close, make_daemon, manager_run, rpc, tool, wait_status

HERMES_TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob", "TodoWrite", "WebFetch", "WebSearch"]


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


def _hermes(d, **over):
    d.hub.config.set("manager.capabilities", {"profile": "hermes", "tools": None, "mcp": None, "web": None, **over})


def _mcp(d, name: str, trust: str, cache: dict | None = None) -> None:
    d.hub.mcp.add({"name": name, "transport": "stdio", "command": "/bin/echo", "trust": trust})
    if cache is not None:
        d.hub.store.conn.execute("UPDATE mcp_servers SET annotations_cache=? WHERE name=?", (json.dumps(cache), name))


def _agent(d, aid: str, **tp) -> None:
    mcp = tp.pop("mcp_servers", [])
    sub = tp.pop("subagents", None)
    spec = {"id": aid, "name": aid, "role": "hub boss", "model": "haiku", "tool_policy": tp, "mcp_servers": mcp}
    if sub:
        spec["subagents"] = sub
    d.hub.agents.create(spec)


# ------------------------------------------------------------------ strict mặc định = hành vi cũ
def test_resolve_defaults_and_fail_closed():
    s = resolve_capabilities(None)
    assert s["profile"] == "strict" and s["tools"] == {"Read", "Grep", "Glob"} and s["mcp"] == "none" and s["web"] is False
    h = resolve_capabilities({"profile": "hermes"})
    assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch"} <= h["tools"] and h["mcp"] == "read_only_trusted"
    assert "Task" not in h["tools"] and "Agent" not in h["tools"]
    # sửa tay config.yaml giá trị lạ → về strict / none (fail-closed), tool ngoài trần bị bỏ
    bad = resolve_capabilities({"profile": "god", "mcp": "all", "tools": ["Read", "Task", "Agent"]})
    assert bad["profile"] == "strict" and bad["mcp"] == "none" and bad["tools"] == {"Read"} and bad["warnings"]
    assert resolve_capabilities({"profile": "hermes", "web": False})["tools"].isdisjoint({"WebFetch", "WebSearch"})
    assert CAP_TOOLS_MAX.isdisjoint({"Task", "Agent", "KillShell"})


def test_validate_setting_rejects_bad_capabilities():
    validate_setting("manager.capabilities", {"profile": "hermes"})
    validate_setting("manager.capabilities.profile", "strict")
    for k, v in (("manager.capabilities.profile", "root"), ("manager.capabilities.tools", ["Bash", "Task"]),
                 ("manager.capabilities.mcp", "everything"), ("manager.capabilities.web", "yes"),
                 ("manager.capabilities", {"profile": "hermes", "bypass": True}), ("manager.capabilities.extra_args", ["--x"])):
        with pytest.raises(ValueError):
            validate_setting(k, v)


async def test_strict_default_keeps_mgr8(d):
    assert d.hub.config.get("manager.capabilities.profile") == "strict"
    _agent(d, "bashy", tools=["Read", "Bash"])
    with pytest.raises(HubError, match="Read/Grep/Glob"):
        await rpc(d, "manager.set", {"agent_id": "bashy"})
    _agent(d, "webby", tools=["Read", "WebFetch"])
    with pytest.raises(HubError, match="vượt|chỉ cho"):
        await rpc(d, "manager.set", {"agent_id": "webby"})
    _mcp(d, "docs", "trusted")
    _agent(d, "mcpy", tools=["Read"], mcp_servers=["docs"])
    with pytest.raises(HubError, match="MCP"):
        await rpc(d, "manager.set", {"agent_id": "mcpy"})
    _agent(d, "ro", tools=["Read", "Grep", "Glob"])
    await rpc(d, "manager.set", {"agent_id": "ro"})
    info = await rpc(d, "manager.capabilities")
    assert info["effective"]["profile"] == "strict" and info["problem"] is None and info["manager"] == "ro"


# ------------------------------------------------------------------ hermes: nới có kiểm soát
async def test_hermes_allows_bash_web_trusted_mcp(d):
    _hermes(d)
    _mcp(d, "docs", "trusted")
    _agent(d, "boss", tools=HERMES_TOOLS, allowed_tools=["Bash(git status:*)", "mcp__docs"], mcp_servers=["docs"])
    await rpc(d, "manager.set", {"agent_id": "boss"})
    info = await rpc(d, "manager.capabilities")
    assert info["problem"] is None and "Bash" in info["effective"]["tools"]


async def test_hermes_still_blocks_untrusted_unregistered_and_escalation(d):
    _hermes(d)
    _mcp(d, "shady", "untrusted")
    cases = {
        "u1": dict(tools=["Read", "Bash"], mcp_servers=["shady"]),          # MCP untrusted
        "u3": dict(tools=["Read", "Bash", "Task"]),                         # subagent tool
        "u4": dict(tools=None),                                             # mọi tool (có Task)
        "u5": dict(tools=["Read", "Bash"], permission_mode="acceptEdits"),
        "u6": dict(tools=["Read", "Bash"], permission_mode="bypassPermissions"),
        "u7": dict(tools=["Read", "Bash"], allowed_tools=["Task"]),
        "u8": dict(tools=["Read", "Bash"], subagents={"x": {"description": "x", "prompt": "y", "permissionMode": "bypassPermissions"}}),
    }
    for aid, tp in cases.items():
        if tp.get("tools") is None:
            tp.pop("tools")
        _agent(d, aid, **tp)
        with pytest.raises(HubError) as ei:
            await rpc(d, "manager.set", {"agent_id": aid})
        assert ei.value.code == BLOCKED, (aid, ei.value.message)
    # MCP không có trong registry: agents.create đã từ chối → giả lập hàng DB bị sửa
    _agent(d, "u2", tools=["Read"])
    d.hub.store.conn.execute("UPDATE agents SET mcp_servers=? WHERE id='u2'", (json.dumps(["ghost"]),))
    assert "registry" in (d.hub.manager.perms.manager_config_problem(d.hub.store.get_agent("u2")) or "")
    # allow_bypass (chỉ người đặt được trên agent) vẫn bị khung từ chối
    _agent(d, "u9", tools=["Read", "Bash"], allow_bypass=True)
    with pytest.raises(HubError, match="allow_bypass"):
        await rpc(d, "manager.set", {"agent_id": "u9"})
    # mcp=none trong hermes → MCP trusted cũng không được
    _hermes(d, mcp="none")
    _mcp(d, "docs", "trusted")
    _agent(d, "u10", tools=["Read", "Bash"], mcp_servers=["docs"])
    with pytest.raises(HubError, match="không cho MCP"):
        await rpc(d, "manager.set", {"agent_id": "u10"})
    assert d.hub.manager.agent_id() is None


async def test_hermes_requires_guard_and_sandbox(d):
    _hermes(d)
    _agent(d, "boss", tools=HERMES_TOOLS)
    d.hub.config.set("guard.sandbox.enabled", False)
    with pytest.raises(HubError, match="sandbox"):
        await rpc(d, "manager.set", {"agent_id": "boss"})
    d.hub.config.set("guard.sandbox.enabled", True)
    d.hub.config.set("guard.enabled", False)
    with pytest.raises(HubError, match="guard.enabled"):
        await rpc(d, "manager.set", {"agent_id": "boss"})
    d.hub.config.set("guard.enabled", True)
    await rpc(d, "manager.set", {"agent_id": "boss"})


async def test_downgrade_to_strict_blocks_runs(d):
    _hermes(d)
    _agent(d, "boss", tools=HERMES_TOOLS)
    await rpc(d, "manager.set", {"agent_id": "boss"})
    d.hub.config.set("manager.capabilities", {"profile": "strict"})
    with pytest.raises(HubError) as ei:
        await rpc(d, "manager.chat", {"channel": "web", "text": "x"})
    assert "vượt khung" in ei.value.message and "manager capabilities" in ei.value.message
    # tick cũng không chạy
    res = await d.hub.manager.maybe_tick(force=True)
    assert res.get("run_id") is None


# ------------------------------------------------------------------ chỉ NGƯỜI đổi khung
async def test_manager_and_agents_cannot_change_capabilities(d):
    _hermes(d)
    _agent(d, "boss", tools=HERMES_TOOLS)
    await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    rid = res["run_id"]
    await wait_status(d, rid)
    val = {"profile": "hermes", "tools": HERMES_TOOLS + ["Task"]}
    # lớp 2: điều phối viên gọi config.set
    with pytest.raises(HubError) as ei:
        await rpc(d, "config.set", {"key": "manager.capabilities", "value": val}, as_caller=auth.manager_caller("boss", rid))
    assert ei.value.code == BLOCKED
    # agent thường qua socket (kể cả có token bị lộ) → HUMAN_ONLY
    ag = auth.Caller(via="socket", token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
    with pytest.raises(HubError):
        await rpc(d, "config.set", {"key": "manager.capabilities.profile", "value": "hermes"}, as_caller=ag)
    # tool hub-self không có thao tác cấu hình
    with pytest.raises(HubError):
        await tool(d, rid, "config_set", {"key": "manager.capabilities", "value": val})
    assert d.hub.config.get("manager.capabilities.tools") is None
    # web không sửa được khoá này
    from cfharness.web.app import SAFE_CONFIG

    assert not any(k.startswith("manager") for k, _, _ in SAFE_CONFIG)
    assert "manager" in SECURITY_KEYS


async def test_child_process_of_manager_run_cannot_use_agent_rights(d):
    """Tiến trình con (Bash) của run manager gọi THẲNG socket: chỉ managerself.call; run.start/task.create/learn.propose → từ chối."""
    _hermes(d)
    _agent(d, "boss", tools=HERMES_TOOLS)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    rid = res["run_id"]
    await wait_status(d, rid)
    child = auth.Caller(via="socket", pid=4242, token_ok=False, run_id=rid, agent_id="boss", evidence=["env_run_id"])
    for m, p in (("run.start", {"agent": "w1", "prompt": "x"}), ("task.create", {"title": "t", "body": "b", "assignee": "w1"}),
                 ("learn.propose", {"target": "memory", "op": "add", "payload": {"text": "x"}}), ("run.stop", {"id": rid}),
                 ("feedback.record", {"run": rid, "kind": "good"}), ("approval.approve", {"id": "ap_x"})):
        with pytest.raises(HubError) as ei:
            await rpc(d, m, p, as_caller=child)
        assert ei.value.code == BLOCKED, (m, ei.value.message)
    assert not [r for r in d.hub.store.conn.execute("SELECT id FROM runs WHERE agent_id='w1'")]
    # đường hợp lệ của hub-self (cùng run) vẫn chạy
    ov = await rpc(d, "managerself.call", {"run_id": rid, "op": "hub_overview", "args": {}}, as_caller=child)
    assert ov
    # agent thường (không phải run manager) vẫn dùng quyền agent như cũ
    other = auth.Caller(via="socket", pid=4243, run_id="r_other", agent_id="w1", evidence=["env_run_id"])
    assert d.hub.manager.run_socket_block(other, "run.start") is None


async def test_child_over_real_socket_denied(d):
    _hermes(d)
    _agent(d, "boss", tools=HERMES_TOOLS)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    rid = res["run_id"]
    await wait_status(d, rid)
    srv = await transport.start_server(d._handle_conn, d.paths.socket, limit=16 * 1024 * 1024)
    try:
        code = ("import json,socket,sys;from cfharness import transport;s=transport.connect(sys.argv[1],30);"
                "s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':sys.argv[2],'params':json.loads(sys.argv[3])})+'\\n').encode());"
                "print(s.makefile().readline())")
        env = {**os.environ, "CFHARNESS_RUN_ID": rid}

        async def call(method, params):
            cp = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(d.paths.socket), method, json.dumps(params)],
                                         capture_output=True, text=True, timeout=30, env=env)
            return json.loads(cp.stdout)

        r = await call("run.start", {"agent": "w1", "prompt": "x"})
        assert r.get("error", {}).get("code") == BLOCKED and "hub-self" in r["error"]["message"], r
        r = await call("managerself.call", {"run_id": rid, "op": "hub_overview", "args": {}})
        assert "result" in r, r
    finally:
        srv.close()


# ------------------------------------------------------------------ spec run manager hermes: guard + sandbox vẫn đủ
async def test_hermes_manager_run_spec_keeps_guard_and_sandbox(d):
    _hermes(d)
    _mcp(d, "docs", "trusted", cache={"search": {"name": "search", "readOnlyHint": True}, "update_page": {"name": "update_page", "readOnlyHint": None}})
    _agent(d, "boss", tools=HERMES_TOOLS, mcp_servers=["docs"])
    await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    await wait_status(d, res["run_id"])
    spec = [s for s in d.script.specs if s.run_id == res["run_id"]][-1]
    assert spec.permission_prompt_tool, "hub-guard permission-prompt-tool phải có"
    assert spec.agents_json is None
    tp = spec.tool_policy
    assert tp.get("permission_mode") in (None, "default", "plan")
    assert "mcp__docs__update_page" in tp["disallowed_tools"] and "mcp__docs__search" not in tp["disallowed_tools"]
    assert any("git push --force" in x for x in tp["disallowed_tools"])  # lớp tĩnh
    st = json.loads(spec.settings_path.read_text())
    assert st["hooks"]["PreToolUse"], "hook guard PreToolUse phải có"
    sb = st["sandbox"]
    assert sb["enabled"] is True
    home = sb_path(d.paths.home)  # Windows: sandbox dùng dấu `/`
    deny_w = sb["filesystem"]["denyWrite"]
    deny_r = sb["filesystem"]["denyRead"]
    assert any(x.endswith("config.yaml") and x.startswith(home) or x == f"{home}/config.yaml" for x in deny_w)
    assert any(x.endswith("/policy") for x in deny_w) and any(x.endswith(".auth") for x in deny_r) and any(x.endswith("/secrets") for x in deny_r)
    assert sb_path(d.paths.socket) not in (sb.get("network") or {}).get("allowUnixSockets", [])
    assert st.get("permissions", {}).get("deny"), "permissions.deny tĩnh phải có"
    mcp = json.loads(spec.mcp_config_path.read_text())["mcpServers"]
    assert "hub-guard" in mcp and "hub-self" in mcp and "docs" in mcp


# ------------------------------------------------------------------ classifier: đường vòng từ Bash/Write/Web của run manager
def _mgr_ctx(d):
    cwd = d.paths.home / "agents" / "boss" / "workspaces" / "conv-x"
    cwd.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(d.hub.guard.context_for(None, cwd=str(cwd)), manager_run=True), cwd


FORBIDDEN_FOR_MANAGER = [
    "cfharness approve ap_123", "cfharness deny ap_1", "cfharness config set manager.capabilities.profile hermes",
    "cfharness manager set w1", "cfharness manager capabilities set --profile hermes", "cfharness learn policy set enabled false",
    "cfharness guard sandbox allow-localhost boss", "uv run cfharness config set guard.enabled false", "python -m cfharness approve ap_1",
    "env CFHARNESS_HOME=/tmp/x cfharness approve ap_1", "cfharness agent edit boss --tools Bash", "cfharness golden rm g1",
    "sqlite3 {H}/hub.db 'select 1'", "sqlite3 {H}/hub.db \"update agents set tool_policy='{{}}'\"",
    "echo 'manager: {{}}' >> {H}/config.yaml", "cat > {H}/policy/protected.yaml <<X\nx\nX",
    "python3 -c \"open('{H}/config.yaml','w').write('x')\"", "cp /tmp/x {H}/agents/boss/SOUL.md",
    "nc -U {S}", "python3 -c \"import socket;s=socket.socket(socket.AF_UNIX);s.connect('{S}')\"",
    "curl -s http://cfharness.localhost:8770/", "curl -s http://127.0.0.1:8770/do/approve", "curl http://localhost:8765/mcp",
    "wget -qO- http://[::1]:8770/", "curl http://2130706433:8770/",
    "cat {H}/auth/web.json", "cat {S}.auth", "ls {H}/secrets", "cat {H}/secrets/FOO",
    "claude -p hi --dangerously-skip-permissions", "claude -p hi --permission-mode bypassPermissions", "claude -p 'do x'",
    "npx @anthropic-ai/claude-code -p hi", "echo hi > CLAUDE.md", "mkdir -p .claude && echo '{{}}' > .claude/settings.json",
]


async def test_classifier_blocks_manager_bypasses(d):
    ctx, cwd = _mgr_ctx(d)
    H, S = str(d.paths.home), str(d.paths.socket)
    for c in FORBIDDEN_FOR_MANAGER:
        cmd = c.format(H=H, S=S)
        v = classify("Bash", {"command": cmd}, ctx)
        assert v.forbidden, (cmd, v.to_dict())
    tools = [("Read", {"file_path": f"{H}/auth/web.json"}), ("Read", {"file_path": f"{S}.auth"}), ("Read", {"file_path": f"{H}/secrets/X"}),
             ("Write", {"file_path": f"{H}/config.yaml", "content": "x"}), ("Write", {"file_path": f"{H}/hub.db", "content": "x"}),
             ("Edit", {"file_path": f"{H}/policy/protected.yaml", "old_string": "a", "new_string": "b"}),
             ("Write", {"file_path": f"{H}/agents/boss/SOUL.md", "content": "x"}), ("Write", {"file_path": f"{H}/agents/boss/MEMORY.md", "content": "x"}),
             ("Write", {"file_path": f"{H}/agents/w1/config.yaml", "content": "x"}), ("Write", {"file_path": str(cwd / "CLAUDE.md"), "content": "x"}),
             ("Write", {"file_path": str(cwd / ".mcp.json"), "content": "{}"}), ("WebFetch", {"url": "http://cfharness.localhost:8770/"}),
             ("WebFetch", {"url": "http://127.0.0.1:8765/mcp"}), ("WebFetch", {"url": "http://169.254.169.254/latest/meta-data"}),
             ("Task", {"prompt": "x", "subagent_type": "general-purpose"}), ("mcp__cfharness__hub_approve", {"id": "ap"}),
             ("mcp__cfharness__hub_policy_set", {"x": 1})]
    for name, ti in tools:
        v = classify(name, ti, ctx)
        assert v.forbidden, (name, ti, v.to_dict())


async def test_classifier_manager_ordinary_work_not_blocked(d):
    ctx, cwd = _mgr_ctx(d)
    for cmd in ("ls -la", "git status", "cat README.md", "curl -s -m 3 https://example.com", "curl -o out.html https://example.com/x", "python3 -c 'print(1)'", "df -h", "echo hi > notes.md"):
        v = classify("Bash", {"command": cmd}, ctx)
        assert not v.forbidden and v.verdict in ("safe", "write"), (cmd, v.to_dict())
    for name, ti in (("WebFetch", {"url": "https://example.com/docs"}), ("WebSearch", {"query": "x"}),
                     ("Write", {"file_path": str(cwd / "a.py"), "content": "x"}), ("TodoWrite", {"todos": []})):
        v = classify(name, ti, ctx)
        assert not v.forbidden and v.verdict in ("safe", "write"), (name, v.to_dict())


async def test_classifier_normal_agent_unchanged_except_bypass(d):
    ctx = d.hub.guard.context_for(None, cwd=str(d.paths.home.parent))
    assert classify("Bash", {"command": "cfharness manager capabilities set --profile hermes"}, ctx).forbidden
    assert classify("Bash", {"command": "cfharness manager capabilities"}, ctx).forbidden
    assert not classify("Bash", {"command": "cfharness manager capabilities show"}, ctx).forbidden
    assert classify("Bash", {"command": "claude -p x --dangerously-skip-permissions"}, ctx).forbidden
    v = classify("Bash", {"command": "claude -p 'summarize'"}, ctx)
    assert not v.forbidden and v.verdict == "write"
    v = classify("WebFetch", {"url": "http://127.0.0.1:3000/"}, ctx)  # agent thường: giữ chờ người (không cấm)
    assert v.verdict == "uncertain" and not v.forbidden
    assert classify("WebFetch", {"url": "https://example.com"}, ctx).verdict == "safe"
    assert not classify("Bash", {"command": "curl http://localhost:3000/health"}, ctx).forbidden


# ------------------------------------------------------------------ toàn vẹn: ghi thẳng config.yaml bị khôi phục
async def test_capabilities_tamper_restored(d):
    d.hub.integrity.check()
    cfg = d.paths.config
    data = yaml.safe_load(cfg.read_text()) or {}
    data.setdefault("manager", {})["capabilities"] = {"profile": "hermes"}
    cfg.write_text(yaml.safe_dump(data))
    d.hub.integrity.check()
    after = yaml.safe_load(cfg.read_text()) or {}
    assert ((after.get("manager") or {}).get("capabilities") or {}).get("profile") != "hermes"
    d.hub.config.reload()
    assert d.hub.manager.perms.capabilities()["profile"] == "strict"


# ------------------------------------------------------------------ CLI: show/set (người) qua RPC thật
async def test_cli_capabilities_set_and_sync(d):
    _agent(d, "boss", tools=["Read", "Grep", "Glob"])
    await rpc(d, "manager.set", {"agent_id": "boss"})
    srv = await transport.start_server(d._handle_conn, d.paths.socket, limit=16 * 1024 * 1024)
    try:
        tok = auth.issue_token(d.paths.socket)
        d.auth_token = tok
        env = {**os.environ, "CFHARNESS_HOME": str(d.paths.home)}
        env.pop("CFHARNESS_RUN_ID", None)

        def cli(*args):
            return subprocess.run([sys.executable, "-m", "cfharness.cli", *args], capture_output=True, text=True, timeout=60, env=env)

        cp = await asyncio.to_thread(cli, "manager", "capabilities", "set", "--profile", "hermes", "--sync-agent", "--json")
        assert cp.returncode == 0, cp.stderr + cp.stdout
        info = json.loads(cp.stdout)
        assert info["effective"]["profile"] == "hermes" and info["problem"] is None
        assert "Bash" in d.hub.store.get_agent("boss")["tool_policy"]["tools"]
        audit = [r for r in d.hub.audit.query(limit=50) if r["kind"] == "config.set"]
        assert audit and audit[0]["actor"] in ("user", "human")
        cp = await asyncio.to_thread(cli, "manager", "capabilities", "reset", "--json")
        assert cp.returncode == 0 and json.loads(cp.stdout)["effective"]["profile"] == "strict"
        assert json.loads(cp.stdout)["problem"]  # agent giờ vượt khung strict → báo
    finally:
        srv.close()
