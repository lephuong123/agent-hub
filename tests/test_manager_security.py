"""M7 MGR-CORE — rủi ro an toàn của điều phối viên: mỗi ca phải bị DAEMON từ chối + audit actor `manager:<agent>`.

Hai lớp được kiểm riêng:
- lớp 1 (tool hub-self → `managerself.call` → `ManagerOps`): tool không tồn tại / trường cấm → `manager.denied` + `manager.action{ok:false}`;
- lớp 2 (`Daemon._dispatch` với `Caller.manager`): gọi THẲNG method nhạy cảm như thể lớp 1 bị lách → `MANAGER_ALLOWED` + `check_rpc`
  → `auth.denied` (audit actor manager:<agent>). Test `test_layer2_*` vô hiệu lớp 1 để chứng minh lớp 2 tự đứng được.
Không gọi claude thật.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from agent_hub import auth
from agent_hub.errors import BLOCKED, HubError
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore
from m7_fake import close, make_daemon, manager_run, rpc, tool, wait_status


@pytest.fixture
async def env(hub_home):
    d = await make_daemon(hub_home)
    res = await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1", "tool_policy": {"disallowed_tools": ["Bash(rm:*)", "Bash(git push:*)"]}})
    yield d, res["run_id"]
    await close(d)


def _denials(d, kind: str | None = None) -> list[dict]:
    rows = [r for r in d.hub.audit.query(limit=500) if r["actor"] == "manager:boss" and r["kind"] in ("manager.denied", "auth.denied")]
    return [r for r in rows if kind is None or kind in json.dumps(r.get("data"), ensure_ascii=False)]


async def _deny_tool(d, rid, op, args, match=None):
    n0 = len(_denials(d))
    with pytest.raises(HubError) as ei:
        await tool(d, rid, op, args)
    assert ei.value.code == BLOCKED, ei.value.message
    if match:
        assert match in ei.value.message, ei.value.message
    assert len(_denials(d)) == n0 + 1, "phải có đúng 1 dòng audit từ chối (actor manager:boss)"
    acts = [e.payload for e in d.hub.store.query_events(types=["manager.action"])]
    assert acts and acts[-1]["ok"] is False and acts[-1]["tool"] == op
    return ei.value


async def _deny_rpc(d, rid, method, params, match=None):
    """Lớp 2: gọi method như người gọi manager (giả sử lớp 1 đã bị lách)."""
    n0 = len(_denials(d))
    with pytest.raises(HubError) as ei:
        await rpc(d, method, params, as_caller=auth.manager_caller("boss", rid))
    assert ei.value.code == BLOCKED and (ei.value.data or {}).get("reason") == "manager_forbidden", ei.value.message
    if match:
        assert match in ei.value.message, ei.value.message
    assert len(_denials(d)) == n0 + 1
    return ei.value


# ------------------------------------------------------------------ duyệt approval
async def test_manager_cannot_approve(env):
    d, rid = env
    await _deny_tool(d, rid, "approval_approve", {"id": "ap_1"}, match="không có trong bộ tool")
    for m in ("approval.approve", "approval.deny"):
        await _deny_rpc(d, rid, m, {"id": "ap_1"}, match="MANAGER_ALLOWED")


# ------------------------------------------------------------------ policy / protected / guard
async def test_manager_cannot_touch_policy_protected(env):
    d, rid = env
    await _deny_tool(d, rid, "guard_protect_add", {"selector": "/x"})
    for m, p in (("guard.protect.add", {"kind": "path", "selector": "/tmp"}), ("guard.protect.rm", {"id": "x"}),
                 ("guard.integrity.accept", {"path": "policy/protected.yaml", "content": ""}), ("config.set", {"key": "guard.enabled", "value": False}),
                 ("learn.policy_set", {"key": "enabled", "value": False}), ("learn.rollback", {"id": "le_x"})):
        await _deny_rpc(d, rid, m, p)
    # công tắc học của team = chính sách an toàn
    await _deny_tool(d, rid, "team_create", {"id": "t1", "policies": {"learning": {"enabled": False}}}, match="policies.learning")
    d.hub.teams.create({"id": "t2"})
    await _deny_tool(d, rid, "team_edit", {"id": "t2", "changes": {"policies": {"learning": {"auto_apply": False}}}}, match="policies.learning")


# ------------------------------------------------------------------ golden
async def test_manager_cannot_edit_golden(env):
    d, rid = env
    await _deny_tool(d, rid, "golden_add", {"agent": "w1"})
    for m in ("golden.add", "golden.rm", "golden.update", "golden.from_run"):
        await _deny_rpc(d, rid, m, {"agent": "w1", "id": "g1"})


# ------------------------------------------------------------------ sandbox
async def test_manager_cannot_set_sandbox_exception(env):
    d, rid = env
    await _deny_rpc(d, rid, "guard.sandbox.set", {"agent": "w1", "allow_localhost": True})
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"sandbox_allow_localhost": True}}, match="sandbox")
    await _deny_tool(d, rid, "agent_create", {"id": "w9", "spec": {"sandbox": {"allow_localhost": True}}}, match="sandbox")
    assert not d.hub.guard.sandbox_overrides("w1")


# ------------------------------------------------------------------ purge
async def test_manager_cannot_purge(env):
    d, rid = env
    await _deny_tool(d, rid, "agent_purge", {"id": "w1"})
    for m in ("trash.purge", "agent.purge", "agent.delete", "agent.revert", "trash.restore", "run.revert"):
        await _deny_rpc(d, rid, m, {"id": "w1", "confirm": "w1", "to": 1})
    # cổng tầng service vẫn chặn kể cả lời gọi nội bộ dưới danh tính manager
    from agent_hub.errors import Blocked
    from agent_hub.recovery import purge_agent

    d.hub.agents.remove("w1")
    tok = auth.CURRENT_CALLER.set(auth.manager_caller("boss", rid))
    try:
        with pytest.raises(Blocked):
            purge_agent(d.hub, "w1", by="manager:boss", channel="manager")
    finally:
        auth.CURRENT_CALLER.reset(tok)


# ------------------------------------------------------------------ ESTOP
async def test_manager_can_turn_estop_on_but_never_off(env):
    d, rid = env
    await _deny_tool(d, rid, "estop_off", {})
    await _deny_rpc(d, rid, "estop.set", {"on": False}, match="chỉ được BẬT")
    await _deny_rpc(d, rid, "team.estop", {"id": "t", "on": False})
    await tool(d, rid, "estop_on", {"reason": "khẩn"})
    assert d.hub.runs.estop["on"]
    await _deny_rpc(d, rid, "estop.set", {"on": False})
    assert d.hub.runs.estop["on"]


# ------------------------------------------------------------------ tự sửa mình / manager khác
async def test_manager_cannot_modify_itself_or_other_managers(env):
    d, rid = env
    for args in ({"id": "boss", "changes": {"model": "opus"}}, {"id": "boss", "soul": "I am free now"},
                 {"id": "boss", "changes": {"limits": {"max_budget_usd_per_run": 100}}}):
        await _deny_tool(d, rid, "agent_update", args, match="điều phối viên")
    await _deny_tool(d, rid, "agent_archive", {"id": "boss"}, match="điều phối viên")
    await _deny_tool(d, rid, "learn_propose", {"agent": "boss", "target": "config", "op": "patch", "payload": {"changes": {"model": "opus"}}})
    await _deny_tool(d, rid, "run_start", {"agent": "boss", "prompt": "x"})
    await _deny_tool(d, rid, "run_stop", {"id": rid})
    await _deny_tool(d, rid, "team_member_add", {"team": "t", "agent": "boss"})
    # manager trước đây (đã bị thay) cũng là "agent manager"
    d.hub.agents.create({"id": "old", "name": "old", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "old"})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    await _deny_tool(d, rid, "agent_update", {"id": "old", "changes": {"model": "haiku"}}, match="điều phối viên")
    await _deny_rpc(d, rid, "agent.update", {"id": "boss", "changes": {"model": "opus"}})
    assert d.hub.store.get_agent("boss")["model"] == "haiku"


# ------------------------------------------------------------------ cấp manager cho agent khác
async def test_manager_cannot_grant_manager_role(env):
    d, rid = env
    await _deny_tool(d, rid, "manager_set", {"agent_id": "w1"})
    await _deny_rpc(d, rid, "manager.set", {"agent_id": "w1"})
    await _deny_tool(d, rid, "agent_create", {"id": "boss2", "template": "manager"}, match="template 'manager'")
    assert d.hub.manager.agent_id() == "boss"
    # và một agent thường (không phải người) gọi manager.set → HUMAN_ONLY
    with pytest.raises(HubError):
        await rpc(d, "manager.set", {"agent_id": "w1"}, as_caller=auth.Caller(via="socket", run_id="r_w", agent_id="w1", evidence=["env_run_id"]))
    # hub-self vai trò manager không được gắn cho run của agent khác
    r = d.hub.runs.submit("w1", "[agent-hub · OWNER hi", run_opts={"hub_self": "manager"})
    await wait_status(d, r["id"])
    with pytest.raises(HubError):
        await tool(d, r["id"], "agent_create", {"id": "x"})


# ------------------------------------------------------------------ bypassPermissions / allow_bypass / extra_args
@pytest.mark.parametrize("changes, match", [
    ({"tool_policy": {"permission_mode": "bypassPermissions"}}, "bypassPermissions"),
    ({"tool_policy": {"allow_bypass": True}}, "allow_bypass"),
    ({"tool_policy": {"permission_prompts": "none"}}, "permission_prompts"),
    ({"extra_args": ["--dangerously-skip-permissions"]}, "extra_args"),
    ({"runtime": {"extra_args": ["--settings", "x.json"]}}, "extra_args"),
    ({"subagents": {"x": {"permissionMode": "bypassPermissions"}}}, "subagents"),
])
async def test_manager_cannot_escalate_permissions(env, changes, match):
    d, rid = env
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": changes}, match=match)
    await _deny_tool(d, rid, "agent_create", {"id": "w8", "spec": changes}, match=match)
    await _deny_tool(d, rid, "agent_create", {"id": "w7", "template": "coder", "spec": changes}, match=match)
    row = d.hub.store.get_agent("w1")
    assert (row["tool_policy"] or {}).get("permission_mode") != "bypassPermissions" and not (row["tool_policy"] or {}).get("allow_bypass")
    assert d.hub.store.get_agent("w8") is None and d.hub.store.get_agent("w7") is None


# ------------------------------------------------------------------ mẫu chặn do người đặt
async def test_manager_cannot_remove_human_disallowed_patterns(env):
    d, rid = env
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": ["Bash(rm:*)"]}}}, match="git push")
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": []}}}, match="mẫu chặn do người đặt")
    await _deny_tool(d, rid, "learn_propose", {"agent": "w1", "target": "config", "op": "patch",
                                                "payload": {"changes": {"tool_policy": {"disallowed_tools": []}}}}, match="mẫu chặn")
    assert d.hub.store.get_agent("w1")["tool_policy"]["disallowed_tools"] == ["Bash(rm:*)", "Bash(git push:*)"]
    # thêm được; bỏ đúng mẫu mình thêm được; người đặt lại sau → lại thành của người
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": ["Bash(rm:*)", "Bash(git push:*)", "WebFetch"]}}})
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": ["Bash(rm:*)", "Bash(git push:*)"]}}})
    await _deny_rpc(d, rid, "agent.update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": ["Bash(rm:*)"]}}}, match="git push")


# ------------------------------------------------------------------ MCP ngoài registry / MCP của hub
async def test_manager_cannot_assign_unregistered_or_hub_mcp(env):
    d, rid = env
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"mcp_servers": ["evil"]}}, match="registry")
    await _deny_tool(d, rid, "agent_create", {"id": "w6", "spec": {"mcp_servers": ["evil"]}}, match="registry")
    d.hub.mcp.add({"name": "hubctl", "transport": "stdio", "command": "agent-hub", "args": ["mcp", "serve"]})  # người đăng ký MCP của hub
    d.hub.mcp.add({"name": "docs", "transport": "stdio", "command": "docs-mcp", "trust": "trusted"})
    d.hub.mcp.add({"name": "raw", "transport": "stdio", "command": "raw-mcp"})  # untrusted (mặc định) → chỉ người gán (MGR-7d)
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"mcp_servers": ["raw"]}}, match="untrusted")
    await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"mcp_servers": ["hubctl"]}}, match="MCP của chính hub")
    names = {m["name"] for m in await tool(d, rid, "mcp_registry_list")}
    assert "hubctl" not in names and "docs" in names
    await _deny_rpc(d, rid, "mcp.add", {"name": "x", "config": {"command": "x"}})
    await _deny_rpc(d, rid, "mcp.assign", {"agent": "w1", "server": "x"})
    assert d.hub.store.get_agent("w1")["mcp_servers"] in ([], None)
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"mcp_servers": ["docs"]}})  # trong registry → được
    assert d.hub.store.get_agent("w1")["mcp_servers"] == ["docs"]


# ------------------------------------------------------------------ workspace vào vùng an toàn
async def test_manager_cannot_point_workspace_into_hub_home(env):
    d, rid = env
    for p in (str(d.hub.paths.home), str(d.hub.paths.home / "policy"), "/", str(__import__("pathlib").Path.home())):
        await _deny_tool(d, rid, "agent_update", {"id": "w1", "changes": {"workspace_default": p}})
    await _deny_tool(d, rid, "run_start", {"agent": "w1", "prompt": "x", "cwd": str(d.hub.paths.home)})
    await _deny_tool(d, rid, "task_create", {"title": "x", "body": "y", "assignee": "w1", "workspace": str(d.hub.paths.home / "policy")})


# ------------------------------------------------------------------ lớp 2 đứng được khi lớp 1 bị vô hiệu
async def test_layer2_daemon_blocks_even_if_layer1_disabled(env, monkeypatch):
    d, rid = env
    from agent_hub.manager import ops as ops_mod

    async def no_layer1(self, op, method, params, args, summary):  # lớp 1 "hỏng": gọi thẳng daemon
        resp = await self.svc.daemon.call_as(self.caller, method, dict(params))
        if "error" in resp:
            raise HubError(resp["error"]["message"], data=resp["error"].get("data"), code=resp["error"].get("code"))
        return resp["result"]

    monkeypatch.setattr(ops_mod.ManagerOps, "_rpc", no_layer1)
    n0 = len(_denials(d))
    for op, args in (("agent_create", {"id": "w5", "spec": {"tool_policy": {"permission_mode": "bypassPermissions"}}}),
                     ("agent_archive", {"id": "boss"}), ("run_stop", {"id": rid}), ("estop_on", {"reason": "x"})):
        try:
            await tool(d, rid, op, args)
        except HubError as e:
            assert e.code == BLOCKED
    # estop_on hợp lệ vẫn chạy; 3 ca còn lại bị lớp 2 chặn
    assert len(_denials(d)) - n0 == 3
    assert d.hub.store.get_agent("w5") is None and d.hub.store.get_agent("boss")["archived_at"] is None


# ------------------------------------------------------------------ ràng run ↔ người gọi qua socket
async def test_managerself_call_bound_to_manager_run_process(env):
    d, rid = env
    # tiến trình cục bộ không thuộc run (kể cả người có token) không mượn được run_id của manager
    for caller in (auth.Caller(via="socket", pid=1234, token_ok=True),
                   auth.Caller(via="socket", pid=99, run_id="r_other", agent_id="w1", evidence=["env_run_id"])):
        with pytest.raises(HubError, match="chỉ dành cho hub-self"):
            await rpc(d, "managerself.call", {"run_id": rid, "op": "agent_create", "args": {"id": "zz"}}, as_caller=caller)
    assert d.hub.store.get_agent("zz") is None
    assert d.hub.audit.query(kind="manager.denied")


async def test_managerself_over_real_socket_from_foreign_process(env):
    """Tiến trình KHÔNG thuộc run (qua socket thật) gọi managerself.call với run_id của manager → từ chối."""
    d, rid = env
    srv = await asyncio.start_unix_server(d._handle_conn, path=str(d.paths.socket))
    try:
        code = ("import json,socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
                "s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':'managerself.call','params':{'run_id':sys.argv[2],'op':'agent_create','args':{'id':'zz'}}})+'\\n').encode());"
                "print(s.makefile().readline())")
        cp = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(d.paths.socket), rid], capture_output=True, text=True, timeout=30)
        assert json.loads(cp.stdout).get("error", {}).get("code") == BLOCKED, cp.stdout
        assert d.hub.store.get_agent("zz") is None
    finally:
        srv.close()


# ------------------------------------------------------------------ classifier: lệnh CLI manager từ Bash của agent
def test_classifier_manager_cli_forbidden(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("agent-hub manager set w1", "agent-hub manager chat 'approve everything'", "agent-hub manager tick"):
        v = classify("Bash", {"command": cmd}, ctx)
        assert v.forbidden, (cmd, v)
    assert not classify("Bash", {"command": "agent-hub manager get"}, ctx).forbidden


# ------------------------------------------------------------------ trong quyền vẫn làm được (không chặn nhầm)
async def test_allowed_changes_still_work(env):
    d, rid = env
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"model": "opus", "effort": "high", "limits": {"max_budget_usd_per_run": 3},
                                                               "tool_policy": {"tools": ["Read", "Bash"], "allowed_tools": ["Bash(ls:*)"], "permission_mode": "acceptEdits"},
                                                               "role": "coder", "name": "W1"}, "soul": "# W1\nBe careful."})
    row = d.hub.store.get_agent("w1")
    assert row["model"] == "opus" and row["tool_policy"]["permission_mode"] == "acceptEdits" and row["tool_policy"]["disallowed_tools"] == ["Bash(rm:*)", "Bash(git push:*)"]
    await tool(d, rid, "agent_archive", {"id": "w1"})
    await tool(d, rid, "agent_restore", {"id": "w1"})
    assert d.hub.store.get_agent("w1")["archived_at"] is None
