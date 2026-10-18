"""M3: team (FR-TM-01..04), task + DAG (FR-TK-02..04), dispatcher (FR-TK-05..13), ngân sách ngày (FR-OB-03), rate limit
(FR-RT-16), hub-self (FR-MS-08). Không gọi claude: `ScriptedRuntime` sinh stream-json theo kịch bản."""

import asyncio
import sqlite3
import threading

import pytest

from agent_hub.errors import Blocked, Conflict, HubError
from agent_hub.hub import Hub
from m3_fake import ScriptedRuntime, kind_of


async def settle(cond=None, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond is not None and cond():
            return True
    if cond is not None:
        raise AssertionError("hết giờ chờ điều kiện")
    return False


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script=None, **cfg):
        rt = ScriptedRuntime(script or (lambda spec: {"text": "ok"}))
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        for k, v in cfg.items():
            h.config.set(k, v)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def mk_team(hub, *, lead=True, workers=(("researcher", "researcher", "Finds facts, researches sources"), ("writer", "writer", "Writes reports and documents")), reviewer=False, policies=None):
    members = []
    if lead:
        hub.agents.create({"id": "lead", "name": "Lead", "role": "planner", "description": "Plans and splits work"})
        members.append({"agent_id": "lead", "role": "lead"})
    for aid, role, desc in workers:
        hub.agents.create({"id": aid, "name": aid, "role": role, "description": desc})
        members.append({"agent_id": aid, "role": "worker"})
    if reviewer:
        hub.agents.create({"id": "rev", "name": "Rev", "role": "reviewer", "description": "Reviews results"})
        members.append({"agent_id": "rev", "role": "reviewer"})
    return hub.teams.create({"id": "docs", "name": "Docs", "charter": "Write accurate docs.", "policies": policies or {}, "members": members})


PLAN = {
    "summary": "research then write",
    "subtasks": [
        {"key": "r", "title": "Research topic", "body": "Research the facts", "role": "researcher", "parents": []},
        {"key": "w", "title": "Write report", "body": "Write the report from the research", "role": "writer", "parents": ["r"]},
    ],
}


# ============================================================================ team
async def test_team_crud_lead_unique_archive_restore(make_hub):
    hub, _ = make_hub()
    t = mk_team(hub)
    assert [m["role"] for m in t["members"]] == ["lead", "worker", "worker"]
    hub.agents.create({"id": "lead2", "name": "L2"})
    with pytest.raises(Conflict):
        hub.teams.member_set("docs", "lead2", "lead")
    with pytest.raises(HubError):
        hub.teams.member_set("docs", "lead2", "boss")
    with pytest.raises(HubError):
        hub.teams.create({"id": "x", "policies": {"failure_limit": 0}})
    up = hub.teams.update("docs", {"policies": {"budget_usd_per_day": 1.5}})
    assert up["policies"]["budget_usd_per_day"] == 1.5 and up["policies"]["failure_limit"] == 2
    # agent thuộc nhiều team
    hub.teams.create({"id": "ops", "members": [{"agent_id": "writer", "role": "worker"}]})
    assert hub.teams.member("ops", "writer")
    # archive: có task đang chạy → từ chối; --cancel-open-tasks → huỷ rồi archive
    hub.tasks.create({"team": "docs", "assignee": "writer", "title": "x", "body": "y"})
    hub.store.conn.execute("UPDATE tasks SET status='running'")
    with pytest.raises(Conflict):
        await hub.teams.archive("docs")
    await hub.teams.archive("docs", cancel_open_tasks=True)
    assert hub.store.conn.execute("SELECT status FROM tasks").fetchone()[0] == "archived"
    assert [x["id"] for x in hub.teams.list()] == ["ops"]
    assert not (hub.paths.home / "teams" / "docs").exists()
    hub.teams.restore("docs")
    assert (hub.paths.home / "teams" / "docs" / "memory.md").exists()
    show = hub.teams.show("docs")
    assert show["counts"] == {"archived": 1} and show["estop"] is False


