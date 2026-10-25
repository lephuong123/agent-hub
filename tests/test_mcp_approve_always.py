"""MCP `hub_approve(always=true, confirm_rule=…)` — "luôn cho phép" từ client MCP của người (LLM-2, hardening-SEC.md).

In-process: Daemon thật (FakeRuntime giữ run) + MCP server SDK in-process. Caller của MCP đi qua `Daemon.call_as` với danh tính
dựng sẵn: người (token đúng, không bằng chứng agent) hoặc agent (bằng chứng cây tiến trình) — đúng cổng HUMAN_ONLY của daemon.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from mcp import Client

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.mcp_server.server import build_server
from cfharness.rpc import RpcError
from cfharness.runtime import FakeRuntime

UNCERTAIN = "eval \"$CMD\""  # uncertain (eval_dynamic), đủ điều kiện "luôn cho phép"
DESTRUCTIVE = "rm -rf /etc"

HUMAN = auth.Caller(via="socket", token_ok=True)


@pytest.fixture
async def d(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"enabled": True, "git_probe": False}, "approval": {"timeout_s": 30}}))
    rt = FakeRuntime(hold=True)
    dm = Daemon(hub_home, runtime=rt)
    dm.hub.start()
    yield dm
    rt.release()
    await asyncio.wait_for(dm.hub.stop(), 15)


def mcp_for(dm: Daemon, who: auth.Caller):
    loop = asyncio.get_running_loop()

    def call(method, params):  # chạy trong thread của anyio (như DaemonCaller qua socket)
        resp = asyncio.run_coroutine_threadsafe(dm.call_as(who, method, params), loop).result(30)
        if "error" in resp:
            e = resp["error"]
            raise RpcError(e.get("code", -1), e.get("message", ""), e.get("data"))
        return resp["result"]

    return build_server(call)


async def _held(dm: Daemon, cmd: str, agent: str = "w"):
    hub = dm.hub
    if hub.store.get_agent(agent) is None:
        hub.agents.create({"id": agent, "name": agent.upper()})
    rid = hub.runs.submit(agent, "x")["id"]
    for _ in range(500):
        cur = hub.store.get_run(rid)
        if cur and cur["status"] == "running":
            break
        await asyncio.sleep(0.01)
    task = asyncio.ensure_future(hub.guard.check({"run_id": rid, "tool_name": "Bash", "tool_input": {"command": cmd}, "channel": "hook"}))
    for _ in range(500):
        reqs = [r for r in hub.approvals.list("pending", run_id=rid)]
        if reqs:
            return rid, task, reqs[0]
        await asyncio.sleep(0.01)
    raise AssertionError("không mở approval")


def _pending(dm: Daemon, aid: str) -> bool:
    return dm.hub.approvals.get(aid)["status"] in ("open", "waiting_human")


async def _finish(dm: Daemon, aid: str, task) -> None:
    if _pending(dm, aid):
        dm.hub.approvals.resolve(aid, "denied")
    await asyncio.wait_for(task, 20)


async def test_human_mcp_always_creates_exact_rule(d):
    rid, task, ap = await _held(d, UNCERTAIN)
    async with Client(mcp_for(d, HUMAN)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        props = tools["hub_approve"].input_schema["properties"]
        assert "always" in props and "confirm_rule" in props and "LÂU DÀI" in tools["hub_approve"].description
        reqs = (await c.call_tool("hub_open_requests", {})).structured_content["requests"]
        offer = next(r for r in reqs if r["id"] == ap["id"])["always_offer"]
        assert offer["kind"] == "exact" and offer["fingerprint"] == ap["action_fingerprint"] and offer["summary"]
        res = await c.call_tool("hub_approve", {"request_id": ap["id"], "always": True, "confirm_rule": offer["fingerprint"]})
        assert not res.is_error, res.content
        rule = res.structured_content["always_rule"]
    assert rule["kind"] == "exact" and rule["agent_id"] == "w" and rule["tool"] == "Bash" and rule["fingerprint"] == ap["action_fingerprint"]
    assert rule["channel"].startswith("mcp:")
    assert (await asyncio.wait_for(task, 20))["decision"] == "allow"
    pol = d.hub.guard.policy()["always_allow"]
    assert [r["id"] for r in pol] == [rule["id"]]
    # lần sau: cùng lệnh, cùng agent → cho phép không hỏi
    assert (await asyncio.wait_for(d.hub.guard.check({"run_id": rid, "tool_name": "Bash", "tool_input": {"command": UNCERTAIN},
                                                         "channel": "hook"}), 20))["decision"] == "allow"


async def test_mcp_always_refused_for_destructive_and_request_stays_pending(d):
    _, task, ap = await _held(d, DESTRUCTIVE)
    async with Client(mcp_for(d, HUMAN)) as c:
        reqs = (await c.call_tool("hub_open_requests", {})).structured_content["requests"]
        assert next(r for r in reqs if r["id"] == ap["id"])["always_offer"] is None
        res = await c.call_tool("hub_approve", {"request_id": ap["id"], "always": True, "confirm_rule": ap["action_fingerprint"]})
        assert res.is_error and "luôn cho phép" in res.content[0].text
    assert _pending(d, ap["id"]) and d.hub.guard.policy()["always_allow"] == []
    await _finish(d, ap["id"], task)


@pytest.mark.parametrize("confirm", [None, "", "sai-fingerprint"])
async def test_mcp_always_needs_matching_confirm_echo(d, confirm):
    _, task, ap = await _held(d, UNCERTAIN)
    args = {"request_id": ap["id"], "always": True}
    if confirm is not None:
        args["confirm_rule"] = confirm
    async with Client(mcp_for(d, HUMAN)) as c:
        res = await c.call_tool("hub_approve", args)
        assert res.is_error
        assert ap["action_fingerprint"] not in res.content[0].text  # lỗi không lộ giá trị đúng (thử lại mù không tự khớp)
        # confirm_rule không đi kèm always → lỗi, không duyệt một lần ngầm
        res = await c.call_tool("hub_approve", {"request_id": ap["id"], "confirm_rule": ap["action_fingerprint"]})
        assert res.is_error
    assert _pending(d, ap["id"]) and d.hub.guard.policy()["always_allow"] == []
    await _finish(d, ap["id"], task)


async def test_mcp_always_from_agent_caller_blocked(d):
    rid, task, ap = await _held(d, UNCERTAIN)
    agent = auth.Caller(via="socket", token_ok=True, run_id=rid, agent_id="w", evidence=[f"ppid:{rid}"])
    async with Client(mcp_for(d, agent)) as c:
        res = await c.call_tool("hub_approve", {"request_id": ap["id"], "always": True, "confirm_rule": ap["action_fingerprint"]})
        assert res.is_error and "NGƯỜI" in res.content[0].text and "run agent" in res.content[0].text
    assert _pending(d, ap["id"]) and d.hub.guard.policy()["always_allow"] == []
    await _finish(d, ap["id"], task)


async def test_coordinator_owner_permission_never_carries_always(d):
    """ask_owner_permission(approve_action): action chốt do hub dựng chỉ có id — `always` của điều phối viên bị bỏ."""
    from cfharness.manager import owner_actions

    _, task, ap = await _held(d, UNCERTAIN)
    action, _desc = owner_actions.build(d.hub, "approve_action", {"approval_id": ap["id"], "always": True, "always_confirm": ap["action_fingerprint"]})
    assert action["method"] == "approval.approve" and action["params"] == {"id": ap["id"]}
    await _finish(d, ap["id"], task)
