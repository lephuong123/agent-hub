"""V2-N1 (Cao, chặn phát hành — FR-AG-03, FR-SG-11): `agent.revert`, `agent.archive`, `trash.restore`, `trash.purge` chỉ NGƯỜI.

Mỗi đường vào phải tới cùng một cổng, fail closed, có audit `auth.denied`:
- daemon RPC (socket, `call_as` của kênh trong daemon, điều phối viên) — cổng `HUMAN_ONLY` ở `_dispatch`;
- tầng service (`auth.require_human` trong handler của surface_rpc) — chặn cả lời gọi nội bộ lọt qua cổng RPC;
- CLI / MCP của hub / hub-self — bộ phân loại `forbidden`, hub-self không có tool tương ứng (trừ `agent_archive` của điều phối viên).
Ca socket thật (tiến trình con không token) ở `test_sec_v2.py::test_admin_surface_requires_human_token_over_socket`.
"""

from __future__ import annotations

import time

import pytest

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.errors import BLOCKED, Blocked
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.mcp_server.hub_self import MANAGER_TOOLS, ROLE_TOOLS
from cfharness.runtime.fake import FakeRuntime

ADMIN = ("agent.revert", "agent.archive", "trash.restore", "trash.purge")
AGENT = auth.Caller(via="socket", pid=4242, token_ok=True, run_id="r_evil", agent_id="evil", evidence=["descendant_of_run_pid:4000"])
NO_TOKEN = auth.Caller(via="socket", pid=4243, token_ok=False)
CALLERS = {
    "agent_with_stolen_token": AGENT,
    "socket_no_token": NO_TOKEN,
    "telegram_not_allowlisted": auth.Caller(via="telegram:999", token_ok=False),
    "manager": auth.manager_caller("boss", "r_boss"),
}


def _params(m: str) -> dict:
    return {"agent.revert": {"id": "a2", "to": 1}, "agent.archive": {"id": "a4"},
            "trash.restore": {"id": "a3", "kind": "agent"}, "trash.purge": {"id": "a3", "kind": "agent", "confirm": "a3"}}[m]


@pytest.fixture
async def d(hub_home):
    dm = Daemon(hub_home, runtime=FakeRuntime())
    dm.hub.start()
    h = dm.hub
    h.agents.create({"id": "a2", "name": "A2", "tool_policy": {"allow_bypass": True}})
    h.agents.update("a2", {"tool_policy": {"allow_bypass": False}})  # revision 1 = allow_bypass bật (nâng quyền nếu revert)
    h.agents.create({"id": "a3", "name": "A3"})
    h.agents.remove("a3")  # trong thùng rác
    h.agents.create({"id": "a4", "name": "A4"})
    yield dm
    await h.stop()


def _untouched(dm: Daemon) -> None:
    h = dm.hub
    assert not (h.store.get_agent("a2").get("tool_policy") or {}).get("allow_bypass")
    assert h.store.get_agent("a3") is not None and h.store.get_agent("a3").get("archived_at")
    assert not h.store.get_agent("a4").get("archived_at")


def _denials(dm: Daemon) -> list[dict]:
    return dm.hub.audit.query(kind="auth.denied", limit=500)


# ============================================================================ tầng service (lớp 2 — không phụ thuộc cổng RPC)
@pytest.mark.parametrize("who", sorted(CALLERS))
@pytest.mark.parametrize("method", ADMIN)
async def test_service_gate_refuses_non_human_and_audits(d, method, who):
    """Gọi thẳng handler đã đăng ký (bỏ qua `_dispatch`, như một đường nội bộ quên cổng RPC) với người gọi không phải người → Blocked
    + audit `auth.denied` (layer=service). Điều phối viên chỉ được `agent.archive` (MANAGER_ALLOWED)."""
    caller = CALLERS[who]
    n0 = len(_denials(d))
    tok = auth.CURRENT_CALLER.set(caller)
    try:
        if who == "manager" and method == "agent.archive":
            await d.methods[method](_params(method))
            assert d.hub.store.get_agent("a4").get("archived_at")
            return
        with pytest.raises(Blocked):
            await d.methods[method](_params(method))
    finally:
        auth.CURRENT_CALLER.reset(tok)
    _untouched(d)
    rows = _denials(d)[: len(_denials(d)) - n0]
    assert any((r.get("data") or {}).get("layer") == "service" for r in rows), rows