# ============================================================================ task, DAG
async def test_task_idempotency_dag_cycle_and_promotion(make_hub):
    hub, rt = make_hub()
    mk_team(hub, lead=False)
    a = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "A", "body": "a", "idempotency_key": "k"})
    a2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "A", "body": "a", "idempotency_key": "k"})
    assert a2["id"] == a["id"] and a2["idempotent_replay"]
    assert hub.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    b = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "B", "body": "b", "parents": [a["id"]]})
    assert b["status"] == "todo"
    with pytest.raises(Conflict):
        hub.tasks.link(b["id"], a["id"])  # A → B → A
    with pytest.raises(HubError):
        hub.tasks.create({"team": "docs", "assignee": "outsider", "title": "z"})
    await settle(lambda: hub.tasks.row(b["id"])["status"] == "done")
    # prompt của B có tóm tắt kết quả A (FR-TK-04)
    spec_b = next(s for s in rt.specs.values() if s.prompt.startswith(f"[agent-hub task {b['id']}"))
    assert "Results of prerequisite tasks" in spec_b.prompt and "Research" in spec_b.prompt or "A (" in spec_b.prompt
    runs = hub.store.list_runs()
    assert all(r["task_id"] in (a["id"], b["id"]) and r["team_id"] == "docs" and r["kind"] == "task" for r in runs)
    evs = [e.type for e in hub.store.query_events() if e.task_id == b["id"]]
    assert "task.created" in evs and "task.status" in evs and "run.completed" in evs


async def test_comments_in_prompt(make_hub):
    hub, rt = make_hub()
    mk_team(hub, lead=False)
    hub.teams.update("docs", {"policies": {}})
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "C", "body": "c", "parents": []})
    # đã chạy ngay — tạo task khác chưa đủ cha để chèn comment trước khi spawn
    gate = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "gate", "body": "g"})
    t2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "D", "body": "d", "parents": [gate["id"]]})
    for i in range(3):
        hub.tasks.comment(t2["id"], f"note {i}", author="human")
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "done")
    p = next(s.prompt for s in rt.specs.values() if s.prompt.startswith(f"[agent-hub task {t2['id']}"))
    assert p.index("note 0") < p.index("note 1") < p.index("note 2")
    assert hub.tasks.row(t["id"])["status"] == "done"


# ============================================================================ lead decompose (FR-TK-08)
async def test_lead_decomposes_routes_by_role_and_aggregates(make_hub):
    def script(spec):
        k = kind_of(spec)
        if k == "lead":
            assert spec.json_schema and "subtasks" in spec.json_schema["properties"]
            return {"structured": PLAN, "text": "plan", "cost": 0.002}
        if "## Task: Research topic" in spec.prompt:
            return {"text": "FACT: water boils at 100C", "cost": 0.001}
        return {"text": "REPORT based on research", "cost": 0.001}

    hub, rt = make_hub(script)
    mk_team(hub)
    root = hub.tasks.create({"team": "docs", "title": "Research then write a report", "body": "about water"})
    assert root["status"] == "triage" and root["kind"] == "root"
    await settle(lambda: hub.tasks.row(root["id"])["status"] == "done")
    subs = hub.tasks.subtasks(root["id"])
    assert len(subs) == 2
    by_title = {s["title"]: s for s in subs}
    assert by_title["Research topic"]["assignee_agent_id"] == "researcher"
    assert by_title["Write report"]["assignee_agent_id"] == "writer"
    assert hub.tasks.parents(by_title["Write report"]["id"]) == [by_title["Research topic"]["id"]]
    write_prompt = next(s.prompt for s in rt.specs.values() if "Write report" in s.prompt and s.prompt.startswith("[agent-hub task"))
    assert "FACT: water boils" in write_prompt  # kết quả cha vào ngữ cảnh con
    r = hub.tasks.row(root["id"])
    assert "REPORT based on research" in r["result_json"]["final"] and len(r["result_json"]["subtasks"]) == 2
    lead_run = next(x for x in hub.store.list_runs() if x["kind"] == "lead")
    assert lead_run["result_json"] == PLAN and lead_run["schema_valid"] == 1
    # hub-self MCP có trong mcp.json của run task (FR-MS-08)
    assert any(s.allowed_mcp_servers and "hub-self" in s.allowed_mcp_servers for s in rt.specs.values())
    assert "task.decomposed" in [e.type for e in hub.store.query_events()]


