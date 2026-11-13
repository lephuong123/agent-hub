"""Connector claude.ai theo agent (`tool_policy.connectors`) — docs/dev-notes/manager-connectors.md.

Kiểm: kiểm/chuẩn hoá tên; run thường của agent có connector → `--setting-sources user`, bỏ `--strict-mcp-config`, plugin user tắt,
ToolSearch được thêm; guard chặn MCP ngoài agent + connector cho phép; guard tắt → không bật; điều phối viên không tự cấp được.
Không gọi claude thật.
"""

from __future__ import annotations

import json

import pytest

from cfharness.agents import ToolPolicy
from cfharness.guard.classifier import classify
from m7_fake import MgrScript, close, make_daemon, rpc, wait_status

TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]


@pytest.fixture(autouse=True)
def _os_sandbox(monkeypatch):
    monkeypatch.setattr("cfharness.manager.sandbox_gate.os_sandbox_available", lambda: True)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    h = tmp_path / "userhome"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"vercel@official": True}}), encoding="utf-8")
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


class HoldScript(MgrScript):
    """Giữ run thường ở `running` để runtime/settings.json còn trên đĩa khi test đọc."""

    def __call__(self, spec):
        self.specs.append(spec)
        return {"text": "ok", "cost": 0.001, "hold": True}


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home, HoldScript())
    yield dm
    await close(dm)


def test_tool_policy_connectors_normalized():
    assert ToolPolicy(connectors=["Atlassian", "claude.ai Claude Docs", "Claude Docs"]).connectors == ["Atlassian", "Claude_Docs"]
    assert ToolPolicy(connectors="Slack, Gmail").connectors == ["Slack", "Gmail"]
    assert ToolPolicy(connectors=[]).connectors is None and ToolPolicy().connectors is None
    for bad in (["$(rm -rf)"], ["../x"], [""], 5):
        with pytest.raises(ValueError):
            ToolPolicy(connectors=bad)


async def _agent_run(d, connectors):
    d.hub.mcp.add({"name": "docs", "transport": "stdio", "command": "/bin/echo", "trust": "trusted"})
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "mcp_servers": ["docs"],
                         "tool_policy": {"tools": TOOLS, "connectors": connectors}})
    res = await rpc(d, "run.start", {"agent": "w1", "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    await wait_status(d, run_id, ("running",))
    return [s for s in d.script.specs if s.run_id == run_id][-1], run_id


async def test_agent_run_spec_with_connectors(d, user_home):
    spec, run_id = await _agent_run(d, ["Atlassian", "Slack"])
    assert spec.isolation_overrides == {"setting_sources": "user", "strict_mcp_config": False}
    assert "claude.ai " in (spec.allowed_mcp_prefixes or [])
    assert "playwright" in spec.allowed_mcp_servers
    assert json.loads(spec.settings_path.read_text())["enabledPlugins"] == {"vercel@official": False}
    assert "ToolSearch" in spec.tool_policy["tools"]

    ctx = d.hub.guard.context_for(d.hub.store.get_run(run_id))
    assert ctx.connectors == ("Atlassian", "Slack") and ctx.run_mcp_servers == frozenset({"docs"})
    v = lambda n: classify(n, {}, ctx)  # noqa: E731
    assert v("mcp__claude_ai_Atlassian__getJiraIssue").verdict == "safe"
    assert v("mcp__claude_ai_Slack__slack_send_message").verdict == "write"
    assert v("mcp__claude_ai_Gmail__send_message").forbidden
    assert v("mcp__playwright__browser_navigate").forbidden
    assert not v("mcp__docs__search").forbidden


async def test_agent_run_without_connectors_unchanged(d, user_home):
    spec, run_id = await _agent_run(d, None)
    assert spec.isolation_overrides is None and not spec.allowed_mcp_prefixes
    assert "enabledPlugins" not in json.loads(spec.settings_path.read_text())
    assert "ToolSearch" not in spec.tool_policy["tools"]
    assert d.hub.guard.context_for(d.hub.store.get_run(run_id)).connectors is None


async def test_agent_connectors_need_guard(d, user_home):
    d.hub.config.set("guard.enabled", False)
    spec, _ = await _agent_run(d, ["Atlassian"])
    assert spec.isolation_overrides is None


def test_manager_cannot_grant_connectors():
    from cfharness.manager.permissions import TOOL_POLICY_FIELDS

    assert "connectors" not in TOOL_POLICY_FIELDS  # whitelist trường điều phối viên được đặt — connector chỉ người cấp
