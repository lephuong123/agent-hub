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


async def _agent_run(d, connectors, add_mcp=None):
    if add_mcp is not None:  # MCP bên thứ ba do claude giữ: server trong cấu hình claude của tài khoản — mọi run nạp (PR7)
        add_mcp("docs")
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku",
                         "tool_policy": {"tools": TOOLS, "connectors": connectors}})
    res = await rpc(d, "run.start", {"agent": "w1", "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    await wait_status(d, run_id, ("running",))
    return [s for s in d.script.specs if s.run_id == run_id][-1], run_id


async def test_agent_run_spec_with_connectors(d, user_home, claude_mcp):
    spec, run_id = await _agent_run(d, ["Atlassian", "Slack"], claude_mcp)
    assert spec.isolation_overrides == {"setting_sources": "user", "strict_mcp_config": False}
    assert "claude.ai " in (spec.allowed_mcp_prefixes or [])
    assert spec.allowed_mcp_servers is None  # nạp MCP của tài khoản: không isolation.violation theo tên server
    assert json.loads(spec.settings_path.read_text())["enabledPlugins"] == {"vercel@official": False}
    assert "ToolSearch" in spec.tool_policy["tools"]

    # PR7: guard không lọc MCP/connector theo agent — mọi tool MCP bên thứ ba qua
    ctx = d.hub.guard.context_for(d.hub.store.get_run(run_id))
    v = lambda n: classify(n, {}, ctx)  # noqa: E731
    for name in ("mcp__claude_ai_Atlassian__getJiraIssue", "mcp__claude_ai_Slack__slack_send_message", "mcp__claude_ai_Gmail__send_message",
                 "mcp__playwright__browser_navigate", "mcp__docs__search"):
        assert v(name).verdict == "safe" and not v(name).forbidden, name


async def test_agent_run_without_connectors_unchanged(d, user_home):
    spec, run_id = await _agent_run(d, None)
    assert spec.isolation_overrides == {"strict_mcp_config": False} and not spec.allowed_mcp_prefixes  # MCP của tài khoản (PR7)
    assert "enabledPlugins" not in json.loads(spec.settings_path.read_text())
    assert "ToolSearch" in spec.tool_policy["tools"]  # MCP nạp → schema tool hoãn qua ToolSearch


async def test_agent_connectors_need_guard(d, user_home):
    d.hub.config.set("guard.enabled", False)
    spec, _ = await _agent_run(d, ["Atlassian"])
    assert spec.isolation_overrides == {"strict_mcp_config": False} and not spec.connectors  # connector (phiên claude.ai) cần guard


def test_manager_cannot_grant_connectors():
    from cfharness.manager.permissions import TOOL_POLICY_FIELDS

    assert "connectors" not in TOOL_POLICY_FIELDS  # whitelist trường điều phối viên được đặt — connector chỉ người cấp