async def test_invalid_plan_blocks_root_without_children(make_hub):
    bad = {"subtasks": [{"key": "a", "title": "A", "body": "a", "parents": ["b"]}, {"key": "b", "title": "B", "body": "b", "parents": ["a"]}]}
    hub, _ = make_hub(lambda spec: {"structured": bad} if kind_of(spec) == "lead" else {"text": "x"})
    mk_team(hub)
    root = hub.tasks.create({"team": "docs", "title": "do it", "body": "x"})
    await settle(lambda: hub.tasks.row(root["id"])["status"] == "blocked")
    assert "chu trình" in hub.tasks.row(root["id"])["block_reason"]
    assert hub.tasks.subtasks(root["id"]) == []
    lead_run = next(x for x in hub.store.list_runs() if x["kind"] == "lead")
    assert lead_run["outcome"] == "protocol_violation"


# ============================================================================ crash / retry / circuit breaker
async def test_crash_retry_resumes_session_then_circuit_opens(make_hub):
    calls = {"n": 0}

    def script(spec):
        calls["n"] += 1
        if "flaky" in spec.prompt and calls["n"] == 1:
            return {"crash": True}
        if "always" in spec.prompt or (spec.resume_session_id and calls.get("always")):
            return {"crash": True}
        return {"text": "fixed"}

    hub, rt = make_hub(script)
    mk_team(hub, lead=False)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "flaky job", "body": "flaky"})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    runs = sorted(hub.store.list_runs(), key=lambda r: r["queued_at"])
    assert [r["outcome"] for r in runs] == ["crashed", "completed"]
    assert runs[1]["parent_run_id"] == runs[0]["id"] and runs[1]["resumed_from_session"] == runs[0]["session_id"]
    assert runs[1]["session_id"] == runs[0]["session_id"]  # --resume <session cũ>
    assert rt.specs[runs[1]["id"]].resume_session_id == runs[0]["session_id"]
    # failure_limit=2: crash 2 lần liên tiếp → blocked + task.circuit_open
    calls["always"] = True
    t2 = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "always crash", "body": "always"})
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "blocked")
    row = hub.tasks.row(t2["id"])
    assert row["consecutive_failures"] == 2 and "circuit open" in row["block_reason"]
    assert any(e.type == "task.circuit_open" and e.task_id == t2["id"] for e in hub.store.query_events())
    # người unblock → ready lại
    hub.tasks.unblock(t2["id"])
    assert hub.tasks.row(t2["id"])["consecutive_failures"] == 0


async def test_timeout_marks_timed_out_and_retries(make_hub):
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        return {"hold": n["i"] == 1, "text": "ok"}

    hub, _ = make_hub(script)
    mk_team(hub, lead=False)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "slow", "body": "s", "max_runtime_s": 0.3})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done", timeout=8)
    outs = [r["outcome"] for r in sorted(hub.store.list_runs(), key=lambda r: r["queued_at"])]
    assert outs == ["timed_out", "completed"]


async def test_rate_limited_requeued_with_backoff_not_counted(make_hub):
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        if n["i"] == 1:
            return {"subtype": "error_during_execution", "error_text": "API Error: rate limit exceeded", "api_error_status": 429}
        return {"text": "ok"}

    hub, _ = make_hub(script)
    hub.config.set("tasks.rate_limit_backoff_s", 0.2)
    mk_team(hub, lead=False)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    await settle(lambda: hub.store.list_runs() and hub.store.list_runs()[-1]["outcome"] == "rate_limited")
    row = hub.tasks.row(t["id"])
    assert row["status"] == "ready" and row["not_before"] and row["consecutive_failures"] == 0 and row["rate_limit_hits"] == 1
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")


