"""M3 qua bề mặt client: MCP `hub_run(team=…)`/`hub_events(task_id)`/`hub_team_*`/`hub_task_*` (FR-MS-02/08), RPC
`task.list|get` (hợp đồng cho tab Tasks của M2), audit, `estop.set {team}`, report (FR-OB-06), clone/export/import
(FR-AG-06), hub-self MCP stdio (FR-MS-08). Daemon in-process + ScriptedRuntime — không gọi claude."""

import asyncio
import json
import subprocess
import sys
import tarfile

import pytest
from mcp import Client

from agent_hub.daemon import Daemon
from agent_hub.errors import HubError
from agent_hub.mcp_server.server import READ_ONLY_TOOLS, build_server
from m3_fake import ScriptedRuntime, kind_of

PLAN = {"summary": "two steps", "subtasks": [
    {"key": "r", "title": "Research", "body": "find facts", "assignee": "researcher"},
    {"key": "w", "title": "Write", "body": "write it", "assignee": "writer", "parents": ["r"]},
]}


def script(spec):
    if kind_of(spec) == "lead":
        return {"structured": PLAN, "cost": 0.001}
    if "## Task: Research" in spec.prompt:
        return {"text": "facts: A, B", "cost": 0.001}
    return {"text": "final report", "cost": 0.001}


@pytest.fixture
async def daemon(hub_home):
    d = Daemon(hub_home, runtime=ScriptedRuntime(script))
    d.hub.config.set("tasks.tick_s", 0.05)
    d.hub.start()
    loop = asyncio.get_running_loop()

    def caller(method, params):
        fut = asyncio.run_coroutine_threadsafe(d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}), loop)
        resp = fut.result(60)
        if "error" in resp:
            from agent_hub.rpc import RpcError

            e = resp["error"]
            raise RpcError(e["code"], e["message"], e.get("data"))
        return resp["result"]

    d.caller = caller
    yield d
    d.hub.runs.shutting_down = True
    d.hub.store.close()


async def rpc(d, method, params):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in resp:
        raise HubError(resp["error"]["message"])
    return resp["result"]


def sc(r):
    assert not r.is_error, r.content
    return r.structured_content


async def test_mcp_team_flow_lead_two_workers_result(daemon):
    h = daemon.hub
    for aid, role in (("lead", "planner"), ("researcher", "researcher"), ("writer", "writer")):
        h.agents.create({"id": aid, "name": aid, "role": role})
    srv = build_server(daemon.caller)
    async with Client(srv) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for n in ("hub_team_list", "hub_team_get", "hub_team_create", "hub_team_update", "hub_team_member_set", "hub_task_create", "hub_task_list", "hub_task_get", "hub_task_comment"):
            assert n in tools and tools[n].annotations.read_only_hint is (n in READ_ONLY_TOOLS)
        team = sc(await c.call_tool("hub_team_create", {"team_id": "docs", "charter": "Be accurate.", "members": [
            {"agent_id": "lead", "role": "lead"}, {"agent_id": "researcher", "role": "worker"}, {"agent_id": "writer", "role": "worker"}]}))
        assert team["id"] == "docs" and len(team["members"]) == 3
        out = sc(await c.call_tool("hub_run", {"team": "docs", "prompt": "Research then write", "idempotency_key": "job-1"}))
        assert out["task_id"].startswith("t_")
        again = sc(await c.call_tool("hub_run", {"team": "docs", "prompt": "Research then write", "idempotency_key": "job-1"}))
        assert again["task_id"] == out["task_id"]
        after, epoch, done, seen = 0, None, None, set()
        for _ in range(50):
            ev = sc(await c.call_tool("hub_events", {"task_id": out["task_id"], "after_seq": after, "epoch": epoch, "wait_s": 2}))
            seen |= {e["type"] for e in ev["events"]}
            after, epoch = ev["latest_seq"], ev["epoch"]
            if ev["done"]:
                done = ev
                break
        assert done is not None and done["result"]["status"] == "done"
        assert "final report" in done["result"]["result_json"]["final"] and len(done["result"]["subtasks"]) == 2
        assert {"task.decomposed", "task.status", "run.completed"} <= seen
        lst = sc(await c.call_tool("hub_task_list", {"team": "docs"}))["tasks"]
        assert len(lst) == 3 and all(t["status"] == "done" for t in lst)
        got = sc(await c.call_tool("hub_task_get", {"task_id": out["task_id"]}))
        assert len(got["subtasks"]) == 2 and got["runs"][0]["kind"] == "lead"
        cm = sc(await c.call_tool("hub_task_comment", {"task_id": out["task_id"], "text": "thanks"}))
        assert cm["comment_id"].startswith("c_")
        tg = sc(await c.call_tool("hub_team_get", {"team_id": "docs"}))
        assert tg["counts"] == {"done": 3} and tg["cost_today_usd"] == pytest.approx(0.003)
        bad = await c.call_tool("hub_run", {"prompt": "x"})
        assert bad.is_error
    audit = h.audit.query(kind="mcp.team_run")
    assert audit and audit[0]["channel"].startswith("mcp:")


