"""Connector claude.ai cho điều phối viên (`manager.capabilities.connectors`) — docs/dev-notes/manager-connectors.md.

Kiểm: kiểm/resolve tên connector (cần mcp=trusted); run manager có connector → `--setting-sources user`, bỏ `--strict-mcp-config`,
plugin user bị tắt, hook/MCP user là "đã biết" cho kiểm cô lập; guard chặn cứng MCP ngoài agent + connector cho phép, connector
cho phép thì đọc=safe / ghi=write / xoá=approval; run không connector giữ nguyên. Không gọi claude thật.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from cfharness.claude_adapter import ClaudeStreamAdapter
from cfharness.config import DEFAULTS, validate_setting
from cfharness.guard.classifier import classify
from cfharness.manager.connectors import run_setup, user_claude_extras
from cfharness.manager.permissions import connector_server, connector_slug, resolve_capabilities
from cfharness.runtime import RunSpec, build_argv
from m7_fake import close, make_daemon, rpc, wait_status

HERMES_TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob", "TodoWrite", "WebFetch", "WebSearch"]


@pytest.fixture(autouse=True)
def _os_sandbox(monkeypatch):
    monkeypatch.setattr("cfharness.manager.sandbox_gate.os_sandbox_available", lambda: True)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """~ giả có cấu hình Claude Code của người: một hook, hai plugin (một bật), một MCP user."""
    h = tmp_path / "userhome"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "enabledPlugins": {"vercel@official": True, "old@x": False},
    }), encoding="utf-8")
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx"}, "video-context": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


# ------------------------------------------------------------------ tên + khung
def test_slug_and_server_names():
    assert connector_slug("Claude Docs") == "Claude_Docs"
    assert connector_slug("claude.ai Claude Docs") == connector_slug("claude_ai_Claude_Docs") == "Claude_Docs"
    assert connector_server("Coupler.io") == "claude_ai_Coupler_io"
    assert connector_server("Abacum (MCP)") == "claude_ai_Abacum__MCP_"


def test_resolve_needs_trusted_mcp():
    ok = resolve_capabilities({"profile": "hermes", "mcp": "trusted", "connectors": ["Claude Docs", "claude.ai Claude Docs", "Atlassian"]})
    assert ok["connectors"] == ("Claude_Docs", "Atlassian") and not ok["warnings"]
    ro = resolve_capabilities({"profile": "hermes", "connectors": ["Claude Docs"]})  # hermes mặc định read_only_trusted
    assert ro["connectors"] == () and any("trusted" in w for w in ro["warnings"])
    assert resolve_capabilities({"profile": "hermes", "mcp": "trusted"})["connectors"] == ()
    assert resolve_capabilities({"profile": "hermes", "mcp": "trusted", "connectors": "Claude Docs"})["connectors"] == ()


def test_validate_setting_connectors():
    validate_setting("manager.capabilities.connectors", ["Claude Docs", "Google Drive"])
    validate_setting("manager.capabilities.connectors", None)
    for bad in ("Claude Docs", [1], ["$(rm -rf)"], ["../x"], [""]):
        with pytest.raises(ValueError):
            validate_setting("manager.capabilities.connectors", bad)


# ------------------------------------------------------------------ argv + spec
def _spec(**kw):
    base = dict(run_id="r_1", agent_id="boss", prompt="hi", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"), settings_path=Path("/s.json"), mcp_config_path=Path("/m.json"))
    base.update(kw)
    return RunSpec(**base)


def test_argv_isolation_override():
    c = {**DEFAULTS["runtime"]["claude"]}
    argv, _ = build_argv(_spec(), c)
    assert argv[argv.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in argv
    argv, _ = build_argv(_spec(isolation_overrides={"setting_sources": "user", "strict_mcp_config": False}), c)
    assert argv[argv.index("--setting-sources") + 1] == "user" and "--strict-mcp-config" not in argv
    assert "--mcp-config" in argv and "--settings" in argv  # file của hub vẫn được truyền


def test_run_setup_reads_user_config(user_home):
    ex = user_claude_extras()
    assert ex == {"hooks": ["SessionStart"], "plugins": ["vercel@official"], "mcp_servers": ["playwright", "video-context"]}
    st = run_setup(("Claude_Docs",))
    assert st["isolation_overrides"] == {"setting_sources": "user", "strict_mcp_config": False}
    assert st["settings_patch"] == {"enabledPlugins": {"vercel@official": False}}
    assert st["allowed_hooks"] == ["SessionStart"] and st["allowed_mcp"] == ["playwright", "video-context"]


def test_run_setup_missing_user_files(tmp_path):
    st = run_setup(("X",), home=tmp_path)
    assert st["settings_patch"] == {} and st["allowed_hooks"] == [] and st["allowed_mcp"] == []


def test_adapter_connector_prefix_not_violation():
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": [], "plugins": [],
            "mcp_servers": [{"name": "claude.ai Claude Docs", "status": "connected"}, {"name": "rogue", "status": "connected"}]}
    a = ClaudeStreamAdapter("boss", "r_1", allowed_mcp_servers=["hub-guard"], allowed_mcp_prefixes=["claude.ai "])
    evs = a.feed(init)
    names = [e.payload.get("name") for e in evs if e.type == "isolation.violation"]
    assert names == ["rogue"]
    b = ClaudeStreamAdapter("boss", "r_1", allowed_mcp_servers=["hub-guard"])
    evs = b.feed(init)
    assert sorted(e.payload.get("name") for e in evs if e.type == "isolation.violation") == ["claude.ai Claude Docs", "rogue"]


async def _connector_manager(d, connectors):
    d.hub.config.set("manager.capabilities", {"profile": "hermes", "tools": None, "mcp": "trusted", "web": None, "connectors": connectors})
    d.hub.mcp.add({"name": "docs", "transport": "stdio", "command": "/bin/echo", "trust": "trusted"})
    d.hub.agents.create({"id": "boss", "name": "boss", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": HERMES_TOOLS}, "mcp_servers": ["docs"]})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    await wait_status(d, res["run_id"])
    return [s for s in d.script.specs if s.run_id == res["run_id"]][-1], res["run_id"]


async def test_manager_run_spec_with_connectors(d, user_home):
    spec, _ = await _connector_manager(d, ["Claude Docs"])
    assert spec.isolation_overrides == {"setting_sources": "user", "strict_mcp_config": False}
    assert spec.allowed_mcp_prefixes and "claude.ai " in spec.allowed_mcp_prefixes
    assert {"playwright", "video-context", "docs", "hub-guard", "hub-self"} <= set(spec.allowed_mcp_servers)
    assert "SessionStart" in spec.allowed_hooks
    st = json.loads(spec.settings_path.read_text())
    assert st["enabledPlugins"] == {"vercel@official": False}
    assert st["hooks"]["PreToolUse"] and spec.permission_prompt_tool  # guard vẫn đủ
    assert "ToolSearch" in spec.tool_policy["tools"]  # schema tool connector nạp hoãn, không nhét hết vào ngữ cảnh


async def test_manager_run_without_connectors_unchanged(d, user_home):
    spec, _ = await _connector_manager(d, None)
    assert spec.isolation_overrides is None and not spec.allowed_mcp_prefixes
    assert "playwright" not in (spec.allowed_mcp_servers or [])
    assert "enabledPlugins" not in json.loads(spec.settings_path.read_text())
    assert "ToolSearch" not in spec.tool_policy["tools"]


async def test_connectors_need_guard(d, user_home):
    """Không có hook guard = không lớp nào chặn MCP lạ của ~/.claude → run (nếu vẫn được tạo) KHÔNG bật connector."""
    await _connector_manager(d, ["Claude Docs"])
    d.hub.config.set("guard.enabled", False)
    try:
        res = await rpc(d, "manager.chat", {"channel": "web", "text": "again"})
    except Exception:
        return  # manager từ chối khung rộng khi guard tắt (lớp có sẵn) — cũng đạt
    await wait_status(d, res["run_id"], ("running", "failed", "blocked", "completed"))
    specs = [s for s in d.script.specs if s.run_id == res["run_id"]]
    assert all(s.isolation_overrides is None for s in specs)


# ------------------------------------------------------------------ guard
async def test_guard_connector_rules(d, user_home):
    _, run_id = await _connector_manager(d, ["Claude Docs", "Abacum (MCP)"])
    run = d.hub.store.get_run(run_id)
    ctx = d.hub.guard.context_for(run)
    assert ctx.connectors == ("Claude_Docs", "Abacum__MCP_") and ctx.run_mcp_servers == frozenset({"docs"})
    v = lambda name: classify(name, {}, ctx)  # noqa: E731
    assert v("mcp__claude_ai_Claude_Docs__read").verdict == "safe"
    assert v("mcp__claude_ai_Claude_Docs__guide").verdict == "safe"
    w = v("mcp__claude_ai_Claude_Docs__update")
    assert w.verdict == "write" and not w.forbidden
    dl = v("mcp__claude_ai_Claude_Docs__delete")
    assert dl.verdict == "destructive" and not dl.forbidden  # xoá vẫn qua approval
    assert v("mcp__claude_ai_Abacum__MCP___get_report").verdict == "safe"  # slug có "__"
    for name in ("mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__search_threads", "mcp__playwright__browser_navigate",
                 "mcp__plugin_vercel_vercel__deploy"):
        assert v(name).forbidden, name
    assert not v("mcp__docs__search").forbidden  # MCP của agent: luật cũ
    assert v("mcp__hub-guard__approval_prompt").verdict == "safe"


async def test_guard_non_connector_run_unchanged(d, user_home):
    _, run_id = await _connector_manager(d, None)
    ctx = d.hub.guard.context_for(d.hub.store.get_run(run_id))
    assert ctx.connectors is None
    v = classify("mcp__claude_ai_Gmail__send_message", {}, ctx)
    assert v.rule != "mcp_not_in_run"
    other = dataclasses.replace(ctx, manager_run=False)
    assert other.connectors is None