async def test_rate_limit_event_throttles_concurrency(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok", "rate_limit": {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.95}}}})
    hub.config.set("hub.max_concurrent_runs", 4)
    hub.agents.create({"id": "a", "name": "A"})
    r = hub.runs.submit("a", "x")
    await hub.runs.wait(r["id"], 5)
    assert hub.store.state_get("rate_limit")["throttled"] is True
    assert hub.runs.effective_limit() == 1
    assert any(e.type == "ratelimit.throttle" for e in hub.store.query_events())


# ============================================================================ ngân sách ngày (FR-OB-03)
async def test_team_budget_blocks_claims(make_hub):
    hub, rt = make_hub(lambda spec: {"text": "ok", "cost": 0.02})
    mk_team(hub, lead=False, policies={"budget_usd_per_day": 0.03})
    t1 = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "one", "body": "1"})
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "done")
    first = hub.store.list_runs()[0]
    assert first["max_budget_usd"] == pytest.approx(0.03)  # trần run = phần còn lại của ngày
    t2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "two", "body": "2"})
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "done")
    assert hub.store.list_runs()[0]["max_budget_usd"] == pytest.approx(0.01)
    t3 = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "three", "body": "3"})
    await settle(lambda: hub.tasks.row(t3["id"])["wait_reason"] == "budget:team")
    row = hub.tasks.row(t3["id"])
    assert row["status"] == "ready" and row["current_run_id"] is None
    assert hub.teams.show("docs")["budget_remaining_usd"] == 0
    assert any(e.type == "budget.exceeded" and e.payload.get("scope") == "team" for e in hub.store.query_events())
    assert any(e.type == "budget.warning" and e.payload.get("scope") == "team" for e in hub.store.query_events())
    # run ad-hoc gắn team cũng bị chặn
    with pytest.raises(Blocked):
        hub.runs.submit("writer", "adhoc", team_id="docs")
    # nâng ngân sách → claim ngay
    hub.teams.update("docs", {"policies": {"budget_usd_per_day": 1}})
    await settle(lambda: hub.tasks.row(t3["id"])["status"] == "done")


async def test_agent_and_hub_daily_budget(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok", "cost": 0.05})
    hub.agents.create({"id": "a", "name": "A", "limits": {"budget_usd_per_day": 0.05}})
    t = hub.tasks.create({"assignee": "a", "title": "x", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    t2 = hub.tasks.create({"assignee": "a", "title": "y", "body": "y"})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "budget:agent")
    hub.agents.create({"id": "b", "name": "B"})
    hub.config.set("budget.hub_usd_per_day", 0.05)
    with pytest.raises(Blocked) as e:
        hub.runs.submit("b", "x")
    assert "toàn hub" in e.value.message


# ============================================================================ review (FR-TK-12)
async def test_review_request_changes_then_approve(make_hub):
    verdicts = iter([{"verdict": "request_changes", "comment": "add sources"}, {"verdict": "approve", "comment": "good"}])

    def script(spec):
        if kind_of(spec) == "review":
            assert "Edit" in (spec.tool_policy.get("disallowed_tools") or [])  # reviewer chỉ đọc
            return {"structured": next(verdicts)}
        return {"text": "draft"}

    hub, rt = make_hub(script)
    mk_team(hub, lead=False, reviewer=True, policies={"review_required": True})
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "doc", "body": "write doc"})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done", timeout=8)
    kinds = [r["kind"] for r in sorted(hub.store.list_runs(), key=lambda r: r["queued_at"])]
    assert kinds == ["task", "review", "task", "review"]
    cs = hub.tasks.comments(t["id"])
    assert [c["kind"] for c in cs] == ["review:request_changes", "review:approve"]
    second_worker = [s for s in rt.specs.values() if s.prompt.startswith(f"[agent-hub task {t['id']}")][-1]
    assert "add sources" in second_worker.prompt  # worker mới đọc comment yêu cầu sửa
    sig = hub.store.conn.execute("SELECT agent_id, kind FROM signals").fetchall()
    assert [tuple(x) for x in sig] == [("writer", "reject")]


