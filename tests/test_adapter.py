"""Adapter stream-json → event hub trên fixture thật (FR-RT-04, NFR-24 contract test)."""

import json

from agent_hub.claude_adapter import ClaudeStreamAdapter, adapt_lines


def types(evs):
    return [e.type for e in evs]


def test_sample_fixture_contract(sample_lines):
    assert len(sample_lines) == 35
    evs = adapt_lines("researcher", sample_lines, run_id="r1", allowed_hooks=[], allowed_mcp_servers=[])
    t = types(evs)
    assert t.count("run.started") == 1
    assert t.count("run.completed") == 1
    starts = [e for e in evs if e.type == "tool.start"]
    completes = [e for e in evs if e.type == "tool.complete"]
    assert starts and completes
    assert {e.payload["tool_id"] for e in starts} == {e.payload["tool_id"] for e in completes}
    assert starts[0].payload["name"] == "Bash"
    done = next(e for e in evs if e.type == "run.completed")
    assert done.payload["cost_usd"] == 0.00474188
    assert done.payload["outcome"] == "completed"
    assert done.payload["summary"] == "xong"
    assert done.payload["cost_basis"] == "list"
    assert done.payload["duration_ms"] == 2925
    started = next(e for e in evs if e.type == "run.started")
    assert started.payload["session_id"] == "e716ae1d-3b52-4ead-9779-1f6eab1de0aa"
    assert started.payload["model"] == "claude-haiku-5-5"
    assert started.payload["claude_code_version"] == "2.1.302"
    # mọi event mang run_id + session_id
    assert all(e.run_id == "r1" for e in evs)
    assert all(e.session_id for e in evs if e.type != "hook.event")
    # thứ tự: run.started trước tool, run.completed cuối (trước agent.status)
    assert t.index("run.started") < t.index("tool.start") < t.index("tool.complete") < t.index("run.completed")
    assert t[-1] == "agent.status"
    assert "message.complete" in t
    assert "ratelimit.update" in t
    assert "reasoning.progress" in t
    assert "raw.unknown" not in t and "raw.invalid" not in t


def test_serena_hook_flagged_as_isolation_violation(sample_lines):
    evs = adapt_lines("a", sample_lines, allowed_hooks=[])
    hooks = [e for e in evs if e.type == "hook.event"]
    assert hooks and hooks[0].payload["hook_name"] == "SessionStart:startup"
    assert hooks[0].level == "debug"
    viol = [e for e in evs if e.type == "isolation.violation"]
    assert len(viol) == 1 and viol[0].payload == {"kind": "hook", "name": "SessionStart:startup"}
    # không kiểm khi allowed_hooks=None
    assert not [e for e in adapt_lines("a", sample_lines) if e.type == "isolation.violation"]


def test_unknown_mcp_server_flagged():
    init = {"type": "system", "subtype": "init", "session_id": "s", "model": "m", "mcp_servers": [{"name": "serena", "status": "connected"}, {"name": "vb", "status": "connected"}]}
    evs = ClaudeStreamAdapter("a", allowed_mcp_servers=["vb"]).feed(json.dumps(init))
    viol = [e.payload for e in evs if e.type == "isolation.violation"]
    assert viol == [{"kind": "mcp_server", "name": "serena"}]


def test_unknown_type_and_invalid_line_do_not_break_run(sample_lines):
    lines = list(sample_lines)
    lines.insert(5, json.dumps({"type": "new_future_type", "x": 1}))
    lines.insert(6, "not json at all")
    lines.insert(7, json.dumps({"type": "system", "subtype": "brand_new"}))
    ad = ClaudeStreamAdapter("a")
    evs = [e for ln in lines for e in ad.feed(ln)]
    t = types(evs)
    assert t.count("raw.unknown") == 2
    assert t.count("raw.invalid") == 1
    assert ad.unknown_count == 2
    assert "run.completed" in t


