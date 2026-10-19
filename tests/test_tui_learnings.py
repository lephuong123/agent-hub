"""M4-B: tab Learnings (FR-UI-06) + màn golden task trong `cfharness top` — Textual Pilot, daemon IN-PROCESS (socket thật,
FakeRuntime), pipeline/ledger THẬT của M4-A. Không gọi `claude`. Thêm tool MCP `hub_steer`.

Ảnh SVG: `CFHARNESS_SCREENSHOTS=docs/screenshots/m4 uv run pytest tests/test_tui_learnings.py`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.tui.app import ConfirmScreen, InfoScreen, PromptScreen, TopApp
from cfharness.tui.learnings import GoldenScreen, LearningsScreen, checker_to_spec, parse_checker_spec
from test_tui import ISOLATED, SIZE, start_daemon, stop_daemon, until

SHOTS = os.environ.get("CFHARNESS_SCREENSHOTS")


def shot(app, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        app.save_screenshot(filename=name, path=SHOTS)


SKILL = "# Weekly report\n\n1. Collect done items.\n2. One line per item.\n"


def test_checker_spec_roundtrip():
    for spec in ("regex: \\b42\\b", "contains: Paris | France", "judge: Must be one polite sentence.", 'json: {"type": "object"}', "cmd: pytest -q"):
        assert checker_to_spec(parse_checker_spec(spec)) == spec
    assert parse_checker_spec('{"kind": "regex", "pattern": "x"}')["kind"] == "regex"
    with pytest.raises(ValueError):
        parse_checker_spec("regex:")
    with pytest.raises(ValueError):
        parse_checker_spec("weird: x")


def seed(hub):
    """Dữ liệu học thật qua pipeline của M4-A + vài event B/A để feed có đủ loại."""
    hub.agents.create({"id": "researcher", "name": "Researcher", "model": "haiku"})
    hub.agents.create({"id": "coder", "name": "Coder", "model": "haiku"})
    L = hub.learning
    r1 = L.pipeline.propose(target="skill", op="add", agent_id="researcher", payload={"name": "weekly-report", "description": "Weekly status report", "body": SKILL}, created_by="user")
    assert r1["status"] == "applied", r1
    r2 = L.pipeline.propose(target="memory", op="add", agent_id="coder", payload={"store": "memory", "action": "add", "content": "User prefers dates as dd/mm/yyyy."}, created_by="user")
    assert r2["status"] == "applied", r2
    r3 = L.pipeline.propose(target="config", op="patch", agent_id="coder", payload={"changes": {"effort": "low"}}, created_by="user")  # chưa golden → hoãn
    hub.publish(make_event("learning.eval_failed", "researcher", {"proposal_id": "pr_demo", "risk_class": "L3", "target": "config", "target_ref": "soul", "op": "patch",
                                                                 "scores": {"before": 1.0, "after": 0.0}, "reason": "điểm 1.00 → 0.00 (tụt 100%, ngưỡng 10%)"}))
    hub.publish(make_event("guard.blocked", "coder", {"source": "learning", "proposal_id": "pr_bad", "reason": "policy_touch", "detail": "đề xuất sửa danh mục phá huỷ", "target": "config", "op": "patch"}))
    run_id = "r_goldsrc01"
    hub.store.insert_run({"id": run_id, "agent_id": "coder", "kind": "adhoc", "prompt": "What is 17*23? Number only.", "status": "completed", "queued_at": "2099-01-01T00:00:00.000Z",
                          "summary": "391", "cost_usd": 0.0005})
    hub.learning_ops.digest.run(since="2000-01-01T00:00:00Z", send=False)
    return {"skill_entry": r1["ledger_entry_id"], "mem_entry": r2["ledger_entry_id"], "deferred": r3, "run_id": run_id}


@pytest.fixture
async def daemon(hub_home):
    ctl = await start_daemon(hub_home, FakeRuntime(ISOLATED, line_delay_s=0.0))
    yield ctl
    await stop_daemon(ctl)


async def test_learnings_tab_feed_rollback_pin_digest_and_golden(daemon, hub_home):
    hub = daemon.hub
    s = seed(hub)
    app = TopApp(hub_home.socket, takeover_exec=False)
    async with app.run_test(size=SIZE) as pilot:
        # digest lần đầu trong ngày bật lên khi mở TUI (FR-LN-13)
        assert await until(lambda: isinstance(app.screen, InfoScreen), pilot=pilot)
        assert "Digest học" in str(app.screen.query_one(Static).render())
        shot(app, "01-digest-first-open.svg")
        await pilot.press("escape")
        await pilot.press("l")
        assert await until(lambda: isinstance(app.screen, LearningsScreen) and app.screen.query_one("#learn-feed", DataTable).row_count >= 5, pilot=pilot)
        scr: LearningsScreen = app.screen
        kinds = {x["type"] for x in scr.items}
        assert {"learning.applied", "learning.deferred", "learning.eval_failed", "guard.blocked"} <= kinds
        head = str(scr.query_one("#learn-head", Static).render())
        assert "Canary" in head and "Digest" in head
        table = scr.query_one("#learn-feed", DataTable)
        cells = [str(c) for r in range(table.row_count) for c in table.get_row_at(r)]
        assert any("1.00→0.00" in c for c in cells) and any("🔴" in c for c in cells) and any("L3" == c for c in cells)
        shot(app, "02-learnings-feed.svg")
        # chọn dòng áp dụng skill → chi tiết có diff + lệnh rollback
        idx = next(i for i, x in enumerate(scr.items) if x["type"] == "learning.applied" and x["payload"].get("ledger_entry_id") == s["skill_entry"])
        keys = [table.coordinate_to_cell_key((r, 0))[0].value for r in range(table.row_count)]
        table.move_cursor(row=keys.index(str(idx)))
        await pilot.pause(0.3)
        detail = str(scr.query_one("#learn-detail", Static).render())
        assert f"cfharness learn rollback {s['skill_entry']}" in detail and "Weekly" in detail
        shot(app, "03-learnings-detail.svg")
        # p: pin skill (knowledge.pin của A)
        await pilot.press("p")
        assert await until(lambda: any(p["ref"] == "weekly-report" for p in hub.knowledge.pins("agent", "researcher")), pilot=pilot)
        await pilot.press("p")
        assert await until(lambda: not hub.knowledge.pins("agent", "researcher"), pilot=pilot)
        # r: rollback → xác nhận → ledger có entry actor=user, skill bị xoá mềm
        await pilot.press("r")
        assert await until(lambda: isinstance(app.screen, ConfirmScreen), pilot=pilot)
        shot(app, "04-rollback-confirm.svg")
        await pilot.press("y")
        assert await until(lambda: hub.learning.ledger.get(s["skill_entry"]).get("rolled_back_by"), pilot=pilot)
        rb = hub.learning.ledger.get(hub.learning.ledger.get(s["skill_entry"])["rolled_back_by"])
        assert rb["actor"] == "user" and rb["op"] == "rollback"
        assert hub.audit.query(kind="learn.rollback")[0]["channel"] == "tui"
        # f: lọc
        await pilot.press("f")
        await pilot.pause(0.3)
        assert all(x["type"] == "learning.applied" for i, x in enumerate(scr.items) if str(i) in [table.coordinate_to_cell_key((r, 0))[0].value for r in range(table.row_count)])
        # D: digest
        await pilot.press("D")
        assert await until(lambda: isinstance(app.screen, InfoScreen), pilot=pilot)
        await pilot.press("escape")

        # G: golden task — thêm từ run (thao tác NGƯỜI), xem, sửa rubric, xoá mềm
        await pilot.press("G")
        assert await until(lambda: isinstance(app.screen, GoldenScreen), pilot=pilot)
        gs: GoldenScreen = app.screen
        assert await until(lambda: "chưa có golden task" in str(gs.query_one("#golden-head", Static).render()), pilot=pilot)
        shot(app, "05-golden-empty-candidates.svg")
        await pilot.press("a")
        assert await until(lambda: isinstance(app.screen, PromptScreen), pilot=pilot)
        inp = app.screen.query_one(Input)
        inp.value = s["run_id"]
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, PromptScreen) and "Cách chấm" in str(app.screen.title_text), pilot=pilot)
        app.screen.query_one(Input).value = "judge: Output must be exactly the number 391."
        await pilot.press("enter")
        assert await until(lambda: len(hub.evals.golden.active_for("coder")) == 1, pilot=pilot)
        g = hub.evals.golden.active_for("coder")[0]
        assert g["created_by"] == "user:tui" and g["source_run_id"] == s["run_id"] and g["checker"]["kind"] == "llm_judge"
        assert await until(lambda: gs.query_one("#golden", DataTable).row_count == 1, pilot=pilot)
        shot(app, "06-golden-list.svg")
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, InfoScreen), pilot=pilot)
        shot(app, "07-golden-view.svg")
        await pilot.press("escape")
        await pilot.press("e")
        assert await until(lambda: isinstance(app.screen, PromptScreen), pilot=pilot)
        assert app.screen.query_one(Input).value.startswith("judge: Output must be")
        app.screen.query_one(Input).value = "regex: \\b391\\b"
        await pilot.press("enter")
        assert await until(lambda: hub.evals.golden.get(g["id"])["version"] == 2, pilot=pilot)
        assert hub.evals.golden.get(g["id"])["checker"] == {"kind": "regex", "pattern": "\\b391\\b", "must_match": True}
        await pilot.press("delete")
        assert await until(lambda: isinstance(app.screen, ConfirmScreen), pilot=pilot)
        await pilot.press("y")
        assert await until(lambda: not hub.evals.golden.active_for("coder"), pilot=pilot)
        await pilot.press("A")
        assert await until(lambda: gs.query_one("#golden", DataTable).row_count == 1, pilot=pilot)
        await pilot.press("u")
        assert await until(lambda: len(hub.evals.golden.active_for("coder")) == 1, pilot=pilot)
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_digest_popup_only_first_open_of_day(daemon, hub_home):
    hub = daemon.hub
    hub.agents.create({"id": "a", "name": "A"})
    hub.publish(make_event("learning.applied", "a", {"ledger_entry_id": "le_x", "target": "memory", "target_ref": "MEMORY.md", "op": "add", "risk_class": "L1"}))
    hub.learning_ops.digest.run(since="2000-01-01T00:00:00Z", send=False)
    app = TopApp(hub_home.socket, takeover_exec=False)
    async with app.run_test(size=SIZE) as pilot:
        assert await until(lambda: isinstance(app.screen, InfoScreen), pilot=pilot)
    app2 = TopApp(hub_home.socket, takeover_exec=False)
    async with app2.run_test(size=SIZE) as pilot:
        await pilot.pause(0.8)
        assert not isinstance(app2.screen, InfoScreen)


async def test_mcp_hub_steer_tool(hub_home):
    """Tool MCP `hub_steer` gọi RPC `run.steer` (M2): annotations ghi, không phá huỷ; run đã xong → lỗi rõ."""
    from mcp.client.client import Client

    from cfharness.daemon import Daemon
    from cfharness.mcp_server.server import READ_ONLY_TOOLS, build_server

    d = Daemon(hub_home, runtime=FakeRuntime(ISOLATED, line_delay_s=0.0, hold=True))
    d.hub.start()
    loop = asyncio.get_running_loop()

    def caller(method, params):
        fut = asyncio.run_coroutine_threadsafe(d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}), loop)
        resp = fut.result(30)
        if "error" in resp:
            from cfharness.rpc import RpcError

            e = resp["error"]
            raise RpcError(e["code"], e["message"], e.get("data"))
        return resp["result"]

    try:
        d.hub.agents.create({"id": "a", "name": "A"})
        srv = build_server(caller)
        async with Client(srv) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            t = tools["hub_steer"]
            assert t.annotations.read_only_hint is False and t.annotations.destructive_hint is False and "hub_steer" not in READ_ONLY_TOOLS
            run = d.hub.runs.submit("a", "do it")
            assert await until(lambda: d.hub.store.get_run(run["id"])["status"] == "running" and d.hub.runs.active[run["id"]].handle is not None)
            res = await c.call_tool("hub_steer", {"run_id": run["id"], "text": "also check the README"})
            assert not res.is_error, res
            assert res.structured_content["steer_count"] == 1
            assert any(e.type == "user.message" for e in d.hub.store.query_events(run_id=run["id"]))
            assert d.hub.audit.query(kind="run.steer")
            d.hub.runs.runtime().release()
            await d.hub.runs.wait(run["id"], 10)
            res = await c.call_tool("hub_steer", {"run_id": run["id"], "text": "late"})
            assert res.is_error and "không đang chạy" in str(res.content)
    finally:
        d.hub.runs.shutting_down = True
        d.hub.store.close()


async def test_h16_learning_off_key_ledger_by_agent_feedback_and_learned_toast(daemon, hub_home):
    """Hardening H-16 (FR-UI-06/01, FR-LN-13): `L` tắt học cho agent (xác nhận, audit tui), mục Ledger theo agent (`b`),
    `f` feedback trên `top`, thông báo "X learned" khi `learning.applied`."""
    from cfharness.tui.learnings import LedgerScreen

    hub = daemon.hub
    s = seed(hub)
    hub.learning_ops.digest.tui_open = lambda *a, **k: None  # type: ignore[method-assign]
    app = TopApp(hub_home.socket, takeover_exec=False)
    toasts: list[str] = []
    orig = app.notify

    def _notify(message, *a, **kw):
        toasts.append(str(message))
        return orig(message, *a, **kw)

    app.notify = _notify  # type: ignore[method-assign]
    async with app.run_test(size=SIZE) as pilot:
        if await until(lambda: isinstance(app.screen, InfoScreen), timeout=1.0, pilot=pilot):
            await pilot.press("escape")
        # thông báo "X learned" khi có learning.applied (qua firehose)
        r = hub.learning.pipeline.propose(target="memory", op="add", agent_id="researcher", payload={"store": "memory", "action": "add", "content": "Reports go out on Fridays."}, created_by="review")
        assert r["status"] == "applied", r
        assert await until(lambda: any("researcher learned" in t for t in toasts), pilot=pilot), toasts
        # f trên top: feedback cho run gần nhất của agent đang chọn
        await until(lambda: "coder" in app.model.rows, pilot=pilot)
        app.htop.feedback_target = lambda: ("coder", s["run_id"])  # type: ignore[method-assign]  dòng đang chọn → run gần nhất
        await pilot.press("f")
        assert await until(lambda: isinstance(app.screen, PromptScreen), pilot=pilot)
        app.screen.query_one(Input).value = "wrong dates must be dd/mm/yyyy"
        await pilot.press("enter")
        assert await until(lambda: hub.store.conn.execute("SELECT count(*) FROM signals WHERE run_id=? AND kind='correction' AND source='tui'", (s["run_id"],)).fetchone()[0] == 1, pilot=pilot)
        assert hub.audit.query(kind="feedback.record")[0]["channel"] == "tui"
        # tab Learnings: L tắt học cho agent của dòng đang chọn (xác nhận), audit tui
        await pilot.press("l")
        assert await until(lambda: isinstance(app.screen, LearningsScreen) and app.screen.query_one("#learn-feed", DataTable).row_count >= 3, pilot=pilot)
        scr: LearningsScreen = app.screen
        table = scr.query_one("#learn-feed", DataTable)
        idx = next(i for i, x in enumerate(scr.items) if x["type"] == "learning.applied" and x.get("agent_id") == "coder")
        keys = [table.coordinate_to_cell_key((r, 0))[0].value for r in range(table.row_count)]
        table.move_cursor(row=keys.index(str(idx)))
        await pilot.pause(0.2)
        await pilot.press("L")
        assert await until(lambda: isinstance(app.screen, ConfirmScreen), pilot=pilot)
        await pilot.press("y")
        assert await until(lambda: hub.learning.policy.get("agents.coder.enabled") is False, pilot=pilot)
        assert hub.learning.switches("coder")["enabled"] is False and hub.learning.switches("researcher")["enabled"] is True
        assert hub.audit.query(kind="learn.policy_set")[0]["channel"] == "tui"
        # b: Ledger theo agent (learn.ledger {agent})
        await pilot.press("b")
        assert await until(lambda: isinstance(app.screen, LedgerScreen) and app.screen.query_one("#ledger", DataTable).row_count >= 1, pilot=pilot)
        ls: LedgerScreen = app.screen
        assert ls.agent_id == "coder" and all(e["agent_id"] == "coder" for e in ls.entries)
        assert any(e["id"] == s["mem_entry"] for e in ls.entries)
        shot(app, "08-ledger-by-agent.svg")
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause(0.2)