# ============================================================================ hub-self (FR-MS-08)
async def test_hub_self_complete_block_and_scope(make_hub):
    hub, rt = make_hub(lambda spec: {"hold": True, "text": "final words"})
    mk_team(hub)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "w", "body": "w"})
    other = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "r", "body": "r"})
    await settle(lambda: hub.tasks.row(t["id"])["current_run_id"] not in (None, "__claiming__") and hub.tasks.row(other["id"])["current_run_id"] not in (None, "__claiming__"))
    rid = hub.tasks.row(t["id"])["current_run_id"]
    await settle(lambda: hub.store.get_run(rid)["status"] == "running")
    d = hub.dispatcher
    show = d.self_call(rid, "task_show", {})
    assert show["id"] == t["id"] and show["title"] == "w"
    with pytest.raises(HubError):
        d.self_call(rid, "task_complete", {"task_id": other["id"], "summary": "hack"})  # task không phải của mình
    with pytest.raises(HubError):
        d.self_call(rid, "task_create", {"title": "x", "body": "y"})  # chỉ lead
    d.self_call(rid, "task_comment", {"text": "progress"})
    d.self_call(rid, "task_complete", {"summary": "explicit summary", "result": {"n": 1}})
    rt.release(rid)
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    row = hub.tasks.row(t["id"])
    assert row["result_summary"] == "explicit summary" and row["result_json"] == {"n": 1}
    # task_block → task blocked, run outcome=blocked
    rid2 = hub.tasks.row(other["id"])["current_run_id"]
    d.self_call(rid2, "task_block", {"reason": "need credentials"})
    rt.release(rid2)
    await settle(lambda: hub.tasks.row(other["id"])["status"] == "blocked")
    assert hub.tasks.row(other["id"])["block_reason"] == "need credentials"
    assert hub.store.get_run(rid2)["outcome"] == "blocked"


async def test_require_completion_call_protocol_violation(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "forgot"})
    hub.config.set("tasks.require_completion_call", True)
    hub.agents.create({"id": "a", "name": "A"})
    t = hub.tasks.create({"assignee": "a", "title": "x", "body": "x", "max_retries": 1})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "blocked")
    assert hub.store.list_runs()[0]["outcome"] == "protocol_violation"