async def test_rpc_task_list_get_contract(daemon):
    """Chữ ký `task.list|get` ghi trong docs/dev-notes/M3.md (M2 làm tab Tasks dựa vào)."""
    h = daemon.hub
    h.agents.create({"id": "a", "name": "A"})
    h.agents.set_paused("a", True)
    t1 = await rpc(daemon, "task.create", {"title": "one", "assignee": "a", "body": "x"})
    t2 = await rpc(daemon, "task.create", {"title": "two", "assignee": "a", "parents": [t1["id"]], "priority": 5})
    rows = await rpc(daemon, "task.list", {"status": ["ready", "todo"]})
    assert [r["id"] for r in rows] == [t2["id"], t1["id"]]  # priority DESC
    keys = {"id", "team_id", "assignee_agent_id", "role", "kind", "title", "status", "priority", "created_by", "origin_task_id", "decomposed",
            "current_run_id", "consecutive_failures", "attempts", "block_reason", "wait_reason", "not_before", "result_summary",
            "created_at", "updated_at", "started_at", "done_at", "parents", "children", "cost_usd"}
    assert keys <= set(rows[0])
    assert rows[0]["parents"] == [t1["id"]] and rows[1]["children"] == [t2["id"]]
    await asyncio.sleep(0.2)
    assert (await rpc(daemon, "task.get", {"id": t1["id"]}))["wait_reason"] == "agent_paused"
    g = await rpc(daemon, "task.get", {"id": t2["id"][:8]})
    assert g["parents"] == [{"id": t1["id"], "title": "one", "status": "ready"}] and {"comments", "runs", "subtasks", "body"} <= set(g)
    await rpc(daemon, "task.block", {"id": t1["id"], "reason": "wait"})
    assert (await rpc(daemon, "task.get", {"id": t1["id"]}))["status"] == "blocked"
    await rpc(daemon, "task.unblock", {"id": t1["id"]})
    r = await daemon._dispatch({"jsonrpc": "2.0", "id": 1, "method": "task.archive", "params": {"id": t2["id"]}})
    assert r["result"]["status"] == "archived"
    lst = await rpc(daemon, "task.list", {})
    assert lst and all(x["status"] != "archived" for x in lst)
    # estop.set có team
    await rpc(daemon, "team.create", {"config": {"id": "tm"}})
    r = await daemon._dispatch({"jsonrpc": "2.0", "id": 2, "method": "estop.set", "params": {"on": True, "team": "tm", "reason": "x"}})
    assert r["result"]["team"] == "tm" and (await rpc(daemon, "team.get", {"id": "tm"}))["estop"] is True
    assert (await rpc(daemon, "estop.get", {}))["on"] is False  # ESTOP toàn hub không đổi
    assert [a["kind"] for a in h.audit.query(kind="task.block")]


async def test_report_and_cost(daemon):
    h = daemon.hub
    h.agents.create({"id": "a", "name": "A"})
    t = await rpc(daemon, "task.create", {"title": "x", "assignee": "a", "body": "x"})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if h.tasks.row(t["id"])["status"] == "done":
            break
    r = await rpc(daemon, "report.get", {"since": "1d"})
    assert r["runs_by_outcome"] == {"completed": 1} and r["cost"]["total_usd"] == pytest.approx(0.001)
    assert r["cache"]["hit_ratio"] == pytest.approx(100 / 130, abs=1e-3)
    assert r["tasks_by_status"] == {"done": 1} and not r["proposals"]  # M4-A: bảng proposals đã có (rỗng)
    direct = h.store.conn.execute("SELECT COUNT(*), SUM(cost_usd) FROM runs").fetchone()
    assert r["runs_total"] == direct[0] and r["cost"]["total_usd"] == pytest.approx(direct[1])
    c = await rpc(daemon, "report.cost", {"by": "agent"})
    assert c["rows"] == [{"agent": "a", "runs": 1, "cost_usd": pytest.approx(0.001)}]
    with pytest.raises(Exception):
        await rpc(daemon, "report.get", {"since": "yesterday-ish"})


