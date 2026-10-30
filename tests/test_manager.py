"""M7 MGR-CORE — điều phối viên: chỉ định, hội thoại liên tục (`--resume`), tool hub-self manager, ledger/rollback, nhịp heartbeat.
Daemon in-process + ScriptedRuntime — không gọi claude."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import HubError
from cfharness.events import EVENT_LEVELS
from cfharness.mcp_server import hub_self
from m7_fake import close, make_daemon, manager_run, rpc, tool, wait_status


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


def _events(d, type_):
    return [e for e in d.hub.store.query_events(types=[type_])]


# ------------------------------------------------------------------ chỉ định + hội thoại
async def test_set_get_and_chat_creates_conversation_with_manager_hub_self(d):
    assert (await rpc(d, "manager.get"))["agent_id"] is None
    with pytest.raises(HubError, match="chưa có điều phối viên"):
        await rpc(d, "manager.chat", {"channel": "web", "text": "hi"})
    res = await manager_run(d, text="Xin chào")
    g = await rpc(d, "manager.get")
    assert g["agent_id"] == "boss" and g["conversations"][0]["id"] == res["conversation_id"]
    run = d.hub.store.get_run(res["run_id"])
    assert run["kind"] == "manager" and run["run_opts"]["hub_self"] == "manager"
    assert run["run_opts"]["manager_conversation"] == res["conversation_id"]
    # mcp.json của run có hub-self vai trò manager (dù run không gắn task)
    spec = d.script.specs[-1]
    mcp = json.loads(Path(spec.mcp_config_path).read_text()) if Path(spec.mcp_config_path).exists() else None
    if mcp is not None:
        args = mcp["mcpServers"]["hub-self"]["args"]
        assert args[args.index("--role") + 1] == "manager"
    assert "mcp__hub-self" in spec.tool_policy["allowed_tools"]
    assert Path(spec.cwd).name == f"conv-{res['conversation_id']}" and spec.workspace_kind == "scratch"
    assert _events(d, "manager.designated")


async def test_reply_event_history_and_resume_same_session(d):
    d.script.hold_manager = False
    d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    r1 = await rpc(d, "manager.chat", {"channel": "telegram:42", "text": "tin 1"})
    await wait_status(d, r1["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    reps = [e for e in _events(d, "manager.reply") if e.payload["run_id"] == r1["run_id"]]
    assert reps and reps[0].payload["conversation_id"] == r1["conversation_id"] and reps[0].payload["text"] == "Đã giao việc."
    assert reps[0].payload["channel"] == "telegram:42" and reps[0].payload["kind"] == "reply"
    r2 = await rpc(d, "manager.chat", {"channel": "telegram:42", "text": "tin 2", "conversation_id": r1["conversation_id"]})
    run2 = d.hub.store.get_run(r2["run_id"])
    assert run2["resumed_from_session"] == d.hub.store.get_run(r1["run_id"])["session_id"]  # --resume cùng session
    assert run2["cwd"] == d.hub.store.get_run(r1["run_id"])["cwd"]  # resume cần cùng cwd
    await wait_status(d, r2["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    hist = await rpc(d, "manager.history", {"conversation_id": r1["conversation_id"]})
    assert [m["role"] for m in hist] == ["user", "manager", "user", "manager"]
    # reuse_latest: không có conversation_id → hội thoại gần nhất của đúng kênh
    r3 = await rpc(d, "manager.chat", {"channel": "telegram:42", "text": "tin 3", "reuse_latest": True})
    assert r3["conversation_id"] == r1["conversation_id"]
    # không reuse → hội thoại mới (hợp đồng với MGR-UI)
    r4 = await rpc(d, "manager.chat", {"channel": "web", "text": "khác"})
    assert r4["conversation_id"] != r1["conversation_id"]
    convs = await rpc(d, "manager.conversations", {"channel": "telegram:42"})
    assert len(convs) == 1 and convs[0]["messages"] >= 4


async def test_rotate_session_after_turns_carries_history(d):
    d.script.hold_manager = False
    d.hub.config.set("manager.rotate_after_turns", 1)
    d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    r1 = await rpc(d, "manager.chat", {"channel": "cli", "text": "nhớ số 7"})
    await wait_status(d, r1["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    r2 = await rpc(d, "manager.chat", {"channel": "cli", "text": "số mấy?", "conversation_id": r1["conversation_id"]})
    run2 = d.hub.store.get_run(r2["run_id"])
    assert run2["resumed_from_session"] is None and "nhớ số 7" in run2["prompt"] and "fresh session" in run2["prompt"]


async def test_chat_while_running_steers_into_run(d):
    res = await manager_run(d)
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "thêm ý", "conversation_id": res["conversation_id"]})
    assert r2["steered"] and r2["run_id"] == res["run_id"]


# ------------------------------------------------------------------ tool: đọc + ghi trong quyền
async def test_manager_creates_agent_assigns_task_and_audits(d):
    res = await manager_run(d)
    rid = res["run_id"]
    ov = await tool(d, rid, "hub_overview")
    assert ov["you"] == "boss" and any(a["id"] == "boss" and a["manager"] for a in ov["agents"])
    tpls = await tool(d, rid, "templates")
    assert "researcher" in {t["name"] for t in tpls} and "manager" not in {t["name"] for t in tpls}
    a = await tool(d, rid, "agent_create", {"id": "rs", "template": "researcher", "spec": {"model": "haiku", "description": "Summarizes READMEs"}})
    assert a["id"] == "rs" and a["model"] == "haiku"
    t = await tool(d, rid, "task_create", {"title": "Tóm tắt README", "body": "Summarize README.md", "assignee": "rs"})
    task = d.hub.tasks.row(t["task_id"])
    assert task["created_by"] == "manager:boss" and task["assignee_agent_id"] == "rs"
    got = await tool(d, rid, "task_get", {"id": t["task_id"]})
    assert got["id"] == t["task_id"]
    acts = [e.payload for e in _events(d, "manager.action")]
    assert [x["tool"] for x in acts] == ["agent_create", "task_create"] and all(x["ok"] and x["audit_id"] for x in acts)
    assert acts[0]["target"] == "agent:rs" and acts[1]["target"] == f"task:{t['task_id']}"
    rows = d.hub.audit.query(kind="agent.create")
    assert rows and rows[0]["actor"] == "manager:boss" and rows[0]["channel"] == "manager"
    assert d.hub.audit.query(kind="task.create")[0]["actor"] == "manager:boss"


async def test_agent_update_goes_through_ledger_and_is_auto_rollbackable(d):
    res = await manager_run(d)
    rid = res["run_id"]
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "sonnet", "tool_policy": {"disallowed_tools": ["Bash(rm:*)"]}})
    out = await tool(d, rid, "agent_update", {"id": "w1", "changes": {"model": "haiku", "description": "fast worker",
                                                                       "tool_policy": {"disallowed_tools": ["Bash(rm:*)", "Bash(curl:*)"]}}})
    e = d.hub.learning.ledger.get(out["ledger_entry_id"])
    assert e["actor"] == "manager:boss" and e["target"] == "config"
    row = d.hub.store.get_agent("w1")
    assert row["model"] == "haiku" and row["description"] == "fast worker"
    assert d.hub.store.list_revisions("w1")[-1]["actor"] == "manager:boss"
    # mẫu do chính manager thêm → manager được bỏ lại
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"disallowed_tools": ["Bash(rm:*)"]}}})
    # regression monitor (bên gọi TỰ ĐỘNG) được rollback thay đổi của manager (= thay đổi tự động, không phải của người)
    from cfharness.learning.ledger import is_auto_change_actor
    from cfharness.learning_ops.ports import is_auto_change

    assert is_auto_change_actor("manager:boss") and is_auto_change({"actor": "manager:boss"})
    last = d.hub.learning.ledger.list("w1", target="config")[0]
    d.hub.learning.ledger.rollback(last["id"], actor="regression_monitor", reason="hồi quy")
    assert (d.hub.store.get_agent("w1")["tool_policy"]["disallowed_tools"]) == ["Bash(rm:*)", "Bash(curl:*)"]
    d.hub.learning.ledger.rollback(e["id"], actor="regression_monitor", reason="hồi quy")
    assert d.hub.store.get_agent("w1")["model"] == "sonnet" and d.hub.store.get_agent("w1")["description"] == ""


async def test_team_task_run_ops_and_notify_owner(d):
    res = await manager_run(d)
    rid = res["run_id"]
    for x in ("w1", "w2"):
        d.hub.agents.create({"id": x, "name": x, "description": "writes docs"})
    tm = await tool(d, rid, "team_create", {"id": "docs", "name": "Docs", "members": [{"agent_id": "w1", "role": "worker"}]})
    assert tm["id"] == "docs"
    await tool(d, rid, "team_member_add", {"team": "docs", "agent": "w2"})
    await tool(d, rid, "team_edit", {"id": "docs", "changes": {"charter": "Be brief."}})
    t = await tool(d, rid, "task_create", {"title": "Doc", "body": "write", "team": "docs", "decompose": False})
    await tool(d, rid, "task_comment", {"id": t["task_id"], "text": "ưu tiên cao"})
    r = await tool(d, rid, "run_start", {"agent": "w2", "prompt": "ping"})
    assert r["run_id"]
    n = await tool(d, rid, "notify_owner", {"text": "Xong việc A", "kind": "done"})
    assert n["conversation_id"] == res["conversation_id"]
    rep = [e.payload for e in _events(d, "manager.reply") if e.payload.get("kind") == "notify"]
    assert rep and rep[0]["text"] == "Xong việc A" and rep[0]["channel"] == "web"
    hist = await rpc(d, "manager.history", {"conversation_id": res["conversation_id"]})
    assert hist[-1]["role"] == "notify"
    on = await tool(d, rid, "estop_on", {"reason": "agent w2 phá hoại"})
    assert d.hub.runs.estop["on"] and "manager:boss" in d.hub.runs.estop["reason"]


def test_hub_self_manager_role_tools_and_event_types():
    names = set(hub_self.ROLE_TOOLS["manager"])
    for n in ("hub_overview", "agent_create", "agent_update", "task_create", "run_start", "estop_on", "notify_owner", "learn_propose", "memory_search"):
        assert n in names
    for bad in ("approval_approve", "estop_off", "manager_set", "golden_add", "sandbox_set", "agent_purge", "policy_set"):
        assert bad not in names
    for t in ("manager.action", "manager.reply", "manager.tick", "manager.designated"):
        assert t in EVENT_LEVELS
    assert "managerself.call" in auth.AGENT_ALLOWED_MUTATING and {"manager.set", "manager.chat", "manager.tick"} <= auth.HUMAN_ONLY


def test_hub_self_stdio_manager_tools_list(hub_home):
    import subprocess
    import sys

    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    cp = subprocess.run([sys.executable, "-m", "cfharness.mcp_server.hub_self", "--run", "r_x", "--socket", "/nonexistent", "--role", "manager"],
                        input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=30)
    out = [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]
    tools = {t["name"]: t for t in out[1]["result"]["tools"]}
    assert "agent_create" in tools and "task_comment" in tools and "id" in tools["task_comment"]["inputSchema"]["properties"]


# ------------------------------------------------------------------ nhịp
async def test_heartbeat_event_debounce_lock_estop_and_nothing_to_do(d):
    d.hub.config.set("manager.event_debounce_s", 0)
    d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    d.hub.agents.create({"id": "w1", "name": "w1"})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    m = d.hub.manager
    # không có việc gì → không tốn run
    out = await m.maybe_tick()
    assert out["run_id"] is None and out["skipped"] in ("nothing_to_do", "not_due")
    # task bị chặn → sự kiện → tick
    t = d.hub.tasks.create({"assignee": "w1", "title": "x", "body": "y"}, created_by="human")
    d.hub.tasks.set_status(t["id"], "blocked", reason="cần quyết định")
    assert m.pending and m.pending[0]["kind"] == "task_blocked"
    tk = await m.maybe_tick()
    assert tk["run_id"] and tk["reason"] == "events"
    run = await wait_status(d, tk["run_id"])
    assert "task_blocked" in run["prompt"] and run["run_opts"]["manager_kind"] == "tick"
    # khoá chống chồng: đang có run manager → không tick thêm, sự kiện giữ lại
    d.hub.tasks.create({"assignee": "w1", "title": "x2", "body": "y"}, created_by="manager:boss")
    t2 = d.hub.tasks.create({"assignee": "w1", "title": "x3", "body": "y"}, created_by="human")
    d.hub.tasks.set_status(t2["id"], "blocked", reason="lỗi")
    busy = await m.maybe_tick(force=True)
    assert busy["skipped"] == "manager_busy" and m.pending
    d.hub.runs.runtime().release()
    await wait_status(d, tk["run_id"], ("completed",))
    # ESTOP → không tick
    await d.hub.runs.set_estop(True, reason="t")
    assert (await m.maybe_tick())["skipped"] == "estop"
    await d.hub.runs.set_estop(False)
    tk2 = await m.maybe_tick()
    assert tk2["run_id"]
    assert (await rpc(d, "manager.get"))["tick"]["last_tick_run_id"] == tk2["run_id"]
    ticks = [e.payload for e in _events(d, "manager.tick")]
    assert any(x["status"] == "started" for x in ticks)


async def test_heartbeat_approval_waiting_and_periodic_open_work(d):
    d.hub.config.set("manager.approval_wait_min", 0)
    d.hub.config.set("manager.event_debounce_s", 3600)
    d.hub.config.set("manager.tick_interval_s", 0)
    d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    d.hub.agents.create({"id": "w1", "name": "w1"})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    d.hub.store.conn.execute(
        "INSERT INTO approval_requests (id, run_id, agent_id, tool_name, tool_input, action_fingerprint, status, opened_at) VALUES (?,?,?,?,?,?,?,?)",
        ("ap_1", "r_zz", "w1", "Bash", json.dumps({"command": "rm -rf build"}), "fp", "waiting_human", "2020-01-01T00:00:00Z"))
    d.hub.manager._scan_approvals()
    assert any(p["kind"] == "approval_waiting" for p in d.hub.manager.pending)
    d.hub.manager._scan_approvals()  # báo một lần
    assert sum(p["kind"] == "approval_waiting" for p in d.hub.manager.pending) == 1
    # tick định kỳ (tới hạn) chạy dù sự kiện chưa qua debounce; không có gì → bỏ qua, có task manager giao chưa xong → chạy
    d.hub.manager.pending.clear()
    d.hub.manager._pending_keys.clear()
    d.hub.store.conn.execute("UPDATE approval_requests SET status='allowed'")
    assert (await d.hub.manager.maybe_tick())["skipped"] == "nothing_to_do"
    d.hub.tasks.create({"assignee": "w1", "title": "theo dõi", "body": "y"}, created_by="manager:boss")
    tk = await d.hub.manager.maybe_tick()
    assert tk["run_id"] and tk["reason"] == "periodic"
    assert "not finished yet: 1" in d.hub.store.get_run(tk["run_id"])["prompt"]


async def test_worker_run_cannot_use_manager_tools(d):
    d.hub.agents.create({"id": "w1", "name": "w1"})
    d.hub.config.set("learning.agent_memory_tool", False)  # tool memory gắn hub-self vai trò knowledge cho mọi run — ở đây kiểm vai trò manager
    d.script.hold_manager = True
    res = await manager_run(d)
    r = d.hub.runs.submit("w1", "[cfharness · OWNER x", run_opts={"hub_self": "manager"})
    await wait_status(d, r["id"])
    with pytest.raises(HubError, match="không còn là điều phối viên|không phải run điều phối viên"):
        await tool(d, r["id"], "hub_overview")
    # hub-self vai trò manager bị gỡ khỏi run của agent không phải manager
    spec = [s for s in d.script.specs if s.run_id == r["id"]][0]
    assert "mcp__hub-self" not in (spec.tool_policy.get("allowed_tools") or [])
    assert res["run_id"]


def test_cli_parser_manager():
    from cfharness.cli import build_parser

    p = build_parser()
    a = p.parse_args(["manager", "chat", "tạo", "agent", "--new"])
    assert a.mgr_action == "chat" and a.text == ["tạo", "agent"] and a.new
    assert p.parse_args(["manager", "set", "dieu-phoi"]).agent == "dieu-phoi"
    assert p.parse_args(["manager", "tick"]).mgr_action == "tick"
    assert p.parse_args(["manager", "conversations", "--channel", "web"]).channel == "web"


async def test_done_events_seen_by_chat_run_do_not_cost_a_tick(d):
    """Task xong TRONG lúc manager đang trả lời → manager đã thấy → không tick chỉ để NO_ACTION (nghiệm thu thật lần 1)."""
    res = await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    t = d.hub.tasks.create({"assignee": "w1", "title": "x", "body": "y"}, created_by="manager:boss")
    d.hub.tasks.set_status(t["id"], "done", reason="ok")
    t2 = d.hub.tasks.create({"assignee": "w1", "title": "z", "body": "y"}, created_by="human")
    d.hub.tasks.set_status(t2["id"], "blocked", reason="cần người")
    assert {p["kind"] for p in d.hub.manager.pending} == {"task_done", "task_blocked"}
    d.hub.runs.runtime().release()
    await wait_status(d, res["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    assert [p["kind"] for p in d.hub.manager.pending] == ["task_blocked"]