async def test_service_gate_lets_human_through(d):
    """Người (CLI có token, không phải hậu duệ run) và lời gọi nội bộ vẫn làm được cả 4 thao tác."""
    human = auth.Caller(via="socket", pid=1, token_ok=True)
    tok = auth.CURRENT_CALLER.set(human)
    try:
        r = await d.methods["agent.revert"]({"id": "a2", "to": 1})
        assert (r.get("tool_policy") or {}).get("allow_bypass")
        await d.methods["agent.archive"]({"id": "a4"})
        await d.methods["trash.restore"]({"id": "a3", "kind": "agent"})
    finally:
        auth.CURRENT_CALLER.reset(tok)
    await d.methods["agent.archive"]({"id": "a3"})  # nội bộ (không có người gọi RPC)
    res = await d.methods["trash.purge"]({"id": "a3", "kind": "agent", "confirm": "a3", "by": "user", "channel": "cli"})
    assert res["purged"]


# ============================================================================ daemon RPC (lớp 1) — mọi kênh không phải người
@pytest.mark.parametrize("who", sorted(CALLERS))
@pytest.mark.parametrize("method", ADMIN)
async def test_rpc_gate_refuses_every_non_human_channel(d, method, who):
    caller = CALLERS[who]
    if who == "manager" and method == "agent.archive":
        pytest.skip("điều phối viên được xoá mềm agent (MANAGER_ALLOWED) — test_manager_security")
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": _params(method)}, as_caller=caller)
    assert resp.get("error", {}).get("code") == BLOCKED, resp
    _untouched(d)
    assert any((r.get("data") or {}).get("method") == method for r in _denials(d))


async def test_forged_audit_and_token_param_do_not_help(d):
    """Agent tự khai `_audit.actor=user` và gửi `_auth` giả → vẫn bị từ chối (danh tính do daemon xác định)."""
    p = {**_params("agent.revert"), "_audit": {"actor": "user", "channel": "cli"}, "_auth": "x" * 43}
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "agent.revert", "params": p}, as_caller=AGENT)
    assert resp.get("error", {}).get("code") == BLOCKED
    _untouched(d)


# ============================================================================ CLI / MCP của hub / hub-self (bộ phân loại + tool list)
@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    root = tmp_path_factory.mktemp("v2n1")
    hub, ws, uh = root / "hub", root / "ws", root / "home"
    for x in (hub, ws, uh):
        x.mkdir()
    ps = PolicyStore(hub, user_home=uh)
    ps.ensure()
    time.sleep(1.1)
    return GuardContext(ws, ps.load(), run_since=time.time(), hub_socket=str(hub / "hub.sock"), git_probe=False,
                        hub_mcp_servers={"myhub"})


@pytest.mark.parametrize("cmd", [
    "cfharness agent revert a2 --to 1", "agent-hub agent revert a2 --to 1", "python -m cfharness agent revert a2 --to 1",
    "uv run cfharness trash purge a2", "cfharness --json agent archive a2", "CFHARNESS_HOME=/x cfharness trash restore a2",
    "cfharness trash   purge a2 --yes", "sh -c 'cfharness agent revert a2 --to 1'",
])
def test_cli_evasions_forbidden(ctx, cmd):
    assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


@pytest.mark.parametrize("tool", [
    "mcp__cfharness__hub_agent_revert", "mcp__cfharness__hub_agent_archive", "mcp__cfharness__hub_trash_restore",
    "mcp__cfharness__hub_trash_purge", "mcp__cfharness__agent_revert", "mcp__myhub__trash_purge", "mcp__myhub__trash_restore",
    "mcp__cfharness__hub_agent_pause", "mcp__cfharness__hub_task_archive",
])
def test_hub_mcp_admin_tools_forbidden(ctx, tool):
    """Tool MCP của hub (kể cả đăng ký dưới tên khác) ứng với RPC chỉ-người → forbidden (trước: revert = `write` ALLOW)."""
    v = classify(tool, {"id": "a2", "to": 1}, ctx)
    assert v.forbidden, (tool, v.verdict, v.rule)


def test_hub_self_exposes_no_admin_tool():
    """hub-self: vai trò thường không có tool nào tới 4 RPC; điều phối viên chỉ `agent_archive` (daemon kiểm MANAGER_ALLOWED)."""
    bad = {"agent_revert", "agent_archive", "trash_restore", "trash_purge", "agent_purge", "trash_list"}
    for role, tools in ROLE_TOOLS.items():
        exposed = bad & set(tools)
        assert exposed <= ({"agent_archive"} if role == "manager" else set()), (role, exposed)
    assert "agent.archive" in auth.MANAGER_ALLOWED and not ({"agent.revert", "trash.restore", "trash.purge"} & set(auth.MANAGER_ALLOWED))
    assert "agent_archive" in MANAGER_TOOLS

