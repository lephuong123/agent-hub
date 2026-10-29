"""CLI 2.1.311/2.1.312 — lỗi production (nhánh fix/adapter-cli-312).

1. `control_request can_use_tool SandboxNetworkAccess` (Bash trong sandbox ra host ngoài allowlist): hub phải trả
   `control_response` qua stdin (trước đây không trả → lệnh treo > 120 s, curl `000`). Quyết định đi qua guard
   (`GuardService.sandbox_network`), FAIL-CLOSED.
2. `tool_progress` (heartbeat 30 s của Bash dài) và `system/vcs_state_changed` không còn là `raw.unknown`.
3. Fixture thật 2.1.311/2.1.312 + đăng ký phiên bản đã kiểm.
4. `cli.version_warning` một lần mỗi phiên bản mỗi daemon.
5. `system/init` thứ 2+ trong một tiến trình (thông báo task nền mở lượt mới) không phát thêm `run.started`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cfharness.claude_adapter import ClaudeStreamAdapter, adapt_lines
from cfharness.config import DEFAULTS
from cfharness.events import EVENT_LEVELS, NOT_PERSISTED, make_event
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.hub import Hub
from cfharness.oscompat import IS_WINDOWS
from cfharness.runtime import ClaudeCliRuntime, FakeRuntime
from cfharness.runtime.claude_cli import control_response_line
from conftest import fake_cli

FIXTURES = Path(__file__).parent / "fixtures"
FAKE = Path(__file__).parent / "fake_claude.py"
REQ = {"type": "control_request", "request_id": "34ab7bd9-de35-49e0-82fb-9b9c21aa301f", "request": {
    "subtype": "can_use_tool", "tool_name": "SandboxNetworkAccess", "display_name": "SandboxNetworkAccess", "input": {"host": "hanoimoi.vn"},
    "tool_use_id": "c939f624", "description": "Allow network connection to hanoimoi.vn?"}}


def load(ver: str, name: str) -> list[str]:
    return [ln for ln in (FIXTURES / f"claude-{ver}" / name).read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------- adapter (2, 5)
def test_tool_progress_is_debug_event_not_unknown_and_not_persisted():
    ad = ClaudeStreamAdapter("a", "r1")
    evs = ad.feed(json.dumps({"type": "tool_progress", "tool_use_id": "toolu_X-heartbeat-0", "tool_name": "Bash", "parent_tool_use_id": "toolu_X",
                              "elapsed_time_seconds": 30, "heartbeat": True, "session_id": "s"}))
    assert [e.type for e in evs] == ["tool.progress"]
    e = evs[0]
    assert e.payload == {"tool_id": "toolu_X", "name": "Bash", "elapsed_s": 30, "heartbeat": True}
    assert e.subagent_id is None  # parent_tool_use_id ở đây là CHÍNH tool đang chạy, không phải subagent
    assert EVENT_LEVELS["tool.progress"] == "debug" and "tool.progress" in NOT_PERSISTED and not e.persisted
    assert ad.unknown_count == 0


def test_vcs_state_changed_is_status_update():
    evs = adapt_lines("a", [json.dumps({"type": "system", "subtype": "vcs_state_changed", "kind": "push", "branch": "main", "cwd": "/x"})])
    assert [e.type for e in evs] == ["status.update"]
    assert evs[0].payload == {"status": "vcs_state_changed", "vcs_kind": "push", "branch": "main"}


def test_control_request_is_debug_event_not_unknown():
    ad = ClaudeStreamAdapter("a", "r1")
    evs = ad.feed(json.dumps(REQ))
    assert [e.type for e in evs] == ["control.request"]
    assert evs[0].payload == {"request_id": REQ["request_id"], "subtype": "can_use_tool", "tool_name": "SandboxNetworkAccess", "host": "hanoimoi.vn"}
    assert EVENT_LEVELS["control.request"] == "debug" and ad.unknown_count == 0


def test_second_init_does_not_emit_run_started_again():
    init = {"type": "system", "subtype": "init", "session_id": "s", "model": "m", "tools": [], "mcp_servers": [], "claude_code_version": "2.1.312"}
    ad = ClaudeStreamAdapter("a", "r1")
    first = ad.feed(json.dumps(init))
    ad.feed(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.1}))
    second = ad.feed(json.dumps(init))
    assert [e.type for e in first] == ["run.started", "agent.status"]
    assert [e.type for e in second] == ["status.update"]
    assert second[0].payload["status"] == "init" and second[0].payload["init_count"] == 2
    assert ad.init_count == 2


def test_second_init_still_checks_isolation():
    init = {"type": "system", "subtype": "init", "session_id": "s", "mcp_servers": []}
    ad = ClaudeStreamAdapter("a", "r1", allowed_mcp_servers=[])
    ad.feed(json.dumps(init))
    evs = ad.feed(json.dumps({**init, "mcp_servers": [{"name": "rogue", "status": "connected"}]}))
    assert "isolation.violation" in [e.type for e in evs]


# ---------------------------------------------------------------------------- fixture thật (3)
SETS = {
    "2.1.311": {"claude-sandbox-network.jsonl", "claude-vcs-state.jsonl"},
    "2.1.312": {"claude-background-turn.jsonl", "claude-vcs-state.jsonl"},
}


@pytest.mark.parametrize("ver", sorted(SETS))
def test_fixture_set(ver):
    assert {p.name for p in (FIXTURES / f"claude-{ver}").glob("*.jsonl")} == SETS[ver]


@pytest.mark.parametrize("ver,name", [(v, n) for v, ns in sorted(SETS.items()) for n in sorted(ns)])
def test_real_fixture_parses_cleanly(ver, name):
    lines = load(ver, name)
    init = next(json.loads(ln) for ln in lines if '"subtype":"init"' in ln)
    assert init["claude_code_version"] == ver
    evs = adapt_lines("a", lines)
    t = [e.type for e in evs]
    assert "raw.unknown" not in t and "raw.invalid" not in t, [e.payload for e in evs if e.type == "raw.unknown"][:3]
    assert t.count("run.started") == 1
    raw = "\n".join(lines).lower()
    assert "phuong" not in raw and "lequyp" not in raw and "@creativeforce" not in raw


def test_real_sandbox_network_fixture_311():
    evs = adapt_lines("a", load("2.1.311", "claude-sandbox-network.jsonl"))
    reqs = [e.payload for e in evs if e.type == "control.request"]
    assert [r["host"] for r in reqs] == ["hanoimoi.vn", "thanhnien.vn", "caicachhanhchinh.hanoi.gov.vn"]
    assert all(r["subtype"] == "can_use_tool" and r["tool_name"] == "SandboxNetworkAccess" for r in reqs)
    prog = [e.payload for e in evs if e.type == "tool.progress"]
    assert [p["elapsed_s"] for p in prog] == [30, 60, 90, 120] and {p["tool_id"] for p in prog} == {"toolu_01RcmQbEduwNUqtrxWxcqEYS"}


def test_real_background_turn_fixture_312_has_two_inits_one_run_started():
    lines = load("2.1.312", "claude-background-turn.jsonl")
    assert sum('"subtype":"init"' in ln for ln in lines) == 2
    evs = adapt_lines("a", lines)
    assert [e.type for e in evs].count("run.started") == 1
    assert [e.payload.get("init_count") for e in evs if e.type == "status.update" and e.payload.get("status") == "init"] == [2]
    assert [e for e in evs if e.type == "control.request" and e.payload["tool_name"] == "SandboxNetworkAccess"]


@pytest.mark.parametrize("ver", ["2.1.311", "2.1.312"])
def test_versions_registered(ver):
    c = DEFAULTS["runtime"]["claude"]
    assert ver in c["tested_versions"] and ver in c["flag_matrix"]
    assert set(c["flag_matrix"][ver]) == set(c["flag_matrix"]["2.1.310"])  # `claude --help` 2.1.311/2.1.312: không cờ nào bị bỏ


# ---------------------------------------------------------------------------- control_response (1)
def test_control_response_line_shape():
    """Hình dạng CLI chờ (đối chiếu mã CLI 2.1.312: StructuredIO.injectControlResponse + schema `{behavior:'allow',
    updatedInput?} | {behavior:'deny', message}`)."""
    allow = json.loads(control_response_line("req-1", {"behavior": "allow", "updatedInput": {"host": "x.vn"}}))
    assert allow == {"type": "control_response", "response": {"subtype": "success", "request_id": "req-1",
                                                               "response": {"behavior": "allow", "updatedInput": {"host": "x.vn"}}}}
    deny = json.loads(control_response_line("req-2", {"behavior": "deny", "message": "no"}))
    assert deny["response"]["response"] == {"behavior": "deny", "message": "no"}
    err = json.loads(control_response_line("req-3", None, error="unsupported"))
    assert err == {"type": "control_response", "response": {"subtype": "error", "request_id": "req-3", "error": "unsupported"}}


def _ctx(tmp_path):
    ps = PolicyStore(tmp_path / "hub")
    ps.ensure()
    return GuardContext(cwd=tmp_path, policy=ps.load(), git_probe=False)


@pytest.mark.parametrize("host,rule,forbidden", [
    ("hanoimoi.vn", "sandbox_network_not_allowed", False),
    ("169.254.169.254", "ssrf_metadata", True),
    ("127.0.0.1", "webfetch_local", False),
    ("bad host;rm", "sandbox_network_bad_host", False),
    ("", "sandbox_network_bad_host", False),
])
def test_classifier_sandbox_network(tmp_path, host, rule, forbidden):
    v = classify("SandboxNetworkAccess", {"host": host}, _ctx(tmp_path))
    assert v.rule == rule and v.forbidden is forbidden and v.klass == "network"
    assert v.verdict in ("uncertain", "destructive")


def test_sandbox_network_rule_never_downgraded():
    from cfharness.guard.classifier import NEVER_DOWNGRADE_RULES

    assert "sandbox_network_not_allowed" in NEVER_DOWNGRADE_RULES  # allowlist mạng là chính sách người: không LLM/luật "luôn" hạ


# ---------------------------------------------------------------------------- e2e với claude giả (1)
pytestmark_posix = pytest.mark.skipif(IS_WINDOWS, reason="claude giả dùng select() trên stdin (POSIX)")


@pytest.fixture
def control_log(tmp_path, monkeypatch):
    p = tmp_path / "control.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL_LOG", str(p))
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL_WAIT_S", "15")
    return p


@pytest.fixture
def cli_hub(hub_home, tmp_path, control_log):
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(fake_cli(tmp_path, FAKE))}
    h = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    h.start()
    h.agents.create({"id": "a", "name": "A"})
    yield h
    h.store.close()


def _texts(hub, run_id):
    return [e.payload["text"] for e in hub.store.query_events(run_id=run_id, types=["message.complete"])]


def _responses(path):
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


async def _wait_approval(hub, run_id, n=1):
    for _ in range(300):
        rows = hub.approvals.list("pending", run_id=run_id)
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError("không có approval")


@pytestmark_posix
async def test_e2e_host_in_human_allowlist_is_allowed_without_approval(cli_hub, control_log):
    cli_hub.guard.sandbox_allow_domain("a", "*.hanoi.gov.vn")
    run = cli_hub.runs.submit("a", "netask:caicachhanhchinh.hanoi.gov.vn")
    final = await cli_hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed", final["error"]
    assert "net:caicachhanhchinh.hanoi.gov.vn:allow" in _texts(cli_hub, run["id"])
    resp = _responses(control_log)
    assert resp[0]["response"] == {"subtype": "success", "request_id": "req-0-caicachhanhchinh.hanoi.gov.vn",
                                   "response": {"behavior": "allow", "updatedInput": {"host": "caicachhanhchinh.hanoi.gov.vn"}}}
    assert not cli_hub.approvals.list("all", run_id=run["id"])
    ev = cli_hub.store.query_events(run_id=run["id"], types=["sandbox.network_request"])
    assert [(e.payload["host"], e.payload["decision"]) for e in ev] == [("caicachhanhchinh.hanoi.gov.vn", "allow")]
    types = [e.type for e in cli_hub.store.query_events(run_id=run["id"])]
    assert "raw.unknown" not in types and "control.request" in types and "tool.progress" not in types  # tool.progress không vào DB


@pytestmark_posix
async def test_e2e_unknown_host_opens_approval_then_human_allows(cli_hub, control_log):
    run = cli_hub.runs.submit("a", "netask:hanoimoi.vn")
    rows = await _wait_approval(cli_hub, run["id"])
    assert rows[0]["tool_name"] == "SandboxNetworkAccess" and "hanoimoi.vn" in rows[0]["preview"]
    assert not _responses(control_log)  # đang giữ chờ người: chưa trả lời CLI
    cli_hub.approvals.resolve(rows[0]["id"], "allow", by="user")
    final = await cli_hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed", final["error"]
    assert "net:hanoimoi.vn:allow" in _texts(cli_hub, run["id"])
    ev = cli_hub.store.query_events(run_id=run["id"], types=["sandbox.network_request"])
    assert ev[-1].payload["decision"] == "allow" and ev[-1].payload["approval_id"] == rows[0]["id"]


@pytestmark_posix
async def test_e2e_human_denies(cli_hub, control_log):
    run = cli_hub.runs.submit("a", "netask:thanhnien.vn")
    rows = await _wait_approval(cli_hub, run["id"])
    cli_hub.approvals.resolve(rows[0]["id"], "deny", by="user", note="không cần")
    final = await cli_hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed"
    assert "net:thanhnien.vn:deny" in _texts(cli_hub, run["id"])
    r = _responses(control_log)[0]["response"]["response"]
    assert r["behavior"] == "deny" and "DENIED" in r["message"]


@pytestmark_posix
async def test_e2e_timeout_denies_and_run_continues(cli_hub, control_log):
    """Mạng không thuộc bất biến "phá huỷ không tự từ chối khi hết giờ": hết `approval.timeout_s` → deny, run CHẠY TIẾP
    (không dừng tiến trình), approval vẫn mở cho người (deny_and_continue)."""
    cli_hub.config.set("approval.timeout_s", 0.3)
    run = cli_hub.runs.submit("a", "netask:fonts.googleapis.com")
    final = await cli_hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed" and final["status"] == "completed"
    assert "net:fonts.googleapis.com:deny" in _texts(cli_hub, run["id"])
    ap = cli_hub.approvals.list("all", run_id=run["id"])
    assert ap and ap[0]["status"] == "waiting_human" and ap[0]["timeout_action"] == "deny_and_continue"


@pytestmark_posix
async def test_e2e_handler_error_fails_closed(cli_hub, control_log, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_hub.guard, "sandbox_network", boom)
    run = cli_hub.runs.submit("a", "netask:hanoimoi.vn")
    final = await cli_hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed"
    assert "net:hanoimoi.vn:deny" in _texts(cli_hub, run["id"])


@pytestmark_posix
async def test_e2e_estop_denies_even_allowlisted_host(cli_hub, control_log, monkeypatch):
    cli_hub.guard.sandbox_allow_domain("a", "hanoimoi.vn")
    orig = cli_hub.guard.sandbox_network

    async def estop_then(*a, **k):  # ESTOP bật đúng lúc CLI hỏi: host trong allowlist vẫn bị từ chối
        cli_hub.store.state_set("estop", {"on": True, "reason": "test"})
        return await orig(*a, **k)

    monkeypatch.setattr(cli_hub.guard, "sandbox_network", estop_then)
    run = cli_hub.runs.submit("a", "netask:hanoimoi.vn")
    try:
        await cli_hub.runs.wait(run["id"], 20)
    finally:
        cli_hub.store.state_set("estop", {"on": False})
    assert "net:hanoimoi.vn:deny" in _texts(cli_hub, run["id"])
    r = _responses(control_log)[0]["response"]["response"]
    assert r["behavior"] == "deny" and "Emergency stop" in r["message"]


async def test_guard_sandbox_network_unknown_run_denies(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        res = await h.guard.sandbox_network("r_nope", "hanoimoi.vn")
        assert res["decision"] == "deny"
    finally:
        h.store.close()


# ---------------------------------------------------------------------------- cli.version_warning một lần (4)
class _ClaudeLike(FakeRuntime):
    name = "claude"


async def test_version_warning_once_per_daemon(hub_home):
    h = Hub(hub_home, runtime=_ClaudeLike(cli_version="9.9.9"))
    try:
        h.start()
        await h.compat.task
        h.agents.create({"id": "b", "name": "B"})
        r = h.runs.submit("b", "hi")
        await h.runs.wait(r["id"], 10)
        r2 = h.runs.submit("b", "hi again")
        await h.runs.wait(r2["id"], 10)
        warn = h.store.query_events(types=["cli.version_warning"])
        assert [w.payload["version"] for w in warn] == ["9.9.9"]
    finally:
        await h.stop()


async def test_version_warning_from_first_run_when_start_check_did_not_run(hub_home):
    h = Hub(hub_home, runtime=_ClaudeLike(cli_version="9.9.8"))
    try:
        h.agents.create({"id": "b", "name": "B"})
        r = h.runs.submit("b", "hi")
        await h.runs.wait(r["id"], 10)
        h.compat.record_version("9.9.8", "claude")  # kiểm lúc khởi động đến sau: không cảnh báo lần 2
        warn = h.store.query_events(types=["cli.version_warning"])
        assert [w.payload["version"] for w in warn] == ["9.9.8"]
    finally:
        h.store.close()


def test_make_event_new_types_have_levels():
    for t in ("tool.progress", "control.request", "sandbox.network_request"):
        assert make_event(t).level == EVENT_LEVELS[t]
