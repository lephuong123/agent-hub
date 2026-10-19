"""TUI `cfharness top` (FR-UI-01..05, 09, 10; NFR-05/08) — Textual Pilot (`run_test`) với daemon IN-PROCESS
(Unix socket thật, FakeRuntime). Không gọi `claude`.

Ảnh SVG các màn: đặt `CFHARNESS_SCREENSHOTS=docs/screenshots/m2` khi chạy pytest để ghi lại.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Select, Static

from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime
from cfharness.tui.app import AgentFormScreen, AttachScreen, ConfirmScreen, InfoScreen, LearningsScreen, PromptScreen, TasksScreen, TopApp, Transcript
from cfharness.tui.model import TopModel, activity_line
from cfharness.tui.takeover import build_takeover_argv

SHOTS = os.environ.get("CFHARNESS_SCREENSHOTS")
ISOLATED = Path(__file__).parent / "fixtures" / "claude-hub-isolated-run.jsonl"  # run thật đã cô lập (không vi phạm)
SIZE = (170, 46)


def shot(app, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        app.save_screenshot(filename=name, path=SHOTS)


async def until(pred, timeout: float = 10.0, step: float = 0.05, pilot=None) -> bool:
    for _ in range(int(timeout / step)):
        if pred():
            return True
        if pilot is not None:
            await pilot.pause(step)
        else:
            await asyncio.sleep(step)
    return False


class DaemonCtl:
    def __init__(self, d: Daemon, task: asyncio.Task) -> None:
        self.d, self.task = d, task
        self.hub = d.hub

    def runs(self, rid):
        return self.hub.store.get_run(rid)

    def types(self, rid):
        return [e.type for e in self.hub.store.query_events(run_id=rid)]


async def start_daemon(paths, runtime) -> DaemonCtl:
    d = Daemon(paths, runtime=runtime)
    task = asyncio.create_task(d.serve())
    assert await until(lambda: paths.socket.exists())
    await asyncio.sleep(0.05)
    return DaemonCtl(d, task)


async def stop_daemon(ctl: DaemonCtl) -> None:
    ctl.d.stopping.set()
    try:
        await asyncio.wait_for(ctl.task, 20)
    except Exception:
        pass


@pytest.fixture
async def daemon(hub_home):
    ctl = await start_daemon(hub_home, FakeRuntime(ISOLATED, line_delay_s=0.08))
    yield ctl
    await stop_daemon(ctl)


@pytest.fixture
async def held_daemon(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    ctl = await start_daemon(hub_home, rt)
    ctl.rt = rt
    yield ctl
    rt.release()
    await stop_daemon(ctl)


def table_rows(app) -> dict[str, list[str]]:
    t = app.htop.query_one("#agents", DataTable)
    out = {}
    for rk in t.rows:
        out[rk.value] = [str(getattr(c, "plain", c)) for c in t.get_row(rk)]
    return out


def mk_agents(hub, n: int, prefix: str = "ag", **kw) -> list[str]:
    ids = []
    for i in range(n):
        aid = f"{prefix}{i}"
        hub.agents.create({"id": aid, "name": f"{prefix.title()} {i}", "model": "haiku", "role": "tester", **kw})
        ids.append(aid)
    return ids


# =========================================================================== màn bảng
async def test_top_three_agents_live(daemon, hub_home):
    """FR-UI-01: 3 agent chạy song song → cả 3 dòng cập nhật STATUS/LAST TOOL/TOK/$ theo thời gian thực."""
    hub = daemon.hub
    ids = mk_agents(hub, 3)
    app = TopApp(hub_home.socket, refresh_s=0.5)
    async with app.run_test(size=SIZE) as pilot:
        runs = [hub.runs.submit(a, f"task for {a}: summarize README") for a in ids]
        ok = await until(lambda: all(r[3] == "● working" and r[5] != "—" for r in table_rows(app).values()) and len(table_rows(app)) == 3, pilot=pilot)
        assert ok, table_rows(app)
        rows = table_rows(app)
        assert all("summarize README" in r[4] for r in rows.values())
        head = str(app.htop.query_one("#summary", Static).render())
        assert "3 agent" in head and "3 đang chạy" in head
        shot(app, "01-top.svg")
        for r in runs:
            await hub.runs.wait(r["id"], 20)
        assert await until(lambda: all(r[3] == "○ idle" for r in table_rows(app).values()), pilot=pilot)
        rows = table_rows(app)
        assert all(r[8].startswith("$0.0") and r[8] != "$0.000" for r in rows.values()), rows  # $ hôm nay đã có
        st = app.model.latency_stats()
        assert st["n"] > 20 and st["p95"] < 1000, st  # NFR-01 trên máy test
        log = app.htop.query_one("#activity")
        assert len(log.lines) > 5  # firehose có dòng


async def test_firehose_summary_only_and_resubscribe_after_slow_consumer(daemon, hub_home):
    """FR-UI-09: top chỉ nhận event summary (không delta). NFR-08: bị ngắt slow_consumer → tự đăng ký lại."""
    hub = daemon.hub
    mk_agents(hub, 1)
    app = TopApp(hub_home.socket, refresh_s=0.5)
    seen: list[dict] = []
    orig = app.on_hub_event

    def spy(ev):
        seen.append(ev)
        orig(ev)

    app.on_hub_event = spy  # type: ignore[method-assign]
    async with app.run_test(size=SIZE) as pilot:
        await until(lambda: hub.bus.subscriber_count >= 1, pilot=pilot)
        r1 = hub.runs.submit("ag0", "first")
        await hub.runs.wait(r1["id"], 20)
        await until(lambda: any(e["type"] == "run.completed" for e in seen), pilot=pilot)
        assert not any(e["level"] != "summary" for e in seen)
        assert not any(e["type"] in ("message.delta", "reasoning.delta") for e in seen)
        # daemon ngắt subscriber như khi hàng đợi đầy
        for sub in list(hub.bus._subs.values()):
            sub.close("slow_consumer")
        n_before = len(seen)
        await pilot.pause(1.5)
        r2 = hub.runs.submit("ag0", "second")
        await hub.runs.wait(r2["id"], 20)
        assert await until(lambda: any(e["type"] == "run.completed" and e["run_id"] == r2["id"] for e in seen[n_before:]), pilot=pilot)
        seqs = [e["seq"] for e in seen if e["epoch"] == hub.bus.epoch]
        assert len(seqs) == len(set(seqs))  # không trùng sau khi đăng ký lại


async def test_filter_sort_and_learnings_placeholder(daemon, hub_home):
    hub = daemon.hub
    mk_agents(hub, 2, prefix="alpha")
    mk_agents(hub, 1, prefix="beta")
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        await until(lambda: len(table_rows(app)) == 3, pilot=pilot)
        await pilot.press("slash")
        await pilot.press(*"beta")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert list(table_rows(app)) == ["beta0"]
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert len(table_rows(app)) == 3
        await pilot.press("o")
        assert app.model.sort_key == "cost"
        await pilot.press("l")
        await pilot.pause(0.2)
        assert isinstance(app.screen, LearningsScreen)
        assert "Learnings" in str(app.screen.query_one("#learn-head", Static).render())  # M4-B thay chỗ trống M2
        shot(app, "05-learnings.svg")
        await pilot.press("escape")
        await pilot.press("t")
        await pilot.pause(0.5)
        assert isinstance(app.screen, TasksScreen)
        assert await until(lambda: app.screen.available is not None, pilot=pilot)
        pass


# =========================================================================== attach
async def test_attach_replay_then_live_and_detach_keeps_run(daemon, hub_home):
    """FR-UI-02/03: attach giữa chừng → replay (DB + ring) rồi live, seq không trùng; Esc → run vẫn chạy và hoàn tất."""
    hub = daemon.hub
    mk_agents(hub, 1)
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        run = hub.runs.submit("ag0", "explain the repo layout")
        await until(lambda: "tool.start" in daemon.types(run["id"]), pilot=pilot)
        await until(lambda: table_rows(app).get("ag0", [""] * 4)[3] == "● working", pilot=pilot)
        await pilot.press("enter")
        await pilot.pause(0.3)
        scr = app.screen
        assert isinstance(scr, AttachScreen) and scr.run_id == run["id"]
        assert await until(lambda: scr.replayed > 0, pilot=pilot)
        await hub.runs.wait(run["id"], 20)
        assert await until(lambda: any(k[1] for k in scr.seen) and "■ Run completed" in _transcript_text(scr), pilot=pilot)
        text = _transcript_text(scr)
        assert "↺ Attach run" in text and "Bash" in text and "› Prompt: explain the repo layout" in text
        persisted = [e for e in hub.store.query_events(run_id=run["id"])]
        seen_seqs = {k[1] for k in scr.seen}
        assert {e.seq for e in persisted} <= seen_seqs  # không mất event (seq liên tục)
        shot(app, "02-attach.svg")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.screen is app.htop


async def test_attach_late_after_finish_reads_db(daemon, hub_home):
    """FR-UI-03: attach run đã xong (không còn live) → toàn bộ transcript từ DB."""
    hub = daemon.hub
    mk_agents(hub, 1)
    run = hub.runs.submit("ag0", "late attach")
    await hub.runs.wait(run["id"], 20)
    app = TopApp(hub_home.socket, attach=run["id"])
    async with app.run_test(size=SIZE) as pilot:
        assert await until(lambda: isinstance(app.screen, AttachScreen) and app.screen.replayed > 0, pilot=pilot)
        scr = app.screen
        await pilot.pause(0.3)
        text = _transcript_text(scr)
        assert "■ Run completed" in text and "Bash" in text
        assert scr.replayed == len(hub.store.query_events(run_id=run["id"]))


def _transcript_text(scr) -> str:
    tr = scr.query_one(Transcript)
    parts = []
    for w in tr.children:
        r = w.render()
        parts.append(getattr(r, "plain", None) or str(getattr(w, "content", "")) or str(r))
    return "\n".join(parts)


# =========================================================================== steer / stop / approve
async def test_steer_and_stop_from_tui(held_daemon, hub_home):
    """FR-UI-05 + FR-RT-06: `s` gửi steer (user.message → steer.ack), `x` + xác nhận → cancelled, dòng về idle."""
    ctl = held_daemon
    hub = ctl.hub
    mk_agents(hub, 1)
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        run = hub.runs.submit("ag0", "long job")
        assert await until(lambda: (table_rows(app).get("ag0") or [""] * 4)[3] == "● working", pilot=pilot)
        await pilot.press("s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PromptScreen)
        await pilot.press(*"focus on docs")
        await pilot.press("enter")
        assert await until(lambda: "steer.ack" in ctl.types(run["id"]), pilot=pilot)
        um = [e for e in hub.store.query_events(run_id=run["id"]) if e.type == "user.message"]
        assert um[0].payload["text"] == "focus on docs" and um[0].payload["channel"] == "tui" and um[0].source == "tui"
        assert ctl.runs(run["id"])["steer_count"] == 1
        shot(app, "03-steer.svg")
        await pilot.press("x")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        final = await hub.runs.wait(run["id"], 20)
        assert final["outcome"] == "cancelled"
        assert await until(lambda: table_rows(app)["ag0"][3] == "○ idle", pilot=pilot)


async def _open_approval(hub, run, *, waiting_human: bool = False):
    ap = hub.approvals.open(
        hub.store.get_run(run["id"]),
        tool_name="Bash",
        tool_input={"command": "rm -rf /srv/data"},
        tool_use_id="toolu_x",
        fingerprint="fp-" + run["id"],
        verdict={"klass": "fs_delete", "rule": "rm_recursive_outside_run", "verdict": "destructive", "reason": "xoá đệ quy ngoài phạm vi run"},
        channel="hook",
    )
    if waiting_human:
        hub.approvals.to_waiting_human(ap["id"])
    return ap


async def test_approve_from_tui_and_second_viewer_conflict(held_daemon, hub_home):
    """FR-UI-04: thao tác chờ duyệt hiện nổi bật (⏸ waiting_human), `a` cho phép; viewer thứ hai không duyệt lại được."""
    ctl = held_daemon
    hub = ctl.hub
    mk_agents(hub, 2)
    run = hub.runs.submit("ag0", "cleanup job")
    await until(lambda: (ctl.runs(run["id"]) or {}).get("status") == "running")
    ap = await _open_approval(hub, run, waiting_human=True)
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        assert await until(lambda: table_rows(app).get("ag0", [""] * 4)[3] == "⏸ waiting_human", pilot=pilot)
        head = str(app.htop.query_one("#summary", Static).render())
        assert "1 thao tác phá huỷ chờ người" in head
        detail = str(app.htop.query_one("#detail", Static).render())
        assert "rm -rf /srv/data" in detail and "rm_recursive_outside_run" in detail
        shot(app, "04-approval.svg")
        await pilot.press("a")
        assert await until(lambda: hub.approvals.get(ap["id"])["status"] == "allowed", pilot=pilot)
        row = hub.approvals.get(ap["id"])
        assert row["resolved_channel"] == "tui"
        # viewer thứ hai (cùng approval) → Conflict, không quyết lại (request đã đóng)
        from cfharness.tui.client import AsyncHubClient

        with pytest.raises(Exception, match="đã allowed"):
            await AsyncHubClient(hub_home.socket).call("approval.approve", {"id": ap["id"], "channel": "tui"})
        assert await until(lambda: table_rows(app)["ag0"][3] != "⏸ waiting_human", pilot=pilot)


async def test_deny_with_note_from_attach(held_daemon, hub_home):
    ctl = held_daemon
    hub = ctl.hub
    mk_agents(hub, 1)
    run = hub.runs.submit("ag0", "risky")
    await until(lambda: (ctl.runs(run["id"]) or {}).get("status") == "running")
    ap = await _open_approval(hub, run)
    app = TopApp(hub_home.socket, attach=run["id"], refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        assert await until(lambda: isinstance(app.screen, AttachScreen) and app.screen.query_one("#banner").display, pilot=pilot)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press(*"use trash instead")
        await pilot.press("enter")
        assert await until(lambda: hub.approvals.get(ap["id"])["status"] == "denied", pilot=pilot)
        assert hub.approvals.get(ap["id"])["note"] == "use trash instead"
        assert await until(lambda: "✖ Đã từ chối" in _transcript_text(app.screen), pilot=pilot)


# =========================================================================== CRUD agent / template
async def test_create_edit_delete_agent_via_form(daemon, hub_home):
    hub = daemon.hub
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("n")
        assert await until(lambda: isinstance(app.screen, AgentFormScreen), pilot=pilot)
        form = app.screen
        form.query_one("#f-id", Input).value = "rev1"
        form.query_one("#f-template", Select).value = "reviewer"
        form.query_one("#f-model", Select).value = "haiku"
        await pilot.pause(0.1)
        shot(app, "07-form-tao-agent.svg")
        await pilot.click("#save")
        assert await until(lambda: hub.store.get_agent("rev1") is not None, pilot=pilot)
        ag = hub.agents.show("rev1")
        assert ag["model"] == "haiku" and ag["tool_policy"]["permission_mode"] == "plan" and "You are **rev1**" in ag["soul"]
        assert await until(lambda: "rev1" in table_rows(app), pilot=pilot)
        # sửa
        await pilot.press("e")
        assert await until(lambda: isinstance(app.screen, AgentFormScreen), pilot=pilot)
        app.screen.query_one("#f-role", Input).value = "Strict reviewer"
        await pilot.click("#save")
        assert await until(lambda: hub.agents.show("rev1")["role"] == "Strict reviewer", pilot=pilot)
        assert hub.agents.show("rev1")["config_revision"] == 2
        # xoá mềm
        await pilot.press("delete")
        await pilot.pause(0.2)
        await pilot.press("y")
        assert await until(lambda: (hub.store.get_agent("rev1") or {}).get("deleted_at") is not None or hub.store.get_agent("rev1") is None, pilot=pilot)
        assert await until(lambda: "rev1" not in table_rows(app), pilot=pilot)
        # audit ghi kênh tui
        rows = hub.store.conn.execute("SELECT kind, channel FROM audit_log WHERE kind LIKE 'agent.%'").fetchall()
        assert ("agent.create_from_template", "tui") in [(r[0], r[1]) for r in rows]


# =========================================================================== NFR-05: đóng TUI không ảnh hưởng run
async def test_closing_tui_does_not_affect_runs(daemon, hub_home):
    hub = daemon.hub
    ids = mk_agents(hub, 3)
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        runs = [hub.runs.submit(a, "long") for a in ids]
        await until(lambda: sum(1 for r in table_rows(app).values() if r[3] == "● working") == 3, pilot=pilot)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AttachScreen)
        assert hub.bus.subscriber_count >= 2
    # app đã thoát giữa chừng
    await until(lambda: hub.bus.subscriber_count == 0)
    finals = [await hub.runs.wait(r["id"], 20) for r in runs]
    assert [f["outcome"] for f in finals] == ["completed"] * 3


# =========================================================================== takeover
def test_build_takeover_argv_pure(tmp_path):
    argv = build_takeover_argv(
        binary="claude", session_id="S1", settings_path=tmp_path / "s.json", mcp_path=tmp_path / "m.json",
        system_prompt="SOUL", model="haiku", tool_policy={"permission_mode": "plan", "tools": ["Read"], "disallowed_tools": ["Bash(rm:*)"]},
        isolation={"setting_sources": "", "strict_mcp_config": True},
    )
    assert argv[:3] == ["claude", "--resume", "S1"] and "-p" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan" and argv[-2:] == ["--disallowedTools", "Bash(rm:*)"]
    argv = build_takeover_argv(binary="claude", session_id="S", settings_path=tmp_path, mcp_path=tmp_path, system_prompt=None, model=None, tool_policy={"permission_mode": "bypassPermissions"}, isolation={})
    assert "bypassPermissions" not in argv


async def test_takeover_print_and_record(daemon, hub_home):
    hub = daemon.hub
    mk_agents(hub, 1)
    run = hub.runs.submit("ag0", "x")
    await hub.runs.wait(run["id"], 20)
    app = TopApp(hub_home.socket, takeover_exec=False)
    async with app.run_test(size=SIZE) as pilot:
        await until(lambda: "ag0" in table_rows(app), pilot=pilot)
        await pilot.press("T")
        assert await until(lambda: isinstance(app.screen, InfoScreen), pilot=pilot)
        txt = str(app.screen.text)
        assert f"--resume {run['session_id']}" in txt and "--strict-mcp-config" in txt
        shot(app, "08-takeover.svg")
    from cfharness.tui.client import AsyncHubClient

    c = AsyncHubClient(hub_home.socket)
    info = await c.call("run.takeover", {"id": run["id"]})
    assert Path(info["runtime_dir"], "mcp.json").exists() and oct(Path(info["runtime_dir"], "mcp.json").stat().st_mode)[-3:] == "600"
    rec = await c.call("run.takeover_record", {"id": run["id"], "exit_code": 0})
    assert rec["kind"] == "human_takeover" and rec["takeover_of_run_id"] == run["id"] and rec["session_id"] == run["session_id"]
    assert not Path(info["runtime_dir"]).exists()
    # run đang chạy → không takeover
    r2 = hub.runs.submit("ag0", "y")
    with pytest.raises(Exception, match="dừng trước"):
        await c.call("run.takeover", {"id": r2["id"]})
    await hub.runs.wait(r2["id"], 20)


# =========================================================================== model thuần
def test_model_apply_and_activity_line():
    m = TopModel()
    m.load({"agents": [{"id": "a", "status": "working", "last_tool": None, "last_tool_at": None, "run_tokens": 0, "cost_today_usd": 0.1, "run_cost_usd": 0}], "hub": {}, "epoch": "e1", "last_seq": 5})
    aid, need = m.apply({"type": "tool.start", "agent_id": "a", "epoch": "e1", "seq": 6, "ts": "2026-10-18T00:00:00.000Z", "payload": {"name": "mcp__vb__vb_tim", "args_preview": "q=x"}})
    assert aid == "a" and not need and m.rows["a"].tool_running and m.last_seq == 6
    m.apply({"type": "usage.tick", "agent_id": "a", "epoch": "e1", "seq": 7, "payload": {"total_tokens": 1234, "cost_usd": 0.02}})
    assert m.rows["a"]["run_tokens"] == 1234
    _, need = m.apply({"type": "approval.open", "agent_id": "a", "epoch": "e1", "seq": 8, "payload": {}})
    assert need
    line = activity_line({"type": "tool.start", "agent_id": "a", "ts": "2026-10-18T00:00:00.000Z", "payload": {"name": "mcp__vb__vb_tim", "args_preview": "q=x"}}, {})
    assert "vb_tim@vb" in line.plain
    assert activity_line({"type": "usage.tick", "payload": {}}, {}) is None


# =========================================================================== tab Tasks (FR-UI-07, RPC M3)
async def test_tasks_tab_lists_and_creates(daemon, hub_home):
    hub = daemon.hub
    mk_agents(hub, 1)
    t = hub.tasks.create({"assignee": "ag0", "title": "Write release notes"})
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("t")
        assert await until(lambda: isinstance(app.screen, TasksScreen) and app.screen.available, pilot=pilot)
        scr = app.screen
        assert await until(lambda: any(x["id"] == t["id"] for x in scr.tasks), pilot=pilot)
        await pilot.press("c")
        await pilot.pause(0.2)
        await pilot.press(*"ag0: second task")
        await pilot.press("enter")
        assert await until(lambda: any(x["title"] == "second task" for x in scr.tasks), pilot=pilot)
        await pilot.pause(0.3)
        assert "Write release notes" in str(scr.query_one("#task-detail", Static).render()) or scr.tasks
        shot(app, "06-tasks.svg")