# ============================================================================ claim nguyên tử, giới hạn, ESTOP team
async def test_claim_is_atomic_across_connections(make_hub, hub_home):
    hub, _ = make_hub()
    hub.agents.create({"id": "a", "name": "A"})
    hub.agents.set_paused("a", True)  # dispatcher không tự claim
    t = hub.tasks.create({"assignee": "a", "title": "x", "body": "x"})
    wins = []
    barrier = threading.Barrier(8)

    def claim():
        c = sqlite3.connect(str(hub_home.db), timeout=10, isolation_level=None)
        barrier.wait()
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute("UPDATE tasks SET status='running', current_run_id='x' WHERE id=? AND status='ready' AND current_run_id IS NULL", (t["id"],))
        c.execute("COMMIT")
        wins.append(cur.rowcount)
        c.close()

    ths = [threading.Thread(target=claim) for _ in range(8)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert sorted(wins) == [0] * 7 + [1]
    # 2 tick chồng nhau (sự kiện + timer) trong daemon không claim 2 lần
    hub.store.conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL")
    hub.agents.set_paused("a", False)
    hub.dispatcher.tick()
    hub.dispatcher.tick()
    hub.dispatcher.kick()
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    assert len(hub.store.list_runs()) == 1


async def test_global_limit_and_member_limit(make_hub):
    hub, rt = make_hub(lambda spec: {"hold": True})
    hub.config.set("hub.max_concurrent_runs", 2)
    mk_team(hub, lead=False)
    hub.agents.update("writer", {"limits": {"max_concurrent_runs": 5}})
    ts = [hub.tasks.create({"team": "docs", "assignee": "writer", "title": f"t{i}", "body": "x", "priority": i}) for i in range(3)]
    await settle(lambda: sum(1 for t in ts if hub.tasks.row(t["id"])["status"] == "running") == 1)
    # max_in_progress=1 của thành viên → chỉ 1 task của writer chạy
    assert hub.tasks.row(ts[2]["id"])["status"] == "running"  # ưu tiên cao nhất trước
    assert {hub.tasks.row(t["id"])["wait_reason"] for t in ts[:2]} == {"member_limit"}
    hub.teams.member_set("docs", "writer", "worker", max_in_progress=5)
    await settle(lambda: sum(1 for t in ts if hub.tasks.row(t["id"])["status"] == "running") == 2)
    assert hub.tasks.row(ts[0]["id"])["wait_reason"] == "concurrency"
    rt.release()
    await settle(lambda: all(hub.tasks.row(t["id"])["status"] == "done" for t in ts) or (rt.release() and False))


async def test_team_estop(make_hub):
    hub, rt = make_hub(lambda spec: {"hold": True})
    mk_team(hub, lead=False)
    t1 = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "a", "body": "a"})
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "running")
    await hub.teams.set_estop("docs", True, reason="stop it")
    t2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "b", "body": "b"})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "team_estop")
    assert hub.tasks.row(t1["id"])["status"] == "running"  # không --stop-running → run cũ chạy tiếp
    with pytest.raises(Blocked):
        hub.runs.submit("writer", "x", team_id="docs")
    rt.release()
    await settle(lambda: hub.tasks.row(t1["id"])["status"] == "done")
    await hub.teams.set_estop("docs", False)
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "running")
    # --stop-running: run đang chạy bị dừng, task về ready (không tính lỗi)
    await hub.teams.set_estop("docs", True, stop_running=True)
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "ready")
    assert hub.tasks.row(t2["id"])["consecutive_failures"] == 0
    await hub.teams.set_estop("docs", False)
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "running")
    rt.release()
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "done")


async def test_routing_prefers_description_and_load():
    from agent_hub.dispatch.routing import choose_worker

    members = [
        {"agent_id": "w1", "role": "worker", "agent_role": "writer", "agent_description": "Viết báo cáo, tài liệu"},
        {"agent_id": "w2", "role": "worker", "agent_role": "researcher", "agent_description": "Nghiên cứu, tìm nguồn"},
        {"agent_id": "l", "role": "lead", "agent_role": "planner", "agent_description": "research write"},
    ]
    load = {"w1": 0, "w2": 0}
    assert choose_worker({"title": "Nghiên cứu luật mới", "body": ""}, members, lambda a: load[a]) == "w2"
    assert choose_worker({"title": "x", "body": "", "role": "viết"}, members, lambda a: load[a]) == "w1"
    load["w1"] = 3
    assert choose_worker({"title": "zzz", "body": ""}, members, lambda a: load[a]) == "w2"  # không khớp → ít tải nhất


async def test_fr_tk04_child_prompt_contains_parent_results(make_hub):
    """FR-TK-04 (assert chặt, thay `a and b or c` thiếu ngoặc): prompt của task con chứa NGUYÊN VĂN kết quả mọi task cha."""
    def script(spec):
        return {"text": "PARENT-RESULT-ALPHA-42" if "## Task: A" in spec.prompt else ("PARENT-RESULT-BETA-7" if "## Task: C" in spec.prompt else "child done")}

    hub, rt = make_hub(script)
    mk_team(hub, lead=False)
    a = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "A", "body": "a"})
    c = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "C", "body": "c"})
    b = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "B", "body": "b", "parents": [a["id"], c["id"]]})
    await settle(lambda: hub.tasks.row(b["id"])["status"] == "done")
    spec_b = next(s for s in rt.specs.values() if s.prompt.startswith(f"[agent-hub task {b['id']}"))
    assert "Results of prerequisite tasks" in spec_b.prompt
    assert "PARENT-RESULT-ALPHA-42" in spec_b.prompt and "PARENT-RESULT-BETA-7" in spec_b.prompt
    assert f"({a['id']}," in spec_b.prompt and f"({c['id']}," in spec_b.prompt