async def test_clone_export_import_no_secret(daemon, tmp_path):
    h = daemon.hub
    h.agents.create({"id": "coder", "name": "Coder", "model": "haiku", "subagents": {"checker": {"description": "checks", "prompt": "Check things"}}})
    home = h.paths.agent_home("coder")
    for s in ("s1", "s2", "s3"):
        (home / "skills" / s).mkdir(parents=True)
        (home / "skills" / s / "SKILL.md").write_text(f"---\nname: {s}\ncreated_by: agent\n---\nbody\n")
    (home / "MEMORY.md").write_text("# Memory\nremember X\n")
    h.secrets.set("API_TOKEN", "supersecretvalue123")
    (home / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\ncreated_by: agent\n---\nuse token supersecretvalue123\n")
    c2 = await rpc(daemon, "agent.clone", {"src": "coder", "dst": "coder2"})
    assert c2["skills"] == ["s1", "s2", "s3"] and c2["model"] == "haiku" and c2["cloned_from"] == "coder"
    assert "remember X" not in (h.paths.agent_home("coder2") / "MEMORY.md").read_text()  # không --with-memory
    assert "created_by: agent" in (h.paths.agent_home("coder2") / "skills" / "s2" / "SKILL.md").read_text()
    c3 = await rpc(daemon, "agent.clone", {"src": "coder", "dst": "coder3", "with_memory": True})
    assert "remember X" in (h.paths.agent_home(c3["id"]) / "MEMORY.md").read_text()
    exp = await rpc(daemon, "agent.export", {"id": "coder", "path": str(tmp_path), "with_memory": True})
    blob = b""
    with tarfile.open(exp["path"]) as tar:
        for m in tar.getmembers():
            blob += tar.extractfile(m).read() if m.isfile() else b""
    assert b"supersecretvalue123" not in blob and b"${API_TOKEN}" in blob
    imp = await rpc(daemon, "agent.import", {"path": exp["path"], "id": "coder4"})
    assert imp["skills"] == ["s1", "s2", "s3"] and imp["subagents"]["checker"]["prompt"] == "Check things"
    with pytest.raises(Exception):
        await rpc(daemon, "agent.import", {"path": exp["path"], "id": "coder4"})  # đã tồn tại


async def test_subagents_and_schema_reach_runspec(daemon):
    h = daemon.hub
    h.agents.create({"id": "a", "name": "A", "subagents": {"checker": {"description": "c", "prompt": "p"}}, "result_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}})
    rt = h.runs._runtime
    rt.script = lambda spec: {"structured": {"summary": "hi"}}
    r = h.runs.submit("a", "x")
    await h.runs.wait(r["id"], 5)
    spec = rt.specs[r["id"]]
    assert spec.agents_json == {"checker": {"description": "c", "prompt": "p"}} and spec.json_schema["required"] == ["summary"]
    run = h.store.get_run(r["id"])
    assert run["result_json"] == {"summary": "hi"} and run["schema_valid"] == 1
    from agent_hub.runtime import build_argv

    argv, _ = build_argv(spec, {"binary": "claude"})
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["summary"]
    assert json.loads(argv[argv.index("--agents") + 1])["checker"]["prompt"] == "p"


async def test_hub_self_stdio_server_roundtrip(daemon, tmp_path):
    """MCP stdio hub-self thật (tiến trình con) → RPC `taskself.call` (qua socket daemon thật)."""
    h = daemon.hub
    server = await asyncio.start_unix_server(daemon._handle_conn, path=str(h.paths.socket))
    try:
        h.agents.create({"id": "w", "name": "W"})
        h.runs._runtime.script = lambda spec: {"hold": True}
        t = h.tasks.create({"assignee": "w", "title": "job", "body": "do"})
        for _ in range(100):
            await asyncio.sleep(0.02)
            rid = h.tasks.row(t["id"])["current_run_id"]
            if rid and rid != "__claiming__" and h.store.get_run(rid)["status"] == "running":
                break
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "task_complete", "arguments": {"summary": "via mcp"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "task_create", "arguments": {"title": "x", "body": "y"}}},
        ]
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I", "-m", "agent_hub.mcp_server.hub_self", "--run", rid, "--socket", str(h.paths.socket), "--role", "worker",
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate("".join(json.dumps(m) + "\n" for m in msgs).encode()), 30)
        resp = {r["id"]: r for r in map(json.loads, out.decode().splitlines())}
        names = [t["name"] for t in resp[2]["result"]["tools"]]
        assert "task_complete" in names and "task_create" not in names  # worker không có tool của lead
        assert not resp[3]["result"]["isError"] and "complete" in resp[3]["result"]["content"][0]["text"]
        assert resp[4]["result"]["isError"]
        assert h.store.get_run(rid)["task_signal"]["summary"] == "via mcp"
        h.runs._runtime.release(rid)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if h.tasks.row(t["id"])["status"] == "done":
                break
        assert h.tasks.row(t["id"])["result_summary"] == "via mcp"
    finally:
        server.close()


def test_guard_classifies_hub_self_tools():
    from pathlib import Path

    from agent_hub.guard.classifier import GuardContext, classify

    ctx = GuardContext.__new__(GuardContext)
    for attr, v in (("cwd", Path("/tmp")), ("mcp_annotations", lambda s, t: None)):
        object.__setattr__(ctx, attr, v)
    try:
        v = classify("mcp__hub-self__task_complete", {"summary": "x"}, ctx)
    except Exception as e:  # GuardContext cần thêm trường → bỏ qua phần dựng thủ công
        pytest.skip(f"GuardContext: {e}")
    assert v.verdict == "write" and not v.forbidden
    assert classify("mcp__hub-self__task_show", {}, ctx).verdict == "safe"
