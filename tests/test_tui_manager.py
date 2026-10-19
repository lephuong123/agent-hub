"""TUI `top` → phím `m`: màn "Điều phối" (tui/manager.py) — Textual Pilot + daemon IN-PROCESS (socket thật, ScriptedRuntime).

Dùng chung hội thoại kênh `cli` với `agent-hub chat`; audit kênh `tui`. Chữ của điều phối viên là DỮ LIỆU (không markup/ANSI/link).
Không gọi `claude`. Ảnh SVG: đặt `AGENT_HUB_SCREENSHOTS=docs/screenshots/m2`.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input, OptionList, RichLog

from agent_hub.events import make_event
from agent_hub.tui.app import AttachScreen, TasksScreen, TopApp
from agent_hub.tui.manager import ManagerScreen
from m7_fake import wait_status
from test_manager_repl import EVIL, designate, env, session  # noqa: F401  (fixture daemon + điều phối viên)
from test_tui import SIZE, shot, until


def log_text(scr: ManagerScreen) -> str:
    log = scr.query_one("#mgr-log", RichLog)
    return "\n".join("".join(seg.text for seg in line) for line in log.lines)


async def open_manager(app, pilot) -> ManagerScreen:
    await pilot.press("m")
    assert await until(lambda: isinstance(app.screen, ManagerScreen), pilot=pilot)
    scr = app.screen
    assert await until(lambda: scr.state is not None or "Chưa có điều phối viên" in log_text(scr), pilot=pilot)
    return scr


async def type_send(pilot, scr: ManagerScreen, text: str) -> None:
    inp = scr.query_one("#mgr-input", Input)
    inp.focus()
    inp.value = text
    await pilot.press("enter")


async def test_no_manager_shows_guidance_and_disables_input(env, hub_home):
    app = TopApp(hub_home.socket, refresh_s=0.5)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_manager(app, pilot)
        assert "Chưa có điều phối viên" in log_text(scr) and "agent-hub manager set" in log_text(scr)
        assert scr.query_one("#mgr-input", Input).disabled
        await pilot.press("escape")
        assert await until(lambda: not isinstance(app.screen, ManagerScreen), pilot=pilot)


async def test_chat_stream_actions_open_targets_and_shared_with_repl(env, hub_home):
    d = env
    await designate(d)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "worker", "model": "haiku"})
    task = d.hub.tasks.create({"assignee": "w1", "title": "Tóm tắt README"})
    app = TopApp(hub_home.socket, refresh_s=0.5)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_manager(app, pilot)
        assert scr.state.conversation_id is None  # chưa có hội thoại cli
        await type_send(pilot, scr, "giao việc tóm tắt README cho w1")
        assert await until(lambda: scr.state.active_run is not None, pilot=pilot)
        rid = scr.state.active_run
        await wait_status(d, rid)
        cid = scr.state.conversation_id
        assert cid and "giao việc tóm tắt README" in log_text(scr)
        d.hub.publish(make_event("message.delta", "boss", {"text": "Mình đang giao việc cho w1"}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "task_create", "target": f"task:{task['id']}", "summary": "Giao task cho w1", "ok": True}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "run_get", "target": f"run:{rid}", "summary": "Xem run", "ok": True}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "x", "target": "javascript:alert(1)", "summary": "Đích lạ", "ok": True}, run_id=rid))
        assert await until(lambda: len(scr.actions) == 3 and "đang giao việc" in str(scr.query_one("#mgr-status").render()), pilot=pilot)
        assert "Giao task cho w1" in log_text(scr)
        shot(app, "10-dieu-phoi-dang-lam.svg")
        d.hub.runs.runtime().release(rid)  # type: ignore[attr-defined]
        assert await until(lambda: not scr.state.busy and "Đã giao việc." in log_text(scr), pilot=pilot)
        # hội thoại xuất hiện ở cột trái
        assert await until(lambda: any(c["id"] == cid for c in scr.convs), pilot=pilot)
        await pilot.pause(0.3)
        shot(app, "11-dieu-phoi.svg")
        acts = scr.query_one("#mgr-actions", OptionList)
        ids = [acts.get_option_at_index(i).id for i in range(acts.option_count)]
        # mới nhất trước: Đích lạ, Xem run, Giao task
        # (1) đích lạ → chỉ thông báo, không mở màn nào
        await scr.open_target(scr.actions[2]["target"])
        await pilot.pause(0.1)
        assert isinstance(app.screen, ManagerScreen)
        # (2) run → Attach
        acts.focus()
        acts.highlighted = ids.index("a:1")
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, AttachScreen), pilot=pilot)
        assert app.screen.run_id == rid
        await pilot.press("escape")
        assert await until(lambda: isinstance(app.screen, ManagerScreen), pilot=pilot)
        # (3) task → Tasks, chọn sẵn đúng task
        acts = scr.query_one("#mgr-actions", OptionList)
        acts.focus()
        acts.highlighted = ids.index("a:0")
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, TasksScreen) and app.screen._selected_id() == task["id"], pilot=pilot)
        await pilot.press("escape")
        assert await until(lambda: isinstance(app.screen, ManagerScreen), pilot=pilot)
    # audit kênh tui, hội thoại kênh cli → `agent-hub chat` mở lại đúng hội thoại
    row = d.hub.store.conn.execute("SELECT actor, channel FROM audit_log WHERE kind='manager.chat' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["channel"] == "tui" and row["actor"] == "user"
    s, buf = session(hub_home)
    assert await s.open()
    await s.close()
    assert s.state.conversation_id == cid and "Đã giao việc." in buf.getvalue()


async def test_new_conversation_stop_turn_and_injection(env, hub_home):
    d = env
    await designate(d)
    d.script.reply = EVIL
    app = TopApp(hub_home.socket, refresh_s=0.5)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_manager(app, pilot)
        await type_send(pilot, scr, "một")
        assert await until(lambda: scr.state.active_run is not None, pilot=pilot)
        rid = scr.state.active_run
        await wait_status(d, rid)
        await asyncio.sleep(0.1)
        await type_send(pilot, scr, "thêm ý")  # đang làm → steer
        assert await until(lambda: "đã chèn tin" in log_text(scr), pilot=pilot)
        await pilot.press("ctrl+t")  # dừng lượt
        assert await until(lambda: not scr.state.busy and "không trả lời được" in log_text(scr), pilot=pilot)
        assert d.hub.store.get_run(rid)["status"] == "cancelled"
        c1 = scr.state.conversation_id
        await pilot.press("ctrl+n")
        assert scr.state.conversation_id is None
        d.script.hold_manager = False
        await type_send(pilot, scr, "hai")
        assert await until(lambda: scr.state.conversation_id not in (None, c1) and not scr.state.busy and "ghi đè" in log_text(scr), pilot=pilot)
        txt = log_text(scr)
        assert "[link=http://x]y[/link]" in txt and "[bold]B[/bold]" in txt
        for bad in ("http://evil", "pwned", "‮", "​", "\x1b", "\x07"):
            assert bad not in txt, bad
        # chọn lại hội thoại cũ ở cột trái
        convs = scr.query_one("#mgr-convs", OptionList)
        ids = [convs.get_option_at_index(i).id for i in range(convs.option_count)]
        assert ids[0] == "__new__" and f"c:{c1}" in ids
        convs.focus()
        convs.highlighted = ids.index(f"c:{c1}")
        await pilot.press("enter")
        assert await until(lambda: scr.state.conversation_id == c1 and "thêm ý" in log_text(scr), pilot=pilot)