def test_delta_events_levels(sample_lines):
    evs = adapt_lines("a", sample_lines)
    deltas = [e for e in evs if e.type in ("message.delta", "reasoning.delta", "turn.start", "tool.generating")]
    assert deltas and all(e.level == "detail" for e in deltas)
    assert all(not e.persisted for e in evs if e.type in ("message.delta", "reasoning.delta"))


def test_budget_exceeded_outcome(budget_fixture):
    lines = budget_fixture.read_text(encoding="utf-8").splitlines()
    evs = adapt_lines("a", lines)
    fin = [e for e in evs if e.type in ("run.completed", "run.failed")]
    assert len(fin) == 1 and fin[0].type == "run.failed"
    assert fin[0].payload["outcome"] == "budget_exceeded"
    assert fin[0].payload["terminal_reason"] == "budget_exhausted"
    assert fin[0].payload["cost_usd"] == 0.00051374


def test_tool_input_redacted():
    ad = ClaudeStreamAdapter("a")
    msg = {
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234'", "api_key": "xyz"}}]},
    }
    ev = ad.feed(json.dumps(msg))[0]
    assert ev.payload["input"]["api_key"] == "***"
    assert "abcdefghijklmnop1234" not in json.dumps(ev.payload)


def test_subagent_id_from_parent_tool_use_id():
    ad = ClaudeStreamAdapter("a")
    msg = {"type": "assistant", "parent_tool_use_id": "toolu_parent", "message": {"id": "m", "content": [{"type": "text", "text": "hi"}]}}
    ev = ad.feed(json.dumps(msg))[0]
    assert ev.type == "message.complete" and ev.subagent_id == "toolu_parent"


# ---- contract test trên fixture thật của spike M0 (docs/spikes/m0/fixtures) ----
from conftest import FIXTURES  # noqa: E402

ISO = dict(allowed_hooks=[], allowed_mcp_servers=[], allowed_plugin_prefixes=["cc-plugin-"], require_no_auto_memory=True)


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_isolated_fixture_no_serena_hook_no_user_mcp():
    evs = adapt_lines("a", load("claude-isolated.jsonl"), **ISO)
    assert not [e for e in evs if e.type == "hook.event"]
    viol = [e.payload for e in evs if e.type == "isolation.violation"]
    # fixture ghi khi chưa tắt auto-memory → chỉ còn vi phạm auto_memory
    assert [v["kind"] for v in viol] == ["auto_memory"]
    started = next(e for e in evs if e.type == "run.started")
    assert started.payload["mcp_servers"] == []


def test_sigint_fixture_result_aborted_tools():
    evs = adapt_lines("a", load("claude-sigint.jsonl"), **ISO)
    fin = next(e for e in evs if e.type in ("run.completed", "run.failed"))
    assert fin.payload["terminal_reason"] == "aborted_tools" and fin.payload["outcome"] == "failed"


def test_permission_denied_and_steer_fixtures_have_no_unknown():
    for name in ("claude-permission-denied.jsonl", "claude-steer.jsonl"):
        evs = adapt_lines("a", load(name), **ISO)
        assert not [e for e in evs if e.type in ("raw.unknown", "raw.invalid")], name
    evs = adapt_lines("a", load("claude-permission-denied.jsonl"), **ISO)
    pd = [e.payload for e in evs if e.type == "permission.denied"]
    assert pd and pd[0]["decision_reason_type"] == "hook"
    evs = adapt_lines("a", load("claude-steer.jsonl"), **ISO)
    assert [e for e in evs if e.type == "task.background"]


def test_hub_real_isolated_run_fixture_clean():
    """stdout thô của lần chạy thật qua agent-hub (M0, cờ cô lập theo spike): không hook, không vi phạm."""
    evs = adapt_lines("researcher", load("claude-hub-isolated-run.jsonl"), **ISO)
    t = types(evs)
    assert "hook.event" not in t and "isolation.violation" not in t
    assert "raw.unknown" not in t and "raw.invalid" not in t
    done = next(e for e in evs if e.type == "run.completed")
    assert done.payload["cost_usd"] == 0.00379252
